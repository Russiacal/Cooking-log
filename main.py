"""
Cooking-log MCP server.

Exposes tools that Claude (via a custom MCP connector) uses to publish new
cooks to Julia's cooking log. Storage is pluggable — local files for dev,
GitHub Contents API for prod. See backend.py.
"""
from __future__ import annotations

import os
import re
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

import uvicorn
import yaml
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from backend import build_backend

load_dotenv()

SITE_URL = os.environ.get("SITE_URL", "http://localhost:4321").rstrip("/")
BEARER_TOKEN = os.environ["MCP_BEARER_TOKEN"]
PORT = int(os.environ.get("PORT", "8765"))
HOST = os.environ.get("HOST", "0.0.0.0")
TZ = ZoneInfo(os.environ.get("TZ", "America/Los_Angeles"))

backend = build_backend()


def slugify(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^a-z0-9\s-]", "", text)
    text = re.sub(r"[\s-]+", "-", text)
    return text.strip("-")


def parse_frontmatter(text: str) -> Optional[dict]:
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 3)
    if end == -1:
        return None
    try:
        return yaml.safe_load(text[3:end])
    except yaml.YAMLError:
        return None


mcp = FastMCP("cooking-log", host=HOST, port=PORT)


@mcp.tool()
def publish_cook(
    title: str,
    body: str,
    source_url: Optional[str] = None,
    source_name: Optional[str] = None,
    made_on: Optional[str] = None,
    tags: Optional[list[str]] = None,
    photos: Optional[list[str]] = None,
    photo_credit: Optional[str] = None,
) -> str:
    """Publish a new cook to Julia's cooking log.

    Writes a markdown file with structured frontmatter to the log's storage
    (local files in dev, GitHub via API in prod). The blog auto-reloads /
    auto-deploys and the post is live at the returned URL.

    Guidance for writing body content:
    - Keep it conversational — Julia's voice, first person.
    - Include an `## Ingredients` H2 section listing everything she used
      (with her substitutions embedded, e.g. "6-8 anchovies (in place of
      1 tsp anchovy paste)"). Ingredients let her cook from the log without
      opening the source recipe.
    - Modifications, what worked, what didn't — embed in flowing prose or
      short bullet lists as appropriate. Don't force sections that aren't
      natural for the cook.
    - For collapsible detailed directions on single-source cooks, use
      <details><summary>Directions</summary>...</details> — plain HTML in
      markdown works.
    - For mixed-recipe cooks, describe the hybrid approach in prose.

    Args:
        title: Recipe title, e.g. "Miso-glazed salmon" or "Super quick Caesar
            salad dressing" (required).
        body: Markdown body of the post (required).
        source_url: URL of the original recipe, if any.
        source_name: Human-friendly source name, e.g. "NYT Cooking",
            "Bon Appétit", "Once Upon a Chef".
        made_on: Date cooked, YYYY-MM-DD. Defaults to today (LA time).
        tags: 3-5 tags for filtering/discovery, e.g. ["pasta", "weeknight",
            "italian"]. Lowercase, short.
        photos: Image URLs. First one becomes the card thumbnail. Use the
            source recipe photo if Julia hasn't shared her own.
        photo_credit: Attribution for the photo when it's from the source
            recipe, e.g. "Jennifer Segal / Once Upon a Chef". Omit for
            Julia's own photos.

    Returns:
        The URL where the new cook is now live.
    """
    made_on_date = made_on or datetime.now(TZ).date().isoformat()
    slug = f"{made_on_date}-{slugify(title)}"

    fm: dict = {
        "title": title,
        "made_on": made_on_date,
    }
    if source_url:
        fm["source_url"] = source_url
    if source_name:
        fm["source_name"] = source_name
    fm["tags"] = tags or []
    fm["photos"] = photos or []
    if photo_credit:
        fm["photo_credit"] = photo_credit

    yaml_str = yaml.safe_dump(fm, sort_keys=False, allow_unicode=True).strip()
    content = f"---\n{yaml_str}\n---\n\n{body.strip()}\n"

    backend.write(slug, content)

    return f"{SITE_URL}/{slug}/"


@mcp.tool()
def list_recent_cooks(n: int = 10) -> list[dict]:
    """List the N most recently-cooked posts (newest first, by filename date).

    Useful for referencing past cooks in conversation — "I've made cacio e
    pepe twice now, here's my latest tweak." Also useful to check what tags
    Julia has been using so new posts fit the existing tag vocabulary.

    Args:
        n: How many recent cooks to return. Defaults to 10.

    Returns:
        List of {slug, title, made_on, tags, source_name, url} dicts.
    """
    cooks: list[dict] = []
    for slug in backend.list_slugs_reverse():
        if len(cooks) >= n:
            break
        text = backend.read(slug)
        fm = parse_frontmatter(text)
        if fm is None:
            continue
        cooks.append(
            {
                "slug": slug,
                "title": fm.get("title", ""),
                "made_on": str(fm.get("made_on", "")),
                "tags": fm.get("tags") or [],
                "source_name": fm.get("source_name"),
                "url": f"{SITE_URL}/{slug}/",
            }
        )
    return cooks


@mcp.tool()
def search_cooks(query: str) -> list[dict]:
    """Search all cooks for a query string — case-insensitive substring across
    title, body, tags, and source name. Newest matches first.

    Useful for "have I made this before?" — e.g. search("anchovy") finds all
    cooks that mention anchovies anywhere.

    Args:
        query: Search string, e.g. "anchovy" or "sourdough" or "sheet pan".

    Returns:
        List of {slug, title, made_on, url, matched_context} dicts. The
        matched_context is a short excerpt showing where the query matched.
    """
    q = query.lower().strip()
    if not q:
        return []
    results: list[dict] = []
    for slug in backend.list_slugs_reverse():
        text = backend.read(slug)
        lower = text.lower()
        idx = lower.find(q)
        if idx == -1:
            continue
        fm = parse_frontmatter(text)
        if fm is None:
            continue
        ctx_start = max(0, idx - 40)
        ctx_end = min(len(text), idx + len(q) + 40)
        context = text[ctx_start:ctx_end].replace("\n", " ").strip()
        results.append(
            {
                "slug": slug,
                "title": fm.get("title", ""),
                "made_on": str(fm.get("made_on", "")),
                "url": f"{SITE_URL}/{slug}/",
                "matched_context": f"…{context}…",
            }
        )
    return results


class BearerAuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, token: str):
        super().__init__(app)
        self._expected = f"Bearer {token}"

    async def dispatch(self, request, call_next):
        auth = request.headers.get("authorization", "")
        if auth != self._expected:
            return JSONResponse(
                {"error": "unauthorized", "detail": "missing or invalid Bearer token"},
                status_code=401,
            )
        return await call_next(request)


def build_app():
    app = mcp.streamable_http_app()
    app.add_middleware(BearerAuthMiddleware, token=BEARER_TOKEN)
    return app


app = build_app()


if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")
