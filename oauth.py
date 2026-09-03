"""
Minimal OAuth 2.1 authorization_code + PKCE server for the cooking-log MCP.

Why this exists: Claude.ai's custom MCP connector UI requires OAuth (not
raw Bearer tokens). This is the smallest possible OAuth server that
satisfies the flow — single pre-shared client_id/secret pair, auto-approves
the /authorize step (no consent screen — this is Julia's tool, she IS
the consenter).

Split of state:
- Authorization codes stay in-memory. 5-minute TTL, single-use; if a
  Railway restart drops them mid-handshake, Claude just retries.
- Access tokens live in SQLite (via store.OAuthTokenStore) so restarts
  don't force Julia to re-auth. Default TTL is 1 year — the code no
  longer bears the "tokens die on restart" limitation.
"""
from __future__ import annotations

import base64
import hashlib
import secrets
import time
from dataclasses import dataclass
from typing import Optional

from store import OAuthTokenStore


CODE_TTL_SECONDS = 300  # authorization code lives 5 min
DEFAULT_TOKEN_TTL_SECONDS = 365 * 24 * 3600  # 1 year


@dataclass
class AuthCode:
    client_id: str
    redirect_uri: str
    code_challenge: str
    code_challenge_method: str
    expires_at: float


class OAuthStore:
    """Codes in-memory, tokens delegated to a persistent OAuthTokenStore."""

    def __init__(self, token_store: OAuthTokenStore) -> None:
        self._codes: dict[str, AuthCode] = {}
        self._tokens = token_store

    def issue_code(
        self,
        client_id: str,
        redirect_uri: str,
        code_challenge: str,
        code_challenge_method: str,
    ) -> str:
        code = secrets.token_urlsafe(32)
        self._codes[code] = AuthCode(
            client_id=client_id,
            redirect_uri=redirect_uri,
            code_challenge=code_challenge,
            code_challenge_method=code_challenge_method,
            expires_at=time.time() + CODE_TTL_SECONDS,
        )
        return code

    def consume_code(self, code: str) -> Optional[AuthCode]:
        entry = self._codes.pop(code, None)
        if entry is None or entry.expires_at < time.time():
            return None
        return entry

    def issue_token(self, client_id: str) -> str:
        token = secrets.token_urlsafe(32)
        self._tokens.issue(token, client_id)
        return token

    def validate_token(self, token: str) -> bool:
        return self._tokens.validate(token)


def verify_pkce(code_verifier: str, code_challenge: str, method: str) -> bool:
    """RFC 7636 S256 verification.

    S256: BASE64URL(SHA256(code_verifier)) == code_challenge
    """
    if method != "S256" or not code_verifier or not code_challenge:
        return False
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    computed = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return secrets.compare_digest(computed, code_challenge)
