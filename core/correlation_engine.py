"""
Correlation Engine — Production Ready

Scans every captured response and identifies dynamic values that:
  1. Appear in a RESPONSE (JSON body, HTML hidden fields, Set-Cookie header,
     or auth response headers).
  2. Are referenced in at least one SUBSEQUENT REQUEST (URL, body, headers).

Produces:
  - Correlation objects  → JMeter extractors (JSONPath or Regex)
  - CSVColumn objects    → CSV Data Set for parameterisation

Key fixes over original:
  - All string operations guard against None
  - Cookie parser is more precise (first name=value only, no false positives)
  - JSON extraction walks nested paths safely
  - "used in" search skips the request that generated the value
  - Sample values are always strings, truncated safely
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Optional

from .traffic_capture import CapturedRequest


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class Correlation:
    name:                    str
    extractor_type:          str          # "json_path" | "regex" | "cookie"
    extractor_expression:    str
    source_request_seq:      int
    source_location:         str          # "body" | "header"
    used_in_sequences:       list[int]
    sample_value:            Optional[str] = None


@dataclass
class CSVColumn:
    column_name:   str
    sample_values: list[str]
    description:   str


# ---------------------------------------------------------------------------
# Well-known JSON paths to probe for tokens
# ---------------------------------------------------------------------------

# Each entry: (dot-notation path, variable name)
_JSON_TOKEN_PATHS: list[tuple[str, str]] = [
    ("access_token",          "accessToken"),
    ("token",                 "token"),
    ("auth_token",            "authToken"),
    ("id_token",              "idToken"),
    ("refresh_token",         "refreshToken"),
    ("data.token",            "dataToken"),
    ("sessionId",             "sessionId"),
    ("session_id",            "sessionId"),
    ("data.sessionId",        "dataSessionId"),
    ("correlationId",         "correlationId"),
    ("requestId",             "requestId"),
    ("data.id",               "dataId"),
    ("userId",                "userId"),
    ("user.id",               "userId"),
    ("csrfToken",             "csrfToken"),
    ("csrf_token",            "csrfToken"),
    ("_token",                "csrfToken"),
    ("data.access_token",     "accessToken"),
    ("result.token",          "resultToken"),
    ("payload.token",         "payloadToken"),
    ("payload.sessionToken",  "sessionToken"),
]

# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------

# Hidden input fields — name before or after type="hidden"
_HIDDEN_INPUT_PATTERNS = [
    re.compile(
        r'<input[^>]+type=["\']hidden["\'][^>]+name=["\']([^"\']+)["\'][^>]+value=["\']([^"\']{6,})["\']',
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(
        r'<input[^>]+name=["\']([^"\']+)["\'][^>]+type=["\']hidden["\'][^>]+value=["\']([^"\']{6,})["\']',
        re.IGNORECASE | re.DOTALL,
    ),
    # value before name (some frameworks)
    re.compile(
        r'<input[^>]+value=["\']([^"\']{6,})["\'][^>]+name=["\']([^"\']+)["\'][^>]+type=["\']hidden["\']',
        re.IGNORECASE | re.DOTALL,
    ),
]

# First name=value pair from Set-Cookie header
_COOKIE_FIRST_PAIR_RE = re.compile(r'^([A-Za-z][A-Za-z0-9_\-\.]{1,63})=([^;,\s]{6,})')

# Patterns that identify user-supplied CSV candidates in request bodies
_CSV_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r'"(?:username|user_name|email|login|loginId|login_id)"\s*:\s*"([^"]{3,})"', re.I), "username"),
    (re.compile(r'"(?:password|passwd|pass)"\s*:\s*"([^"]{3,})"',                             re.I), "password"),
    (re.compile(r'"(?:policyNumber|policy_number|policyNo)"\s*:\s*"([^"]{3,})"',              re.I), "policyNumber"),
    (re.compile(r'"(?:accountNumber|account_number|accountNo)"\s*:\s*"([^"]{3,})"',           re.I), "accountNumber"),
    (re.compile(r'"(?:customerId|customer_id|clientId)"\s*:\s*"([^"]{3,})"',                  re.I), "customerId"),
    (re.compile(r'"(?:searchQuery|query|searchTerm|search_query)"\s*:\s*"([^"]{3,})"',        re.I), "searchQuery"),
    (re.compile(r'"(?:phoneNumber|phone|mobile)"\s*:\s*"([^"]{3,})"',                         re.I), "phoneNumber"),
    (re.compile(r'"(?:orderId|order_id)"\s*:\s*"([^"]{3,})"',                                 re.I), "orderId"),
    # Form-encoded variants
    (re.compile(r'(?:^|&)(?:username|email|login)=([^&]{3,})',                                re.I), "username"),
    (re.compile(r'(?:^|&)(?:password|passwd)=([^&]{3,})',                                     re.I), "password"),
]

# Cookie names automatically managed by JMeter's Cookie Manager — skip these
_SKIP_COOKIE_NAMES: frozenset[str] = frozenset({
    "path", "domain", "expires", "max-age", "secure", "httponly", "samesite",
    "_ga", "_gid", "_gat", "ga", "gtm", "__utma", "__utmb", "__utmz",
})


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run_correlation(
    all_requests: list[CapturedRequest],
) -> tuple[list[Correlation], list[CSVColumn]]:
    """
    Return (correlations, csv_columns) found across all_requests.
    """
    correlations: list[Correlation] = []
    csv_columns:  list[CSVColumn]   = []
    seen_vars:    set[str]          = set()
    seen_csv:     set[str]          = set()

    for idx, req in enumerate(all_requests):
        resp_body    = req.response_body or ""
        resp_headers = {k.lower(): v for k, v in (req.response_headers or {}).items()}
        content_type = resp_headers.get("content-type", "")

        # Requests that come AFTER this one — the only ones that could use extracted values
        later = all_requests[idx + 1:]
        if not later:
            # Still scan for CSV candidates even on the last request
            _scan_csv_candidates(req.body, csv_columns, seen_csv)
            continue

        # ── 1. JSON response body ────────────────────────────────────────────
        if resp_body and "json" in content_type:
            _scan_json_body(resp_body, req.sequence, later, correlations, seen_vars)

        # ── 2. HTML hidden input fields ──────────────────────────────────────
        if resp_body and "html" in content_type:
            _scan_html_hidden(resp_body, req.sequence, later, correlations, seen_vars)

        # ── 3. Set-Cookie response header ────────────────────────────────────
        set_cookie = resp_headers.get("set-cookie", "")
        if set_cookie:
            _scan_set_cookie(set_cookie, req.sequence, later, correlations, seen_vars)

        # ── 4. Bearer token in response Authorization / X-Auth-Token header ─
        _scan_auth_header(resp_headers, req.sequence, later, correlations, seen_vars)

        # ── 5. CSV candidates from request body ──────────────────────────────
        _scan_csv_candidates(req.body, csv_columns, seen_csv)

    return correlations, csv_columns


# ---------------------------------------------------------------------------
# Sub-scanners
# ---------------------------------------------------------------------------

def _scan_json_body(
    body:          str,
    seq:           int,
    later:         list[CapturedRequest],
    correlations:  list[Correlation],
    seen_vars:     set[str],
) -> None:
    try:
        data = json.loads(body)
    except Exception:
        return

    for dot_path, var_name in _JSON_TOKEN_PATHS:
        if var_name in seen_vars:
            continue
        value = _json_walk(data, dot_path.split("."))
        if not value or len(str(value)) < 8:
            continue
        str_val = str(value)
        used    = _find_usage(str_val, later)
        if used:
            seen_vars.add(var_name)
            correlations.append(Correlation(
                name=var_name,
                extractor_type="json_path",
                extractor_expression="$." + dot_path,
                source_request_seq=seq,
                source_location="body",
                used_in_sequences=used,
                sample_value=_truncate(str_val),
            ))


def _scan_html_hidden(
    body:         str,
    seq:          int,
    later:        list[CapturedRequest],
    correlations: list[Correlation],
    seen_vars:    set[str],
) -> None:
    for pattern in _HIDDEN_INPUT_PATTERNS:
        for m in pattern.finditer(body):
            # Group order differs for the third pattern variant
            if pattern.pattern.startswith(r'<input[^>]+value'):
                field_val  = m.group(1)
                field_name = m.group(2)
            else:
                field_name = m.group(1)
                field_val  = m.group(2)

            var_name = _camel(field_name)
            if var_name in seen_vars or len(field_val) < 8:
                continue

            used = _find_usage(field_val, later)
            if used:
                seen_vars.add(var_name)
                # Build a precise regex that captures the value
                escaped = re.escape(field_name)
                expr    = rf'name="{escaped}"\s+value="([^"]+)"'
                correlations.append(Correlation(
                    name=var_name,
                    extractor_type="regex",
                    extractor_expression=expr,
                    source_request_seq=seq,
                    source_location="body",
                    used_in_sequences=used,
                    sample_value=_truncate(field_val),
                ))


def _scan_set_cookie(
    set_cookie:   str,
    seq:          int,
    later:        list[CapturedRequest],
    correlations: list[Correlation],
    seen_vars:    set[str],
) -> None:
    # Set-Cookie may contain multiple cookies separated by commas
    # We parse only the first name=value of each cookie directive
    for directive in re.split(r",\s*(?=[A-Za-z])", set_cookie):
        m = _COOKIE_FIRST_PAIR_RE.match(directive.strip())
        if not m:
            continue
        cname    = m.group(1)
        cval     = m.group(2)
        var_name = _camel(cname)

        if cname.lower() in _SKIP_COOKIE_NAMES or var_name in seen_vars:
            continue
        if len(cval) < 8:
            continue

        used = _find_usage(cval, later)
        if used:
            seen_vars.add(var_name)
            expr = rf"{re.escape(cname)}=([^;,\s]+)"
            correlations.append(Correlation(
                name=var_name,
                extractor_type="regex",
                extractor_expression=expr,
                source_request_seq=seq,
                source_location="header",
                used_in_sequences=used,
                sample_value=_truncate(cval),
            ))


def _scan_auth_header(
    resp_headers: dict[str, str],
    seq:          int,
    later:        list[CapturedRequest],
    correlations: list[Correlation],
    seen_vars:    set[str],
) -> None:
    if "authToken" in seen_vars:
        return
    for header_name in ("authorization", "x-auth-token", "x-access-token"):
        value = resp_headers.get(header_name, "")
        if not value:
            continue
        bm = re.search(r"[Bb]earer\s+(\S+)", value)
        if bm:
            token = bm.group(1)
            used  = _find_usage(token, later)
            if used:
                seen_vars.add("authToken")
                correlations.append(Correlation(
                    name="authToken",
                    extractor_type="regex",
                    extractor_expression=r"Bearer ([A-Za-z0-9\-_.+/=]+)",
                    source_request_seq=seq,
                    source_location="header",
                    used_in_sequences=used,
                    sample_value=_truncate(token),
                ))
                return


def _scan_csv_candidates(
    body:        Optional[str],
    csv_columns: list[CSVColumn],
    seen_csv:    set[str],
) -> None:
    if not body:
        return
    for pattern, col_name in _CSV_PATTERNS:
        if col_name in seen_csv:
            continue
        m = pattern.search(body)
        if m:
            seen_csv.add(col_name)
            csv_columns.append(CSVColumn(
                column_name=col_name,
                sample_values=[m.group(1)],
                description=f"Parameterise: {col_name}",
            ))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _json_walk(data: object, parts: list[str]) -> Optional[str]:
    """Safely walk a nested dict/list structure via a list of keys."""
    cur = data
    for part in parts:
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list) and part.isdigit():
            idx = int(part)
            cur = cur[idx] if idx < len(cur) else None
        else:
            return None
        if cur is None:
            return None
    if isinstance(cur, (str, int, float, bool)):
        return str(cur)
    return None


def _find_usage(value: str, requests: list[CapturedRequest]) -> list[int]:
    """
    Return sequence numbers of later requests that reference `value`
    somewhere in their URL, body, or headers.
    Minimum value length 8 to avoid false positives.
    """
    if not value or len(value) < 8:
        return []
    found: list[int] = []
    for r in requests:
        if value in r.url:
            found.append(r.sequence)
        elif r.body and value in r.body:
            found.append(r.sequence)
        elif r.headers and any(value in v for v in r.headers.values()):
            found.append(r.sequence)
    return found


def _camel(name: str) -> str:
    """Convert snake_case / kebab-case to camelCase."""
    parts = re.split(r"[_\-\s\.]+", name)
    if not parts:
        return name
    return parts[0].lower() + "".join(p.title() for p in parts[1:] if p)


def _truncate(value: str, max_len: int = 80) -> str:
    return value[:max_len] + "…" if len(value) > max_len else value

