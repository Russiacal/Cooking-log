"""
Cooking-log MCP server.

Exposes tools that Claude (via a custom MCP connector) uses to publish new
cooks to Julia's cooking log. Storage is pluggable — local files for dev,
GitHub Contents API for prod. See backend.py.

Auth is dual-mode:
- Static Bearer token (MCP_BEARER_TOKEN) — used by smoke tests and any
  direct API access.
- OAuth 2.1 authorization_code + PKCE — required by Claude.ai's custom
  connector UI. See oauth.py.
"""
from __future__ import annotations

import os
import re
from datetime import datetime
from typing import Optional
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import uvicorn
import yaml
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse
from starlette.routing import Route

from backend import build_backend
from oauth import OAuthStore, verify_pkce

load_dotenv()

SITE_URL = os.environ.get("SITE_URL", "http://localhost:4321").rstrip("/")
BEARER_TOKEN = os.environ["MCP_BEARER_TOKEN"]
PORT = int(os.environ.get("PORT", "8765"))
HOST = os.environ.get("HOST", "0.0.0.0")
TZ = ZoneInfo(os.environ.get("TZ", "America/Los_Angeles"))

OAUTH_CLIENT_ID = os.environ.get("OAUTH_CLIENT_ID")
OAUTH_CLIENT_SECRET = os.environ.get("OAUTH_CLIENT_SECRET")
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")

backend = build_backend()
oauth_store = OAuthStore()


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


# ─────────────────────────────────────────────────────────
# OAuth 2.1 endpoints (for Claude.ai's custom connector UI)
# ─────────────────────────────────────────────────────────

# Paths that must be reachable WITHOUT auth for the OAuth handshake to work.
PUBLIC_PATHS = frozenset(
    {
        "/.well-known/oauth-authorization-server",
        "/.well-known/oauth-protected-resource",
        "/authorize",
        "/token",
    }
)


def _base_url(request: Request) -> str:
    if PUBLIC_URL:
        return PUBLIC_URL
    return str(request.base_url).rstrip("/")


async def oauth_authorization_server_metadata(request: Request) -> JSONResponse:
    base = _base_url(request)
    return JSONResponse(
        {
            "issuer": base,
            "authorization_endpoint": f"{base}/authorize",
            "token_endpoint": f"{base}/token",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["client_secret_post"],
            "scopes_supported": ["mcp"],
        }
    )


async def oauth_protected_resource_metadata(request: Request) -> JSONResponse:
    base = _base_url(request)
    return JSONResponse(
        {
            "resource": base,
            "authorization_servers": [base],
            "scopes_supported": ["mcp"],
            "bearer_methods_supported": ["header"],
        }
    )


async def authorize(request: Request) -> JSONResponse | RedirectResponse:
    """Auto-approve. Single-user tool — Julia IS the user consenting."""
    if not OAUTH_CLIENT_ID:
        return JSONResponse(
            {"error": "server_error", "error_description": "OAuth not configured"},
            status_code=500,
        )

    params = request.query_params
    client_id = params.get("client_id", "")
    redirect_uri = params.get("redirect_uri", "")
    state = params.get("state", "")
    code_challenge = params.get("code_challenge", "")
    code_challenge_method = params.get("code_challenge_method", "")
    response_type = params.get("response_type", "")

    if client_id != OAUTH_CLIENT_ID:
        return JSONResponse({"error": "invalid_client"}, status_code=400)
    if response_type != "code":
        return JSONResponse({"error": "unsupported_response_type"}, status_code=400)
    if code_challenge_method != "S256" or not code_challenge:
        return JSONResponse(
            {
                "error": "invalid_request",
                "error_description": "code_challenge with S256 required",
            },
            status_code=400,
        )
    if not redirect_uri:
        return JSONResponse(
            {"error": "invalid_request", "error_description": "redirect_uri required"},
            status_code=400,
        )

    code = oauth_store.issue_code(
        client_id=client_id,
        redirect_uri=redirect_uri,
        code_challenge=code_challenge,
        code_challenge_method=code_challenge_method,
    )
    qs = {"code": code}
    if state:
        qs["state"] = state
    separator = "&" if "?" in redirect_uri else "?"
    return RedirectResponse(f"{redirect_uri}{separator}{urlencode(qs)}", status_code=302)


async def token(request: Request) -> JSONResponse:
    if not OAUTH_CLIENT_ID or not OAUTH_CLIENT_SECRET:
        return JSONResponse(
            {"error": "server_error", "error_description": "OAuth not configured"},
            status_code=500,
        )

    form = await request.form()
    grant_type = form.get("grant_type", "")
    if grant_type != "authorization_code":
        return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)

    client_id = form.get("client_id", "")
    client_secret = form.get("client_secret", "")
    code = form.get("code", "")
    code_verifier = form.get("code_verifier", "")

    if client_id != OAUTH_CLIENT_ID or client_secret != OAUTH_CLIENT_SECRET:
        return JSONResponse({"error": "invalid_client"}, status_code=401)

    entry = oauth_store.consume_code(code)
    if entry is None:
        return JSONResponse(
            {"error": "invalid_grant", "error_description": "code invalid or expired"},
            status_code=400,
        )

    if not verify_pkce(code_verifier, entry.code_challenge, entry.code_challenge_method):
        return JSONResponse(
            {"error": "invalid_grant", "error_description": "PKCE verification failed"},
            status_code=400,
        )

    access_token = oauth_store.issue_token(client_id)
    return JSONResponse(
        {
            "access_token": access_token,
            "token_type": "Bearer",
            "expires_in": 30 * 24 * 3600,
            "scope": "mcp",
        }
    )


# ─────────────────────────────────────────────────────────
# Auth middleware — accepts EITHER static bearer OR OAuth token
# ─────────────────────────────────────────────────────────

class BearerAuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, static_token: str, store: OAuthStore) -> None:
        super().__init__(app)
        self._static_token = static_token
        self._store = store

    async def dispatch(self, request, call_next):
        if request.url.path in PUBLIC_PATHS:
            return await call_next(request)

        auth = request.headers.get("authorization", "")
        if not auth.startswith("Bearer "):
            return self._challenge(request)

        token_value = auth[len("Bearer "):].strip()
        if token_value == self._static_token or self._store.validate_token(token_value):
            return await call_next(request)

        return self._challenge(request)

    def _challenge(self, request: Request) -> JSONResponse:
        base = _base_url(request)
        return JSONResponse(
            {"error": "unauthorized", "detail": "missing or invalid Bearer token"},
            status_code=401,
            headers={
                "WWW-Authenticate": (
                    f'Bearer realm="cooking-log", '
                    f'resource_metadata="{base}/.well-known/oauth-protected-resource"'
                )
            },
        )


def build_app():
    app = mcp.streamable_http_app()
    app.router.routes.extend(
        [
            Route(
                "/.well-known/oauth-authorization-server",
                oauth_authorization_server_metadata,
                methods=["GET"],
            ),
            Route(
                "/.well-known/oauth-protected-resource",
                oauth_protected_resource_metadata,
                methods=["GET"],
            ),
            Route("/authorize", authorize, methods=["GET"]),
            Route("/token", token, methods=["POST"]),
        ]
    )
    app.add_middleware(BearerAuthMiddleware, static_token=BEARER_TOKEN, store=oauth_store)
    return app


app = build_app()


if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")
