"""
Transaction Grouper — Production Ready
Groups captured requests into JMeter TransactionControllers.

Strategy (no AI required):
  - Each unique (method, path-template) pair becomes ONE TransactionController.
  - Path IDs (UUIDs, numeric IDs) are normalised to :id so that
    GET /orders/123 and GET /orders/456 map to the same transaction.
  - Duplicate transaction names get a numeric suffix.
  - Think time defaults to 500 ms; first request of a new host gets 1 000 ms.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

from .traffic_capture import CapturedRequest


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Transaction:
    name:        str
    description: str
    requests:    list[CapturedRequest]
    think_time:  int = 500   # milliseconds


# ---------------------------------------------------------------------------
# Path normalisation helpers
# ---------------------------------------------------------------------------

# Patterns that look like identifiers embedded in path segments
_UUID_RE   = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
_NUM_RE    = re.compile(r"^\d+$")
_HASH_RE   = re.compile(r"^[0-9a-f]{16,}$", re.I)


def _normalise_path(path: str) -> str:
    """
    Replace dynamic segments (IDs, UUIDs, hashes) with :id so that
    requests to the same endpoint with different IDs are grouped together.
    """
    path = _UUID_RE.sub(":id", path)
    segments = path.split("/")
    cleaned  = []
    for seg in segments:
        if _NUM_RE.match(seg) or _HASH_RE.match(seg):
            cleaned.append(":id")
        else:
            cleaned.append(seg)
    return "/".join(cleaned)


def _tx_name_from_request(req: CapturedRequest) -> str:
    """
    Build a human-readable transaction name:
      GET /api/v2/orders/:id
    Keeps at most 4 path segments for readability; appends query param *keys*
    (not values) when present.
    """
    try:
        parsed = urlparse(req.url)
    except Exception:
        return f"{req.method.upper()} /"

    path = _normalise_path(parsed.path.rstrip("/") or "/")

    # Truncate very deep paths — keep last 4 segments
    parts = [p for p in path.split("/") if p]
    if len(parts) > 4:
        path = "/" + "/".join(parts[-4:])
    elif not parts:
        path = "/"
    else:
        path = "/" + "/".join(parts)

    # Append query param *names* only (max 3)
    if parsed.query:
        keys = [kv.split("=")[0] for kv in parsed.query.split("&") if kv]
        if keys:
            path += "?" + "&".join(keys[:3])

    return f"{req.method.upper()} {path}"


# ---------------------------------------------------------------------------
# Main grouper
# ---------------------------------------------------------------------------

def group_into_transactions(
    requests:          list[CapturedRequest],
    app_name:          str,
    progress_callback: object = None,
) -> tuple[list[Transaction], str]:
    """
    One CapturedRequest → one TransactionController.
    Returns (transactions, journey_summary_string).
    """

    def log(msg: str) -> None:
        if progress_callback:
            try:
                progress_callback(msg)
            except Exception:
                pass

    log(f"📋 Building transaction controllers for {len(requests)} request(s)…")

    transactions: list[Transaction] = []
    seen_names:   dict[str, int]   = {}

    for req in requests:
        base_name = _tx_name_from_request(req)

        # Deduplicate names with a counter suffix
        if base_name in seen_names:
            seen_names[base_name] += 1
            name = f"{base_name} ({seen_names[base_name]})"
        else:
            seen_names[base_name] = 1
            name = base_name

        desc = (
            f"URL: {req.url} | "
            f"Status: {req.response_status} | "
            f"Page: {req.page_context}"
        )

        transactions.append(
            Transaction(
                name=name,
                description=desc,
                requests=[req],
                think_time=500,
            )
        )

    log(f"✅ Created {len(transactions)} transaction controller(s)")

    hosts = {urlparse(r.url).netloc for r in requests if r.url}
    journey_summary = (
        f"{app_name} — {len(transactions)} transaction(s) across "
        f"{len(hosts)} host(s)"
    )
    return transactions, journey_summary