from __future__ import annotations

import json
import logging
import os
import queue
import re
import sys
import threading
import time
import traceback
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime
from urllib.parse import urlparse

from flask import Flask, Response, jsonify, render_template, request, send_file

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

from core.traffic_capture import CapturedRequest, RecordingSession, deduplicate, build_curl_commands
from core.transaction_grouper import group_into_transactions
from core.ai_correlation_engine import run_ai_correlation
from core.jmx_generator import generate_jmx, generate_csv
from core.results_dashboard import analyze_load_test

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("jmxforge")

app = Flask(
    __name__,
    template_folder=os.path.join(_HERE, "ui", "templates"),
    static_folder=os.path.join(_HERE, "ui", "static"),
)
app.secret_key = os.urandom(32)

OUTPUT_DIR = os.path.join(_HERE, "output")
os.makedirs(OUTPUT_DIR, exist_ok=True)
RETENTION_SECONDS = int(os.getenv("JMX_FORGE_RETENTION_SECONDS", "3600"))

_sessions: dict[str, RecordingSession] = {}
_session_meta: dict[str, dict] = {}
_jobs: dict[str, dict] = {}
_job_queues: dict[str, queue.Queue] = {}
_STATE_LOCK = threading.Lock()
_CLEANUP_STARTED = False


@app.route("/")
def index():
    _ensure_cleanup_thread()
    return render_template("index.html")


@app.route("/api/health")
def health():
    with _STATE_LOCK:
        return jsonify(
            {
                "status": "ok",
                "time": datetime.utcnow().isoformat() + "Z",
                "active_sessions": len(_sessions),
                "jobs_total": len(_jobs),
            }
        )


@app.route("/api/start-recording", methods=["POST"])
def start_recording():
    _ensure_cleanup_thread()
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"error": "URL is required"}), 400
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    session_id = str(uuid.uuid4())

    def _launch():
        try:
            session = RecordingSession(url)
            with _STATE_LOCK:
                _sessions[session_id] = session
                _session_meta[session_id] = {"url": url, "created_at": time.time()}
            session.start()
        except Exception as exc:
            log.error("Browser launch failed for %s: %s", session_id, exc)

    threading.Thread(target=_launch, name=f"session-{session_id[:8]}", daemon=True).start()

    deadline = time.time() + 3.0
    while time.time() < deadline:
        with _STATE_LOCK:
            if session_id in _sessions:
                break
        time.sleep(0.1)

    return jsonify({"session_id": session_id, "url": url})


@app.route("/api/session-status/<session_id>")
def session_status(session_id: str):
    with _STATE_LOCK:
        session = _sessions.get(session_id)
    if not session:
        return jsonify({"error": "Session not found"}), 404
    return jsonify({"captured_count": session.get_captured_count(), "browser_open": session.is_browser_open()})


@app.route("/api/generate", methods=["POST"])
def generate():
    data = request.get_json(silent=True) or {}
    session_id = (data.get("session_id") or "").strip()
    num_threads = _coerce_int(data.get("num_threads"), 10, 1, 10000)
    ramp_up = _coerce_int(data.get("ramp_up"), 60, 0, 36000)
    loop_count = _coerce_int(data.get("loop_count"), 1, 1, 1000000)

    with _STATE_LOCK:
        session = _sessions.get(session_id)

    if not session:
        return jsonify({"error": "Recording session not found. Start a journey first."}), 404

    job_id = str(uuid.uuid4())
    q = queue.Queue()

    with _STATE_LOCK:
        _jobs[job_id] = {
            "status": "running",
            "session_id": session_id,
            "created_at": time.time(),
            "num_threads": num_threads,
            "ramp_up": ramp_up,
            "loop_count": loop_count,
        }
        _job_queues[job_id] = q

    threading.Thread(
        target=_run_generation,
        args=(job_id, session, num_threads, ramp_up, loop_count),
        name=f"gen-{job_id[:8]}",
        daemon=True,
    ).start()

    return jsonify({"job_id": job_id})


@app.route("/api/generate-from-capture", methods=["POST"])
def generate_from_capture():
    data = request.get_json(silent=True) or {}
    raw_requests = data.get("captured_requests") or []
    if not isinstance(raw_requests, list) or not raw_requests:
        return jsonify({"error": "captured_requests must be a non-empty list"}), 400

    num_threads = _coerce_int(data.get("num_threads"), 10, 1, 10000)
    ramp_up = _coerce_int(data.get("ramp_up"), 60, 0, 36000)
    loop_count = _coerce_int(data.get("loop_count"), 1, 1, 1000000)

    converted: list[CapturedRequest] = []
    for item in raw_requests:
        try:
            converted.append(
                CapturedRequest(
                    sequence=int(item.get("sequence", len(converted) + 1)),
                    url=str(item.get("url") or ""),
                    method=str(item.get("method") or "GET"),
                    headers=dict(item.get("headers") or {}),
                    body=item.get("body"),
                    response_status=int(item.get("response_status") or 0),
                    response_headers=dict(item.get("response_headers") or {}),
                    response_body=item.get("response_body"),
                    timestamp=float(item.get("timestamp") or 0.0),
                    page_context=str(item.get("page_context") or "Captured"),
                    resource_type=str(item.get("resource_type") or "xhr"),
                )
            )
        except Exception:
            continue
    converted = [r for r in converted if r.url]
    if not converted:
        return jsonify({"error": "No valid requests in captured_requests"}), 400

    job_id = str(uuid.uuid4())
    q = queue.Queue()
    with _STATE_LOCK:
        _jobs[job_id] = {
            "status": "running",
            "created_at": time.time(),
            "source": "uploaded_capture",
            "num_threads": num_threads,
            "ramp_up": ramp_up,
            "loop_count": loop_count,
        }
        _job_queues[job_id] = q

    threading.Thread(
        target=_run_generation_from_requests,
        args=(job_id, converted, num_threads, ramp_up, loop_count),
        name=f"gen-upload-{job_id[:8]}",
        daemon=True,
    ).start()
    return jsonify({"job_id": job_id})


@app.route("/api/stream/<job_id>")
def stream(job_id: str):
    with _STATE_LOCK:
        q = _job_queues.get(job_id)
    if q is None:
        return jsonify({"error": "Job not found"}), 404

    def event_generator():
        while True:
            try:
                item = q.get(timeout=30)
            except queue.Empty:
                yield ": keep-alive\n\n"
                continue

            if item is None:
                yield 'data: {"event":"done","data":{}}\n\n'
                break

            yield f"data: {json.dumps(item)}\n\n"

    return Response(
        event_generator(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


@app.route("/api/jobs")
def list_jobs():
    with _STATE_LOCK:
        jobs = []
        for job_id, meta in _jobs.items():
            jobs.append(
                {
                    "job_id": job_id,
                    "status": meta.get("status"),
                    "app_name": meta.get("app_name"),
                    "request_count": meta.get("request_count"),
                    "transaction_count": meta.get("transaction_count"),
                    "correlation_count": meta.get("correlation_count"),
                    "created_at": meta.get("created_at"),
                    "completed_at": meta.get("completed_at"),
                }
            )
    jobs.sort(key=lambda x: x.get("created_at") or 0, reverse=True)
    return jsonify({"jobs": jobs})


@app.route("/api/download/<job_id>/<file_type>")
def download(job_id: str, file_type: str):
    with _STATE_LOCK:
        job = _jobs.get(job_id)

    if not job:
        return jsonify({"error": "Job not found"}), 404
    if job.get("status") != "complete":
        return jsonify({"error": "Job not complete yet"}), 404

    path_key = {"jmx": "jmx_path", "csv": "csv_path", "curl": "curl_path"}.get(file_type)
    if not path_key:
        return jsonify({"error": f"Unknown file type: {file_type}"}), 400

    path = job.get(path_key)
    if not path or not os.path.exists(path):
        return jsonify({"error": "File not found on disk"}), 404

    return send_file(path, as_attachment=True, download_name=os.path.basename(path))


@app.route("/api/preview/<job_id>")
def preview(job_id: str):
    with _STATE_LOCK:
        job = _jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    if job.get("status") != "complete":
        return jsonify({"error": "Job not complete yet"}), 404
    jmx_path = job.get("jmx_path")
    if not jmx_path or not os.path.exists(jmx_path):
        return jsonify({"error": "JMX file not found"}), 404
    with open(jmx_path, "r", encoding="utf-8") as fh:
        content = fh.read()
    return jsonify({"content": content, "validation": job.get("validation")})


@app.route("/api/analyze-results", methods=["POST"])
def analyze_results():
    jtl = request.files.get("jtl_file")
    xmlf = request.files.get("xml_file")
    if not jtl or not xmlf:
        return jsonify({"error": "Both jtl_file and xml_file are required"}), 400
    try:
        result = analyze_load_test(jtl.read(), xmlf.read())
        if result.get("error"):
            return jsonify(result), 400
        return jsonify(result)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


def _run_generation(job_id: str, session: RecordingSession, num_threads: int, ramp_up: int, loop_count: int) -> None:
    with _STATE_LOCK:
        q = _job_queues[job_id]

    def emit(event: str, data: dict) -> None:
        q.put({"event": event, "data": data})

    def logmsg(msg: str, level: str = "") -> None:
        emit("log", {"message": msg, "level": level})
        log.info("job=%s %s", job_id[:8], msg)

    try:
        emit("phase", {"phase": "capture", "message": "Closing browser and collecting captured requests..."})
        raw_requests = session.stop()
        logmsg(f"Raw captured: {len(raw_requests)} request(s)")

        with _STATE_LOCK:
            sid = _jobs[job_id].get("session_id")
            _sessions.pop(sid, None)
            _session_meta.pop(sid, None)

        if not raw_requests:
            emit("error", {"message": "No requests were captured. Perform actions before generation."})
            with _STATE_LOCK:
                _jobs[job_id]["status"] = "failed"
            return

        requests = deduplicate(raw_requests)
        logmsg(f"After deduplication: {len(requests)} unique request(s)")

        app_name = _derive_app_name(requests)
        safe_name = _safe_filename(app_name)
        logmsg(f"Application: {app_name}")

        emit("phase", {"phase": "grouping", "message": "Building transaction controllers..."})
        transactions, journey_summary = group_into_transactions(requests, app_name=app_name, progress_callback=logmsg)
        emit(
            "transactions",
            {
                "transactions": [
                    {"name": tx.name, "description": tx.description, "request_count": len(tx.requests)} for tx in transactions
                ],
                "journey_summary": journey_summary,
            },
        )

        emit("phase", {"phase": "correlation", "message": "Running AI correlation engine..."})
        correlations, csv_columns = run_ai_correlation(requests)
        logmsg(f"Correlations: {len(correlations)} | CSV fields: {len(csv_columns)}")

        emit(
            "correlations",
            {
                "correlations": [
                    {
                        "name": c.name,
                        "type": c.extractor_type,
                        "expression": c.extractor_expression,
                        "source_seq": c.source_request_seq,
                        "usage_count": len(c.used_in_sequences),
                        "sample": c.sample_value,
                        "confidence": c.confidence,
                        "reason": c.reason,
                    }
                    for c in correlations
                ],
                "csv_columns": [{"name": c.column_name, "description": c.description} for c in csv_columns],
            },
        )

        emit("phase", {"phase": "generating", "message": "Generating JMX and output files..."})
        slug = f"{safe_name}_{job_id[:8]}"
        jmx_path = os.path.join(OUTPUT_DIR, f"{slug}.jmx")
        csv_path = os.path.join(OUTPUT_DIR, f"{slug}_data.csv")
        curl_path = os.path.join(OUTPUT_DIR, f"{slug}_curls.txt")

        generate_jmx(
            app_name=app_name,
            transactions=transactions,
            correlations=correlations,
            csv_columns=csv_columns,
            user_journey_summary=journey_summary,
            output_path=jmx_path,
            num_threads=num_threads,
            ramp_up=ramp_up,
            loop_count=loop_count,
        )
        validation = _validate_jmx(jmx_path)
        if not validation.get("valid"):
            raise RuntimeError(f"Generated JMX validation failed: {validation.get('error', 'invalid structure')}")

        if csv_columns:
            generate_csv(csv_columns, csv_path)

        with open(curl_path, "w", encoding="utf-8") as fh:
            fh.write(build_curl_commands(requests))

        result = {
            "app_name": app_name,
            "jmx_filename": os.path.basename(jmx_path),
            "csv_filename": os.path.basename(csv_path) if csv_columns else None,
            "curl_filename": os.path.basename(curl_path),
            "transaction_count": len(transactions),
            "correlation_count": len(correlations),
            "request_count": len(requests),
            "job_id": job_id,
            "validation": validation,
        }

        with _STATE_LOCK:
            _jobs[job_id].update(
                {
                    "status": "complete",
                    "completed_at": time.time(),
                    "jmx_path": jmx_path,
                    "csv_path": csv_path if csv_columns else None,
                    "curl_path": curl_path,
                    "validation": validation,
                    **result,
                }
            )

        emit("complete", result)

    except Exception as exc:
        err_detail = traceback.format_exc()
        log.error("Generation failed for job %s:\n%s", job_id[:8], err_detail)
        emit("error", {"message": str(exc), "detail": err_detail})
        with _STATE_LOCK:
            _jobs[job_id]["status"] = "failed"

    finally:
        q.put(None)


def _run_generation_from_requests(
    job_id: str, raw_requests: list[CapturedRequest], num_threads: int, ramp_up: int, loop_count: int
) -> None:
    with _STATE_LOCK:
        q = _job_queues[job_id]

    def emit(event: str, data: dict) -> None:
        q.put({"event": event, "data": data})

    try:
        emit("phase", {"phase": "capture", "message": "Using uploaded local capture..."})
        requests = deduplicate(raw_requests)
        app_name = _derive_app_name(requests)
        safe_name = _safe_filename(app_name)

        emit("phase", {"phase": "grouping", "message": "Building transaction controllers..."})
        transactions, journey_summary = group_into_transactions(requests, app_name=app_name)
        emit(
            "transactions",
            {
                "transactions": [
                    {"name": tx.name, "description": tx.description, "request_count": len(tx.requests)} for tx in transactions
                ],
                "journey_summary": journey_summary,
            },
        )

        emit("phase", {"phase": "correlation", "message": "Running AI correlation engine..."})
        correlations, csv_columns = run_ai_correlation(requests)
        emit(
            "correlations",
            {
                "correlations": [
                    {
                        "name": c.name,
                        "type": c.extractor_type,
                        "expression": c.extractor_expression,
                        "source_seq": c.source_request_seq,
                        "usage_count": len(c.used_in_sequences),
                        "sample": c.sample_value,
                        "confidence": c.confidence,
                        "reason": c.reason,
                    }
                    for c in correlations
                ],
                "csv_columns": [{"name": c.column_name, "description": c.description} for c in csv_columns],
            },
        )

        emit("phase", {"phase": "generating", "message": "Generating JMX and output files..."})
        slug = f"{safe_name}_{job_id[:8]}"
        jmx_path = os.path.join(OUTPUT_DIR, f"{slug}.jmx")
        csv_path = os.path.join(OUTPUT_DIR, f"{slug}_data.csv")
        curl_path = os.path.join(OUTPUT_DIR, f"{slug}_curls.txt")

        generate_jmx(
            app_name=app_name,
            transactions=transactions,
            correlations=correlations,
            csv_columns=csv_columns,
            user_journey_summary=journey_summary,
            output_path=jmx_path,
            num_threads=num_threads,
            ramp_up=ramp_up,
            loop_count=loop_count,
        )
        validation = _validate_jmx(jmx_path)
        if not validation.get("valid"):
            raise RuntimeError(f"Generated JMX validation failed: {validation.get('error', 'invalid structure')}")
        if csv_columns:
            generate_csv(csv_columns, csv_path)
        with open(curl_path, "w", encoding="utf-8") as fh:
            fh.write(build_curl_commands(requests))

        result = {
            "app_name": app_name,
            "jmx_filename": os.path.basename(jmx_path),
            "csv_filename": os.path.basename(csv_path) if csv_columns else None,
            "curl_filename": os.path.basename(curl_path),
            "transaction_count": len(transactions),
            "correlation_count": len(correlations),
            "request_count": len(requests),
            "job_id": job_id,
            "validation": validation,
        }
        with _STATE_LOCK:
            _jobs[job_id].update(
                {
                    "status": "complete",
                    "completed_at": time.time(),
                    "jmx_path": jmx_path,
                    "csv_path": csv_path if csv_columns else None,
                    "curl_path": curl_path,
                    "validation": validation,
                    **result,
                }
            )
        emit("complete", result)
    except Exception as exc:
        err_detail = traceback.format_exc()
        emit("error", {"message": str(exc), "detail": err_detail})
        with _STATE_LOCK:
            _jobs[job_id]["status"] = "failed"
    finally:
        q.put(None)


def _derive_app_name(requests) -> str:
    for r in requests:
        try:
            parts = urlparse(r.url).netloc.replace("www.", "").split(".")
            if parts and parts[0]:
                return parts[0].replace("-", " ").replace("_", " ").title()
        except Exception:
            pass
    return "Application"


def _safe_filename(name: str) -> str:
    return re.sub(r"[^a-z0-9_\-]", "_", name.lower().strip())


def _coerce_int(value, default: int, minimum: int, maximum: int) -> int:
    try:
        iv = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, iv))


def _validate_jmx(path: str) -> dict:
    try:
        root = ET.parse(path).getroot()
        if root.tag != "jmeterTestPlan":
            return {"valid": False, "error": "Invalid root", "samplers": 0, "transactions": 0, "thread_groups": 0}
        xml = open(path, "r", encoding="utf-8").read()
        samplers = xml.count("<HTTPSamplerProxy")
        tx = xml.count("<TransactionController")
        tg = xml.count("<ThreadGroup")
        valid = samplers > 0 and tx > 0 and tg > 0
        return {"valid": valid, "samplers": samplers, "transactions": tx, "thread_groups": tg}
    except Exception as exc:
        return {"valid": False, "error": str(exc), "samplers": 0, "transactions": 0, "thread_groups": 0}


def _cleanup_loop() -> None:
    while True:
        now = time.time()
        with _STATE_LOCK:
            stale_sessions = [
                sid for sid, meta in _session_meta.items() if now - meta.get("created_at", now) > RETENTION_SECONDS
            ]
            for sid in stale_sessions:
                session = _sessions.pop(sid, None)
                _session_meta.pop(sid, None)
                if session:
                    try:
                        session.stop()
                    except Exception:
                        pass

            stale_jobs = [jid for jid, meta in _jobs.items() if now - meta.get("created_at", now) > RETENTION_SECONDS]
            for jid in stale_jobs:
                _jobs.pop(jid, None)
                _job_queues.pop(jid, None)
        time.sleep(60)


def _ensure_cleanup_thread() -> None:
    global _CLEANUP_STARTED
    with _STATE_LOCK:
        if _CLEANUP_STARTED:
            return
        threading.Thread(target=_cleanup_loop, name="jmxforge-cleanup", daemon=True).start()
        _CLEANUP_STARTED = True


if __name__ == "__main__":
    _ensure_cleanup_thread()
    print("\n" + "=" * 60)
    print("  JMX Forge - Manual Journey Recorder")
    print("  http://localhost:5000")
    print("=" * 60 + "\n")
    app.run(debug=False, port=5000, threaded=True, host="0.0.0.0")
