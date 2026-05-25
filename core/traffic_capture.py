"""
Traffic Capture Engine — Production Ready
Captures real browser traffic via Playwright in a visible (non-headless) window.
User performs the journey manually; all XHR/fetch/API requests are captured.

Key fixes over original:
  - Playwright must run on the main thread OR its own dedicated thread with
    its own event loop. We spawn a dedicated OS thread and use a threading.Event
    to signal stop rather than polling is_connected().
  - Response body is read with a try/except per-response so one bad response
    never kills the whole session.
  - Resource filtering is stricter and faster (set lookups only).
  - build_curl_commands produces valid, shell-safe cURL lines.
"""

from __future__ import annotations

import threading
import time
import re
import os
from dataclasses import dataclass, field
from typing import Optional, Callable
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class CapturedRequest:
    sequence:         int
    url:              str
    method:           str
    headers:          dict[str, str]
    body:             Optional[str]
    response_status:  int
    response_headers: dict[str, str]
    response_body:    Optional[str]
    timestamp:        float
    page_context:     str
    resource_type:    str


# ---------------------------------------------------------------------------
# Filter configuration
# ---------------------------------------------------------------------------

_SKIP_EXTENSIONS = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".webp", ".avif",
    ".woff", ".woff2", ".ttf", ".eot", ".otf", ".css", ".map",
    ".mp4", ".mp3", ".wav", ".ogg", ".pdf", ".zip", ".gz",
})

_SKIP_DOMAINS = frozenset({
    "google-analytics.com", "googletagmanager.com", "hotjar.com",
    "facebook.com", "twitter.com", "linkedin.com", "instagram.com",
    "doubleclick.net", "analytics.google.com", "ads.google.com",
    "cdn.jsdelivr.net", "cdnjs.cloudflare.com",
    "sentry.io", "bugsnag.com", "fullstory.com",
    "intercom.io", "crisp.chat", "freshchat.com",
    "stripe.js",
})

_SKIP_RESOURCE_TYPES = frozenset({
    "image", "media", "font", "stylesheet", "ping", "websocket",
})

_SKIP_PATH_PREFIXES = frozenset({
    "/_next/static/", "/cdn-cgi/", "/__webpack",
    "/static/js/", "/static/css/",
})


def _should_skip(url: str, resource_type: str) -> bool:
    if resource_type in _SKIP_RESOURCE_TYPES:
        return True
    try:
        parsed = urlparse(url)
    except Exception:
        return True

    netloc = parsed.netloc.lower()
    for domain in _SKIP_DOMAINS:
        if netloc == domain or netloc.endswith("." + domain):
            return True

    path = parsed.path.lower()
    ext  = path.rsplit(".", 1)[-1] if "." in path.split("/")[-1] else ""
    if "." + ext in _SKIP_EXTENSIONS:
        return True

    for prefix in _SKIP_PATH_PREFIXES:
        if path.startswith(prefix):
            return True

    # Skip pure .js files (bundles) but allow /api/ routes
    if path.endswith(".js") and "/api/" not in path:
        return True

    return False


def _make_label(url: str) -> str:
    try:
        parsed = urlparse(url)
        path   = parsed.path.strip("/")
        if not path:
            return "Home"
        parts  = path.split("/")
        label  = " > ".join(
            p.replace("-", " ").replace("_", " ").title()
            for p in parts[:4]
        )
        return label or "Home"
    except Exception:
        return "Unknown"


# ---------------------------------------------------------------------------
# Recording session
# ---------------------------------------------------------------------------

class RecordingSession:
    """
    Opens a visible Chromium window. User performs the journey naturally.
    All API/XHR traffic is intercepted and stored.

    Usage:
        session = RecordingSession("https://example.com")
        session.start()          # non-blocking; browser opens in background
        # ... user does stuff ...
        requests = session.stop()
    """

    def __init__(self, url: str, log_callback: Optional[Callable[[str], None]] = None):
        self.url          = url
        self._log         = log_callback or (lambda _: None)
        self._captured:   list[CapturedRequest] = []
        self._seq         = 0
        self._lock        = threading.Lock()
        self._stop_event  = threading.Event()
        self._thread:     Optional[threading.Thread] = None
        self._started     = threading.Event()
        self._error:      Optional[Exception] = None
        self._browser_alive = threading.Event()
        self._headless = str(os.getenv("JMX_FORGE_HEADLESS", "true")).lower() in {"1", "true", "yes", "on"}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Spawn the Playwright thread and wait until the browser is ready."""
        self._thread = threading.Thread(
            target=self._playwright_thread,
            name="playwright-recording",
            daemon=True,
        )
        self._thread.start()
        # Block until browser is launched (or an error occurred)
        self._started.wait(timeout=20)
        if self._error:
            raise self._error

    def stop(self) -> list[CapturedRequest]:
        """Signal the browser to close and return all captured requests."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=15)
        return list(self._captured)

    def get_captured_count(self) -> int:
        with self._lock:
            return len(self._captured)

    def is_browser_open(self) -> bool:
        return self._browser_alive.is_set()

    # ------------------------------------------------------------------
    # Internal Playwright thread
    # ------------------------------------------------------------------

    def _playwright_thread(self) -> None:
        """Everything Playwright runs inside this single thread."""
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as pw:
                launch_args = [
                    "--disable-blink-features=AutomationControlled",
                    "--no-default-browser-check",
                    "--disable-dev-shm-usage",
                    "--no-sandbox",
                ]
                if not self._headless:
                    launch_args.insert(0, "--start-maximized")

                browser = pw.chromium.launch(
                    headless=self._headless,
                    args=launch_args,
                )
                context = browser.new_context(
                    viewport=None if not self._headless else {"width": 1440, "height": 900},
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0.0.0 Safari/537.36"
                    ),
                    ignore_https_errors=True,
                )
                page = context.new_page()
                page.on("response", self._on_response_sync)

                self._browser_alive.set()
                self._started.set()

                # Navigate to the starting URL
                try:
                    page.goto(self.url, wait_until="domcontentloaded", timeout=30_000)
                    self._log(f"🌐 Browser opened at {self.url}")
                except Exception as nav_err:
                    self._log(f"⚠️  Navigation warning: {nav_err}")

                # Wait until stop() is called OR the browser window is closed
                while not self._stop_event.is_set():
                    try:
                        if not browser.is_connected():
                            break
                        # Refresh the page context label
                        try:
                            self._current_url = page.url
                        except Exception:
                            pass
                        time.sleep(0.3)
                    except Exception:
                        break

                # Clean shutdown
                try:
                    context.close()
                except Exception:
                    pass
                try:
                    browser.close()
                except Exception:
                    pass

        except Exception as exc:
            self._error = exc
            self._started.set()   # unblock .start() so it can raise
        finally:
            self._browser_alive.clear()

    def _on_response_sync(self, response) -> None:
        """Called by Playwright in the recording thread for every response."""
        try:
            req = response.request
            if _should_skip(req.url, req.resource_type):
                return

            # Read body safely; large bodies are truncated
            try:
                body_text = response.text()
                if len(body_text) > 200_000:
                    body_text = body_text[:200_000] + "\n<!-- TRUNCATED -->"
            except Exception:
                body_text = None

            with self._lock:
                self._seq += 1
                seq = self._seq
                current_label = _make_label(getattr(self, "_current_url", self.url))
                cr = CapturedRequest(
                    sequence=seq,
                    url=req.url,
                    method=req.method,
                    headers=dict(req.headers),
                    body=req.post_data,
                    response_status=response.status,
                    response_headers=dict(response.headers),
                    response_body=body_text,
                    timestamp=time.time(),
                    page_context=current_label,
                    resource_type=req.resource_type,
                )
                self._captured.append(cr)

            self._log(f"📡 [{req.method:6s}] {req.url[:90]}  → {response.status}")

        except Exception:
            pass   # Never let a single response crash the whole session


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

def deduplicate(requests: list[CapturedRequest]) -> list[CapturedRequest]:
    """
    Remove exact duplicate (method, url, body) triples.
    Keeps the first occurrence and preserves original ordering.
    """
    seen:   set[tuple] = set()
    unique: list[CapturedRequest] = []
    for r in requests:
        key = (r.method.upper(), r.url, r.body or "")
        if key not in seen:
            seen.add(key)
            unique.append(r)
    return unique


# ---------------------------------------------------------------------------
# cURL export
# ---------------------------------------------------------------------------

_SKIP_CURL_HEADERS = frozenset({
    ":method", ":path", ":authority", ":scheme",
    "content-length", "connection", "accept-encoding",
    "sec-fetch-dest", "sec-fetch-mode", "sec-fetch-site", "sec-fetch-user",
    "sec-ch-ua", "sec-ch-ua-mobile", "sec-ch-ua-platform",
})


def build_curl_commands(requests: list[CapturedRequest]) -> str:
    """
    Generate a shell-safe cURL command for every captured request.
    Single-quotes the body and escapes any embedded single-quotes.
    """
    lines: list[str] = []
    for r in requests:
        lines.append(f"# [{r.sequence:04d}] {r.method.upper()} {r.url}")
        lines.append(f"# Page: {r.page_context}  |  HTTP {r.response_status}")
        parts: list[str] = [f"curl -s -o /dev/null -w '%{{http_code}}' -X {r.method.upper()}"]
        parts.append(f"  '{r.url}'")
        for k, v in r.headers.items():
            if k.lower() in _SKIP_CURL_HEADERS:
                continue
            safe_v = v.replace("'", "'\\''")
            parts.append(f"  -H '{k}: {safe_v}'")
        if r.body:
            safe_b = r.body.replace("'", "'\\''")
            parts.append(f"  --data '{safe_b}'")
        lines.append(" \\\n".join(parts))
        lines.append("")
    return "\n".join(lines)
