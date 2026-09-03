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

Photo pipeline (Phase 2):
- iOS Shortcut POSTs to /photos/pending with SHORTCUT_BEARER_TOKEN (a
  separate bearer, different from MCP_BEARER_TOKEN so we can rotate one
  without the other).
- get_pending_photos() lets Claude peek at the queue.
- publish_cook() auto-attaches unconsumed photos when the `photos` arg
  is omitted, then marks them consumed.
"""
from __future__ import annotations

import json
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
from oauth import DEFAULT_TOKEN_TTL_SECONDS, OAuthStore, verify_pkce
from store import build_stores

load_dotenv()

SITE_URL = os.environ.get("SITE_URL", "http://localhost:4321").rstrip("/")
BEARER_TOKEN = os.environ["MCP_BEARER_TOKEN"]
PORT = int(os.environ.get("PORT", "8765"))
HOST = os.environ.get("HOST", "0.0.0.0")
TZ = ZoneInfo(os.environ.get("TZ", "America/Los_Angeles"))

OAUTH_CLIENT_ID = os.environ.get("OAUTH_CLIENT_ID")
OAUTH_CLIENT_SECRET = os.environ.get("OAUTH_CLIENT_SECRET")
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")

SHORTCUT_BEARER_TOKEN = os.environ.get("SHORTCUT_BEARER_TOKEN")
CLOUDINARY_CLOUD_NAME = os.environ.get("CLOUDINARY_CLOUD_NAME")

backend = build_backend()
_db, token_store, photo_queue = build_stores(token_ttl_seconds=DEFAULT_TOKEN_TTL_SECONDS)
oauth_store = OAuthStore(token_store)


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

    BEFORE DRAFTING: call `list_recent_cooks(n=3)` and mirror the voice of
    those posts. The rules below are backup; matching real posts is primary.

    VOICE RULES — hard, apply to every draft:

    1. NEVER use "you" or "your". Julia writes for herself, not an
       audience. Every "you" must become "I"/"we" or be removed.

    2. Pronouns match reality. Julia solo = "I". Julia cooked with
       someone (partner, friend, kid) = "we". If unclear from her
       message who cooked, ASK before drafting.

    3. No editorializing. No food-writing register. The body is a
       memory aid, not an essay. Cut:
       - Sensory adjectives Julia didn't write ("delightful",
         "crunchy", "sharp", "silky", "delicious")
       - Explanations of why techniques work ("spreads the brine
         through the whole salad", "what you want against the rich
         stuff")
       - Outcome judgments ("worked great", "the star", "chef's
         kiss", "worth it")
       - Suggestions or improvements Julia didn't raise
       Whenever Julia provides phrasing, preserve it exactly — don't
       paraphrase or smooth it out. Never invent commentary.

    4. Full grammatical sentences in prose. No fragments.
       ❌ "Tonnino oil-packed tuna, the espelette pepper one."
       ✓ "We used Tonnino oil-packed tuna, the espelette pepper
          variety."

    5. Facts only, and only the SALIENT facts:
       - Note deviations from the source, not compliance. Assume
         Julia followed the recipe unless she says otherwise — don't
         write "added chorizo per the recipe", she knows.
       - Don't note absences (skipped ingredients, omitted steps)
         unless Julia mentioned them.
       - Skip trivial substitutions. If the recipe just says
         "paprika", the specific variety Julia used isn't worth
         mentioning. Only call out swaps that change the dish.
       - Include what ran long/short/didn't work.

    CONCRETE BEFORE/AFTER — Julia had to rewrite this herself, so it's
    canonical:

    ❌ Editorializing (rejected — has "you", explains, judges):
       "The one move I really kept from them: mince the anchovies
        straight into the dressing instead of draping fillets on top.
        Spreads the brine through the whole salad and you never get
        a whole-anchovy bite."

    ✓ Facts only (approved):
       "We kept their move of mincing the anchovies straight into
        the dressing instead of draping fillets on top."

    STRUCTURE:
    - Opening sentence(s): why Julia made it or where the idea came
      from, in her words.
    - Body: 1-3 prose paragraphs — what she followed, what she
      swapped and why, any technique notes. Embedded naturally, not
      bulleted.
    - `## Ingredients` — bullets listing everything used, with
      substitutions noted inline, e.g. "6-8 anchovies (in place of
      1 tsp anchovy paste)". Lets Julia re-cook from the log without
      opening the source.
    - `## Next time` — only if Julia explicitly gave next-time notes.
      Don't infer or promote body observations to this section.

    PHOTOS:
    - If `photos` is omitted or None, any pending photos uploaded via
      Julia's iOS Shortcut are auto-attached and the queue is cleared.
      This is the default happy path — she snaps photos while cooking,
      the Shortcut queues them, publish_cook picks them up.
    - Pass `photos=[]` explicitly to publish with NO photos even if
      there are pending ones.
    - Pass a specific list of URLs to override (queue is left alone
      and NOT cleared).
    - If no pending photos exist AND `photos` is omitted, use the
      source recipe's photo (with `photo_credit`) so the card has a
      thumbnail.

    Args:
        title: Recipe title, e.g. "Miso-glazed salmon" (required).
        body: Markdown body per rules above (required).
        source_url: URL of the original recipe.
        source_name: Human-friendly source name, e.g. "NYT Cooking",
            "ATK", "Once Upon a Chef".
        made_on: Date cooked, YYYY-MM-DD. Defaults to today (LA time).
        tags: 3-5 lowercase tags. Reuse existing vocab — check
            list_recent_cooks first.
        photos: Image URLs. First is the card thumbnail. Omit to
            auto-attach queued photos; pass [] for no photos; pass a
            list to override.
        photo_credit: Attribution when photo is from the source, e.g.
            "Jennifer Segal / Once Upon a Chef". Omit for Julia's own
            photos.

    Returns:
        The URL where the new cook is now live.
    """
    made_on_date = made_on or datetime.now(TZ).date().isoformat()
    slug = f"{made_on_date}-{slugify(title)}"

    if photos is None:
        resolved_photos = photo_queue.consume_all()
    else:
        resolved_photos = photos

    fm: dict = {
        "title": title,
        "made_on": made_on_date,
    }
    if source_url:
        fm["source_url"] = source_url
    if source_name:
        fm["source_name"] = source_name
    fm["tags"] = tags or []
    fm["photos"] = resolved_photos
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


@mcp.tool()
def get_pending_photos() -> dict:
    """Peek at photos uploaded via the iOS Shortcut but not yet attached to a cook.

    Useful when Julia says "publish tonight's salmon with the photos I just
    took" — this confirms what's queued. Note: this does NOT consume the
    queue. publish_cook consumes it when the `photos` argument is omitted.

    Returns:
        {count: int, urls: list[str]} — URLs in upload order (oldest first).
    """
    urls = photo_queue.list_unconsumed()
    return {"count": len(urls), "urls": urls}


# ─────────────────────────────────────────────────────────
# OAuth 2.1 endpoints (for Claude.ai's custom connector UI)
# ─────────────────────────────────────────────────────────

# Paths that must be reachable WITHOUT the MCP auth middleware.
# OAuth endpoints are truly public; /photos/pending has its own bearer
# check inside the handler using SHORTCUT_BEARER_TOKEN.
PUBLIC_PATHS = frozenset(
    {
        "/.well-known/oauth-authorization-server",
        "/.well-known/oauth-protected-resource",
        "/authorize",
        "/token",
        "/photos/pending",
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
            "expires_in": DEFAULT_TOKEN_TTL_SECONDS,
            "scope": "mcp",
        }
    )


# ─────────────────────────────────────────────────────────
# Photos endpoint (for iOS Shortcut)
# ─────────────────────────────────────────────────────────

async def photos_pending(request: Request) -> JSONResponse:
    """POST {urls: [str], uploaded_at?: float} → queue photos for next publish.

    Auth: SHORTCUT_BEARER_TOKEN (separate from MCP_BEARER_TOKEN so the
    Shortcut's embedded token can be rotated independently of Claude's).

    If CLOUDINARY_CLOUD_NAME is set, URLs must start with
    https://res.cloudinary.com/<cloud>/ — cheap safety check against
    someone stumbling on the endpoint and injecting arbitrary URLs.
    """
    if not SHORTCUT_BEARER_TOKEN:
        return JSONResponse(
            {"error": "server_error", "detail": "SHORTCUT_BEARER_TOKEN not configured"},
            status_code=500,
        )

    auth = request.headers.get("authorization", "")
    if auth != f"Bearer {SHORTCUT_BEARER_TOKEN}":
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    try:
        payload = await request.json()
    except (ValueError, json.JSONDecodeError):
        return JSONResponse({"error": "invalid_json"}, status_code=400)

    urls = payload.get("urls")
    if not isinstance(urls, list) or not all(isinstance(u, str) for u in urls):
        return JSONResponse(
            {"error": "invalid_request", "detail": "urls must be a list of strings"},
            status_code=400,
        )

    if CLOUDINARY_CLOUD_NAME:
        prefix = f"https://res.cloudinary.com/{CLOUDINARY_CLOUD_NAME}/"
        bad = [u for u in urls if not u.startswith(prefix)]
        if bad:
            return JSONResponse(
                {
                    "error": "invalid_request",
                    "detail": f"URLs must start with {prefix}",
                    "rejected": bad,
                },
                status_code=400,
            )

    uploaded_at = payload.get("uploaded_at")
    added = photo_queue.add(urls, uploaded_at=uploaded_at)
    return JSONResponse(
        {"added": added, "total_pending": photo_queue.count_unconsumed()}
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
            Route("/photos/pending", photos_pending, methods=["POST"]),
        ]
    )
    app.add_middleware(BearerAuthMiddleware, static_token=BEARER_TOKEN, store=oauth_store)
    return app


app = build_app()


if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")
