"""
Cache-first fetch plumbing shared by both collectors.

Every network access in the project goes through one of two doors here:
  - Cache.get / Cache.put for raw response bodies, keyed by URL hash
  - RateLimiter to space real network calls 2.5s apart, single-threaded

A second run with a warm cache must make zero network calls; FetchStats
counts them so the acceptance test can assert exactly that.
"""

import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = ROOT / "data" / "cache"

# Paths Checkatrade's robots.txt disallows. Never fetch these, even by accident
# (e.g. a harvested href pointing somewhere unexpected).
ROBOTS_DISALLOWED = ("/Account/", "/GiveFeedback/", "/bookable-services/", "/raq-message")
ROBOTS_ALLOWED_PREFIXES = ("/trades/", "/Search/")


class FetchBlockedError(RuntimeError):
    """Raised on the first 403/429 or bot-challenge page. Never retried."""

    def __init__(self, url, status, detail=""):
        self.url = url
        self.status = status
        super().__init__(f"blocked ({status}) at {url} {detail}".strip())


class RobotsError(RuntimeError):
    pass


def check_checkatrade_path(url):
    path = url.split("checkatrade.com", 1)[-1] if "checkatrade.com" in url else url
    if any(bad in path for bad in ROBOTS_DISALLOWED):
        raise RobotsError(f"robots.txt disallows this path, refusing to fetch: {url}")
    if not path.startswith(ROBOTS_ALLOWED_PREFIXES):
        raise RobotsError(f"path outside the allowed /trades/ and /Search/ set: {url}")


def load_env():
    """Parse .env into a dict. Values are secrets: never print or log them."""
    env = {}
    path = ROOT / ".env"
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                env[key.strip()] = value.strip()
    return env


class FetchStats:
    def __init__(self):
        self.network_calls = 0
        self.cache_hits = 0

    def as_dict(self):
        return {"network_calls": self.network_calls, "cache_hits": self.cache_hits}


class RateLimiter:
    def __init__(self, min_interval=2.5):
        self.min_interval = min_interval
        self._last = 0.0

    def wait(self):
        elapsed = time.monotonic() - self._last
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
        self._last = time.monotonic()


class Cache:
    def __init__(self, root=CACHE_DIR):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _key(self, url):
        return hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]

    def get(self, url):
        """Return cached body text, or None on a miss."""
        body = self.root / f"{self._key(url)}.body"
        if body.exists():
            return body.read_text(encoding="utf-8")
        return None

    def put(self, url, text, status=200, kind="html"):
        key = self._key(url)
        (self.root / f"{key}.body").write_text(text, encoding="utf-8")
        meta = {
            "url": url,
            "status": status,
            "kind": kind,
            "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        (self.root / f"{key}.meta.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
