"""Publishes interactive plot pages (HTML files produced by a run) for the web container.

Each run's pages go into their own directory named by an unguessable token, so a link
only works for people it was posted to. Directories older than the TTL are deleted.
"""

import os
import secrets
import shutil
import time
from collections.abc import Sequence
from pathlib import Path
from urllib.parse import quote


class PagePublisher:
    def __init__(self, directory: str, base_url: str, ttl_days: float):
        self.dir = Path(directory)
        self.base_url = base_url.rstrip("/")
        self.ttl = ttl_days * 86400

    def publish(self, files: Sequence[tuple[str, bytes]]) -> list[tuple[str, str]]:
        """Write the pages and return (file name, public URL) pairs."""
        if not files:
            return []
        token = secrets.token_urlsafe(18)
        target = self.dir / token
        target.mkdir(parents=True, mode=0o755)
        os.chmod(target, 0o755)
        links = []
        for name, data in files:
            safe = Path(name).name or "page.html"
            path = target / safe
            path.write_bytes(data)
            os.chmod(path, 0o644)
            links.append((safe, f"{self.base_url}/{token}/{quote(safe)}"))
        return links

    def cleanup(self) -> int:
        """Delete published runs older than the TTL. Returns how many were removed."""
        if not self.dir.is_dir():
            return 0
        cutoff = time.time() - self.ttl
        removed = 0
        for entry in self.dir.iterdir():
            if entry.is_dir() and not entry.is_symlink() and entry.stat().st_mtime < cutoff:
                shutil.rmtree(entry, ignore_errors=True)
                removed += 1
        return removed
