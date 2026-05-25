from __future__ import annotations

import csv
import io
import xml.etree.ElementTree as ET
from datetime import datetime, timezone


def analyze_load_test(jtl_bytes: bytes, xml_bytes: bytes) -> dict:
    samples = _parse_jtl(jtl_bytes)
    test_meta = _parse_testplan(xml_bytes)
    if not samples:
        return {"error": "No samples found in JTL file"}

    elapsed = [s["elapsed"] for s in samples]
    start_ts = min(s["timestamp"] for s in samples)
    end_ts = max(s["timestamp"] for s in samples)
    duration_sec = max(1.0, (end_ts - start_ts) / 1000.0)

    throughput = len(samples) / duration_sec
    error_count = sum(1 for s in samples if not s["success"])
    error_pct = (error_count / len(samples)) * 100.0

    users_series = _users_by_minute(samples)
    peak = max(users_series, key=lambda x: x["users"]) if users_series else {"time": None, "users": 0}

    return {
        "summary": {
            "samples": len(samples),
            "errors": error_count,
            "error_percent": round(error_pct, 3),
            "start_time": _fmt_ms(start_ts),
            "end_time": _fmt_ms(end_ts),
            "duration_seconds": round(duration_sec, 2),
            "throughput_rps": round(throughput, 3),
            "avg_response_ms": round(sum(elapsed) / len(elapsed), 2),
            "min_response_ms": min(elapsed),
            "max_response_ms": max(elapsed),
            "p50_ms": _percentile(elapsed, 50),
            "p80_ms": _percentile(elapsed, 80),
            "p90_ms": _percentile(elapsed, 90),
            "p95_ms": _percentile(elapsed, 95),
            "p99_ms": _percentile(elapsed, 99),
        },
        "peak_window": {
            "peak_started_at": peak.get("time"),
            "peak_users": peak.get("users", 0),
        },
        "timeline": users_series,
        "labels": _by_label(samples),
        "test_plan": test_meta,
    }


def _parse_jtl(raw: bytes) -> list[dict]:
    txt = raw.decode("utf-8", errors="replace").strip()
    if not txt:
        return []
    if txt.startswith("<"):
        return _parse_jtl_xml(txt)
    return _parse_jtl_csv(txt)


def _parse_jtl_xml(txt: str) -> list[dict]:
    root = ET.fromstring(txt)
    out: list[dict] = []
    for node in root.findall(".//httpSample") + root.findall(".//sample"):
        ts = int(node.attrib.get("ts", "0") or 0)
        t = int(node.attrib.get("t", "0") or 0)
        s = str(node.attrib.get("s", "true")).lower() == "true"
        label = node.attrib.get("lb", "UNKNOWN")
        users = int(node.attrib.get("allThreads", "0") or 0)
        out.append({"timestamp": ts, "elapsed": t, "success": s, "label": label, "users": users})
    return [x for x in out if x["timestamp"] > 0]


def _parse_jtl_csv(txt: str) -> list[dict]:
    out: list[dict] = []
    reader = csv.DictReader(io.StringIO(txt))
    for r in reader:
        try:
            ts = int(float(r.get("timeStamp", "0")))
            t = int(float(r.get("elapsed", "0")))
            success = str(r.get("success", "true")).lower() == "true"
            label = r.get("label", "UNKNOWN")
            users = int(float(r.get("allThreads", r.get("grpThreads", "0")) or 0))
            out.append({"timestamp": ts, "elapsed": t, "success": success, "label": label, "users": users})
        except Exception:
            continue
    return [x for x in out if x["timestamp"] > 0]


def _parse_testplan(raw: bytes) -> dict:
    txt = raw.decode("utf-8", errors="replace").strip()
    if not txt or not txt.startswith("<"):
        return {}
    try:
        root = ET.fromstring(txt)
    except Exception:
        return {}

    tx = txt.count("<TransactionController")
    samplers = txt.count("<HTTPSamplerProxy")
    threads = []
    for tg in root.findall(".//ThreadGroup"):
        num = tg.find(".//stringProp[@name='ThreadGroup.num_threads']")
        ramp = tg.find(".//stringProp[@name='ThreadGroup.ramp_time']")
        threads.append(
            {
                "name": tg.attrib.get("testname", "Thread Group"),
                "num_threads": int((num.text or "0") if num is not None else 0),
                "ramp_up": int((ramp.text or "0") if ramp is not None else 0),
            }
        )

    return {"transaction_controllers": tx, "samplers": samplers, "thread_groups": threads}


def _users_by_minute(samples: list[dict]) -> list[dict]:
    buckets: dict[int, list[int]] = {}
    for s in samples:
        minute = int(s["timestamp"] // 60000)
        buckets.setdefault(minute, []).append(s.get("users", 0))

    out = []
    for m in sorted(buckets.keys()):
        vals = buckets[m]
        out.append(
            {
                "time": _fmt_ms(m * 60000),
                "users": int(round(sum(vals) / max(1, len(vals)))),
                "samples": len(vals),
            }
        )
    return out


def _by_label(samples: list[dict]) -> list[dict]:
    grouped: dict[str, list[int]] = {}
    errors: dict[str, int] = {}
    for s in samples:
        lb = s["label"]
        grouped.setdefault(lb, []).append(s["elapsed"])
        if not s["success"]:
            errors[lb] = errors.get(lb, 0) + 1

    out = []
    for lb, vals in grouped.items():
        out.append(
            {
                "label": lb,
                "samples": len(vals),
                "errors": errors.get(lb, 0),
                "error_percent": round((errors.get(lb, 0) / len(vals)) * 100.0, 3),
                "avg_ms": round(sum(vals) / len(vals), 2),
                "p90_ms": _percentile(vals, 90),
                "p95_ms": _percentile(vals, 95),
                "p99_ms": _percentile(vals, 99),
            }
        )
    out.sort(key=lambda x: x["samples"], reverse=True)
    return out


def _percentile(values: list[int], pct: int) -> float:
    if not values:
        return 0.0
    vals = sorted(values)
    k = (len(vals) - 1) * (pct / 100.0)
    f = int(k)
    c = min(f + 1, len(vals) - 1)
    if f == c:
        return float(vals[f])
    d = k - f
    return round(vals[f] * (1.0 - d) + vals[c] * d, 2)


def _fmt_ms(ms: int) -> str:
    dt = datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)
    return dt.isoformat()
