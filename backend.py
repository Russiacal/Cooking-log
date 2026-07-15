"""
Storage backends for the cooking-log MCP server.

Two modes:
- LocalFileBackend: writes markdown files directly to a local directory
  (dev — Astro's file watcher picks up changes and hot-reloads).
- GitHubBackend: commits markdown files to the julia-cooks GitHub repo via
  the Contents API (prod — Vercel auto-deploys on push).

Pick one via env vars. See build_backend() at the bottom.
"""
from __future__ import annotations

import base64
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Protocol

import httpx


class CooksBackend(Protocol):
    def write(self, slug: str, content: str) -> None: ...
    def read(self, slug: str) -> str: ...
    def list_slugs_reverse(self) -> list[str]: ...


class LocalFileBackend:
    def __init__(self, cooks_dir: Path):
        self.dir = cooks_dir.resolve()
        if not self.dir.is_dir():
            raise SystemExit(f"COOKS_DIR does not exist: {self.dir}")

    def write(self, slug: str, content: str) -> None:
        (self.dir / f"{slug}.md").write_text(content, encoding="utf-8")

    def read(self, slug: str) -> str:
        return (self.dir / f"{slug}.md").read_text(encoding="utf-8")

    def list_slugs_reverse(self) -> list[str]:
        return sorted((f.stem for f in self.dir.glob("*.md")), reverse=True)


class GitHubBackend:
    """Commits markdown to a GitHub repo via the Contents API.

    Reads are cached in-process (list: 30s, per-file content: 5min) so that
    a burst of tool calls in one Claude conversation doesn't spam the API.
    Cache is busted on write for the affected slug and the list.
    """

    LIST_TTL = timedelta(seconds=30)
    READ_TTL = timedelta(seconds=300)

    def __init__(self, token: str, repo: str, branch: str, path_prefix: str):
        self.repo = repo
        self.branch = branch
        self.prefix = path_prefix.strip("/")
        self._client = httpx.Client(
            base_url="https://api.github.com",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "cooking-log-mcp",
            },
            timeout=20.0,
        )
        self._list_cache: Optional[tuple[datetime, list[str]]] = None
        self._read_cache: dict[str, tuple[datetime, str]] = {}

    def _path(self, slug: str) -> str:
        return f"{self.prefix}/{slug}.md"

    def write(self, slug: str, content: str) -> None:
        path = self._path(slug)
        existing_sha: Optional[str] = None
        r = self._client.get(
            f"/repos/{self.repo}/contents/{path}", params={"ref": self.branch}
        )
        if r.status_code == 200:
            existing_sha = r.json().get("sha")

        body: dict = {
            "message": f"{'Update' if existing_sha else 'Add'} cook: {slug}",
            "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
            "branch": self.branch,
        }
        if existing_sha:
            body["sha"] = existing_sha

        r = self._client.put(f"/repos/{self.repo}/contents/{path}", json=body)
        r.raise_for_status()

        self._list_cache = None
        self._read_cache.pop(slug, None)

    def list_slugs_reverse(self) -> list[str]:
        now = datetime.now()
        if self._list_cache:
            ts, slugs = self._list_cache
            if now - ts < self.LIST_TTL:
                return slugs

        r = self._client.get(
            f"/repos/{self.repo}/contents/{self.prefix}",
            params={"ref": self.branch},
        )
        if r.status_code == 404:
            slugs: list[str] = []
        else:
            r.raise_for_status()
            items = r.json()
            slugs = sorted(
                (
                    item["name"].removesuffix(".md")
                    for item in items
                    if item.get("type") == "file" and item["name"].endswith(".md")
                ),
                reverse=True,
            )

        self._list_cache = (now, slugs)
        return slugs

    def read(self, slug: str) -> str:
        now = datetime.now()
        cached = self._read_cache.get(slug)
        if cached and now - cached[0] < self.READ_TTL:
            return cached[1]

        r = self._client.get(
            f"/repos/{self.repo}/contents/{self._path(slug)}",
            params={"ref": self.branch},
        )
        r.raise_for_status()
        data = r.json()
        content = base64.b64decode(data["content"]).decode("utf-8")
        self._read_cache[slug] = (now, content)
        return content


def build_backend() -> CooksBackend:
    """Pick a backend from env vars.

    GitHub mode (prod): GITHUB_TOKEN + GITHUB_REPO set. Optional:
      GITHUB_BRANCH (default 'main'), GITHUB_COOKS_PATH (default
      'src/content/cooks').

    Local mode (dev): COOKS_DIR set to an existing directory.
    """
    token = os.environ.get("GITHUB_TOKEN")
    repo = os.environ.get("GITHUB_REPO")
    if token and repo:
        return GitHubBackend(
            token=token,
            repo=repo,
            branch=os.environ.get("GITHUB_BRANCH", "main"),
            path_prefix=os.environ.get("GITHUB_COOKS_PATH", "src/content/cooks"),
        )
    cooks_dir = os.environ.get("COOKS_DIR")
    if cooks_dir:
        return LocalFileBackend(Path(cooks_dir))
    raise SystemExit(
        "No backend configured. Set GITHUB_TOKEN + GITHUB_REPO (prod) "
        "or COOKS_DIR (local dev)."
    )
