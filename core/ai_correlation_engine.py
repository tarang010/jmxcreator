from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Optional

from .traffic_capture import CapturedRequest
from .correlation_engine import CSVColumn


@dataclass
class Correlation:
    name: str
    extractor_type: str
    extractor_expression: str
    source_request_seq: int
    source_location: str
    used_in_sequences: list[int]
    sample_value: Optional[str] = None
    confidence: float = 0.0
    reason: str = ""


_TOKEN_KEY_HINTS = {
    "token",
    "session",
    "auth",
    "csrf",
    "jwt",
    "requestid",
    "correlation",
    "nonce",
    "state",
}

_CSV_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r'"(?:username|user_name|email|login|loginId|login_id)"\s*:\s*"([^"]{3,})"', re.I), "username"),
    (re.compile(r'"(?:password|passwd|pass)"\s*:\s*"([^"]{3,})"', re.I), "password"),
    (re.compile(r'"(?:accountNumber|account_number|accountNo)"\s*:\s*"([^"]{3,})"', re.I), "accountNumber"),
    (re.compile(r'"(?:customerId|customer_id|clientId)"\s*:\s*"([^"]{3,})"', re.I), "customerId"),
    (re.compile(r'(?:^|&)(?:username|email|login)=([^&]{3,})', re.I), "username"),
    (re.compile(r'(?:^|&)(?:password|passwd)=([^&]{3,})', re.I), "password"),
]


def run_ai_correlation(all_requests: list[CapturedRequest]) -> tuple[list[Correlation], list[CSVColumn]]:
    candidates: list[dict] = []
    csv_columns: list[CSVColumn] = []
    seen_csv: set[str] = set()

    for i, req in enumerate(all_requests):
        _scan_csv(req.body, csv_columns, seen_csv)

        resp_headers = {k.lower(): v for k, v in (req.response_headers or {}).items()}
        content_type = resp_headers.get("content-type", "")
        resp_body = req.response_body or ""

        if "json" in content_type and resp_body:
            candidates.extend(_extract_json_candidates(resp_body, req.sequence))
        if "html" in content_type and resp_body:
            candidates.extend(_extract_html_candidates(resp_body, req.sequence))

        set_cookie = resp_headers.get("set-cookie", "")
        if set_cookie:
            candidates.extend(_extract_cookie_candidates(set_cookie, req.sequence))

        for hk, hv in resp_headers.items():
            if hk in ("authorization", "x-auth-token", "x-access-token"):
                bm = re.search(r"[Bb]earer\s+(\S+)", hv or "")
                if bm:
                    tok = bm.group(1)
                    candidates.append(
                        {
                            "name": "authToken",
                            "value": tok,
                            "source_seq": req.sequence,
                            "type": "regex",
                            "expr": r"Bearer ([A-Za-z0-9\-_.+/=]+)",
                            "source_location": "header",
                            "reason": "Bearer token detected in response header",
                            "base_score": 0.95,
                        }
                    )

    dedup = _dedup_candidates(candidates)
    correlations: list[Correlation] = []

    for c in dedup:
        used = _find_usage(c["value"], c["source_seq"], all_requests)
        if not used:
            continue
        confidence = _score_candidate(c, used)
        if confidence < 0.45:
            continue
        correlations.append(
            Correlation(
                name=c["name"],
                extractor_type=c["type"],
                extractor_expression=c["expr"],
                source_request_seq=c["source_seq"],
                source_location=c["source_location"],
                used_in_sequences=used,
                sample_value=_truncate(c["value"]),
                confidence=confidence,
                reason=c.get("reason", ""),
            )
        )

    correlations.sort(key=lambda x: (-x.confidence, x.source_request_seq))
    return correlations, csv_columns


def _extract_json_candidates(body: str, seq: int) -> list[dict]:
    out: list[dict] = []
    try:
        data = json.loads(body)
    except Exception:
        return out

    def walk(node, path: list[str]):
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, path + [k])
        elif isinstance(node, list):
            for idx, v in enumerate(node[:10]):
                walk(v, path + [str(idx)])
        else:
            if not isinstance(node, (str, int, float, bool)):
                return
            val = str(node)
            if len(val) < 8:
                return
            key = path[-1].lower() if path else "value"
            var_name = _to_var_name(path[-1] if path else "value")
            score = 0.55
            reason = "Dynamic value found in JSON response"
            if any(h in key for h in _TOKEN_KEY_HINTS):
                score += 0.25
                reason = f"JSON key '{path[-1]}' looks token/session related"
            expr = "$." + ".".join(path)
            out.append(
                {
                    "name": var_name,
                    "value": val,
                    "source_seq": seq,
                    "type": "json_path",
                    "expr": expr,
                    "source_location": "body",
                    "reason": reason,
                    "base_score": min(0.98, score),
                }
            )

    walk(data, [])
    return out


def _extract_html_candidates(body: str, seq: int) -> list[dict]:
    out: list[dict] = []
    patterns = [
        re.compile(r'<input[^>]+type=["\']hidden["\'][^>]+name=["\']([^"\']+)["\'][^>]+value=["\']([^"\']{8,})["\']', re.I),
        re.compile(r'<input[^>]+name=["\']([^"\']+)["\'][^>]+type=["\']hidden["\'][^>]+value=["\']([^"\']{8,})["\']', re.I),
    ]
    for pat in patterns:
        for m in pat.finditer(body):
            field, val = m.group(1), m.group(2)
            out.append(
                {
                    "name": _to_var_name(field),
                    "value": val,
                    "source_seq": seq,
                    "type": "regex",
                    "expr": rf'name="{re.escape(field)}"\s+value="([^"]+)"',
                    "source_location": "body",
                    "reason": f"Hidden input '{field}' found in HTML response",
                    "base_score": 0.72,
                }
            )
    return out


def _extract_cookie_candidates(set_cookie: str, seq: int) -> list[dict]:
    out: list[dict] = []
    for directive in re.split(r",\s*(?=[A-Za-z])", set_cookie):
        m = re.match(r"^([A-Za-z][A-Za-z0-9_\-.]{1,63})=([^;,\s]{8,})", directive.strip())
        if not m:
            continue
        k, v = m.group(1), m.group(2)
        if k.lower() in {"path", "domain", "expires", "max-age", "secure", "httponly", "samesite"}:
            continue
        out.append(
            {
                "name": _to_var_name(k),
                "value": v,
                "source_seq": seq,
                "type": "regex",
                "expr": rf"{re.escape(k)}=([^;,\s]+)",
                "source_location": "header",
                "reason": f"Cookie '{k}' found in Set-Cookie header",
                "base_score": 0.68,
            }
        )
    return out


def _dedup_candidates(cands: list[dict]) -> list[dict]:
    out: list[dict] = []
    seen: set[tuple] = set()
    for c in cands:
        key = (c["name"], c["value"], c["source_seq"])
        if key in seen:
            continue
        seen.add(key)
        out.append(c)
    return out


def _find_usage(value: str, source_seq: int, all_requests: list[CapturedRequest]) -> list[int]:
    found: list[int] = []
    for r in all_requests:
        if r.sequence <= source_seq:
            continue
        if value in (r.url or ""):
            found.append(r.sequence)
            continue
        if r.body and value in r.body:
            found.append(r.sequence)
            continue
        if r.headers and any(value in hv for hv in r.headers.values()):
            found.append(r.sequence)
    return found


def _score_candidate(c: dict, used: list[int]) -> float:
    score = float(c.get("base_score", 0.5))
    usage_boost = min(0.2, 0.03 * len(used))
    score += usage_boost
    val = c.get("value", "")
    if re.search(r"^[A-Za-z0-9\-_.+/=]{16,}$", val):
        score += 0.08
    return max(0.0, min(0.99, score))


def _to_var_name(raw: str) -> str:
    parts = re.split(r"[_\-\s\.]+", raw or "value")
    parts = [p for p in parts if p]
    if not parts:
        return "dynamicValue"
    return parts[0].lower() + "".join(p[:1].upper() + p[1:] for p in parts[1:])


def _scan_csv(body: Optional[str], csv_columns: list[CSVColumn], seen_csv: set[str]) -> None:
    if not body:
        return
    for pattern, col_name in _CSV_PATTERNS:
        if col_name in seen_csv:
            continue
        m = pattern.search(body)
        if m:
            seen_csv.add(col_name)
            csv_columns.append(
                CSVColumn(
                    column_name=col_name,
                    sample_values=[m.group(1)],
                    description=f"Parameterize: {col_name}",
                )
            )


def _truncate(value: str, max_len: int = 80) -> str:
    return value[:max_len] + "..." if len(value) > max_len else value
