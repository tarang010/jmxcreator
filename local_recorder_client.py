from __future__ import annotations

import argparse
import json
import sys
import time

import requests

from core.traffic_capture import RecordingSession


def _to_dict(req):
    return {
        "sequence": req.sequence,
        "url": req.url,
        "method": req.method,
        "headers": req.headers,
        "body": req.body,
        "response_status": req.response_status,
        "response_headers": req.response_headers,
        "response_body": req.response_body,
        "timestamp": req.timestamp,
        "page_context": req.page_context,
        "resource_type": req.resource_type,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Local Recorder Agent for JMX Forge")
    parser.add_argument("--server", required=True, help="Render/base server URL, e.g. https://jmxcreator.onrender.com")
    parser.add_argument("--url", required=True, help="Target app URL to record")
    parser.add_argument("--threads", type=int, default=10)
    parser.add_argument("--ramp-up", type=int, default=60)
    parser.add_argument("--loops", type=int, default=1)
    args = parser.parse_args()

    session = RecordingSession(args.url)
    print("Opening local browser for recording...")
    session.start()
    print("Perform your journey in the opened browser. Press Enter here when done.")
    input()

    captured = session.stop()
    if not captured:
        print("No requests captured. Exiting.")
        return 1

    payload = {
        "captured_requests": [_to_dict(r) for r in captured],
        "num_threads": args.threads,
        "ramp_up": args.ramp_up,
        "loop_count": args.loops,
    }

    endpoint = args.server.rstrip("/") + "/api/generate-from-capture"
    print(f"Uploading capture to {endpoint} ...")
    resp = requests.post(endpoint, json=payload, timeout=120)
    resp.raise_for_status()
    job_id = resp.json()["job_id"]
    print(f"Job created: {job_id}")

    stream_url = args.server.rstrip("/") + f"/api/stream/{job_id}"
    print("Waiting for generation...")
    with requests.get(stream_url, stream=True, timeout=600) as sresp:
        sresp.raise_for_status()
        for line in sresp.iter_lines(decode_unicode=True):
            if not line or not line.startswith("data: "):
                continue
            data = line[6:]
            try:
                evt = json.loads(data)
            except Exception:
                continue
            event = evt.get("event")
            body = evt.get("data") or {}
            if event == "log":
                print(body.get("message", ""))
            elif event == "error":
                print("ERROR:", body.get("message", "unknown"))
                return 1
            elif event == "complete":
                base = args.server.rstrip("/")
                print("Completed successfully")
                print("JMX:", f"{base}/api/download/{job_id}/jmx")
                if body.get("csv_filename"):
                    print("CSV:", f"{base}/api/download/{job_id}/csv")
                print("cURL:", f"{base}/api/download/{job_id}/curl")
                print("Preview:", f"{base}/api/preview/{job_id}")
                return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
