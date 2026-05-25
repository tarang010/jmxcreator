# JMX Forge

JMX Forge records real browser journeys and generates Apache JMeter-compatible JMX scripts with automated transaction grouping, AI-based correlation, parameterization, and result analytics.

## Key Features

- Live browser recording of URL, method, headers, body, and response
- Automated transaction controller creation
- AI-based correlation engine:
  - scans response JSON/HTML/cookies/headers
  - checks whether discovered values are reused in later requests
  - adds extractor candidates with confidence and reason
- Auto CSV field detection (username/password and similar fields)
- JMX preview and validation report before download
- Upload JTL + XML to generate industry-style load test dashboard
- Download artifacts: `.jmx`, optional `.csv`, and cURL replay file
- Health endpoint, job list endpoint, and stale-session cleanup

## Run

```bash
pip install -r requirements.txt
python -m playwright install chromium
python run.py
```

Open: `http://localhost:5000`

## Deploy On Render

This repo is ready for Render using Docker.

1. Push this project to GitHub.
2. In Render, create a new **Web Service** from the repo.
3. Render will detect [`render.yaml`](/D:/JMX%20Forge/jmx-forge/render.yaml) and provision service `jmx-forge`.
4. Deploy.

Key deployment files:
- [`render.yaml`](/D:/JMX%20Forge/jmx-forge/render.yaml)
- [`Dockerfile`](/D:/JMX%20Forge/jmx-forge/Dockerfile)
- [`.dockerignore`](/D:/JMX%20Forge/jmx-forge/.dockerignore)

Environment defaults for Render:
- `JMX_FORGE_HEADLESS=true`
- `JMX_FORGE_RETENTION_SECONDS=3600`

## Usage

### Journey to JMX

1. Enter target URL.
2. Configure threads, ramp-up, and loops.
3. Click **Perform Journey**.
4. Use the opened browser for the user flow.
5. Click **Generate JMX**.
6. Review pipeline output, correlation confidence, validation, and preview.
7. Download generated artifacts.

### Local Recorder + Render Processor (Recommended for cloud deployment)

When hosted on Render, popup browser recording cannot open on the server host.
Use local recorder agent:

```bash
python local_recorder_client.py \
  --server https://<your-render-service>.onrender.com \
  --url https://<target-app-url> \
  --threads 10 --ramp-up 60 --loops 1
```

Flow:
1. Local browser opens on your machine.
2. You perform journey and press Enter in terminal.
3. Captured requests are uploaded to Render endpoint `/api/generate-from-capture`.
4. Render generates JMX/CSV/cURL and returns download links.

### Result Analytics (JTL + XML)

1. Upload `JTL` file.
2. Upload `XML`/`JMX` file.
3. Click **Analyze Result**.
4. View summary with peak start time/users, throughput, errors, and p50/p80/p90/p95/p99.

## API

- `GET /api/health`
- `POST /api/start-recording`
- `GET /api/session-status/<session_id>`
- `POST /api/generate`
- `POST /api/generate-from-capture`
- `GET /api/stream/<job_id>`
- `GET /api/jobs`
- `GET /api/preview/<job_id>`
- `GET /api/download/<job_id>/<jmx|csv|curl>`
- `POST /api/analyze-results` (multipart: `jtl_file`, `xml_file`)

## Smoke Test

```bash
python smoke_test.py
```

Expected output: `SMOKE TEST PASSED`

## Documentation

- `docs/project-documentation.html`
