# IncidentIQ

IncidentIQ is an AI-driven incident response dashboard built for HackWithBangalore 3.0. It combines log analysis, structured investigation results, and a Hindsight memory backend with a clearly labeled local SQLite fallback to help teams reduce repeated incident investigation.

## Features
- Manual incident creation and tracking
- Log analysis for severity extraction, stack traces, HTTP error patterns, and repeated failures
- Structured root-cause assessment with evidence and confidence scoring
- Hindsight client integration with explicit SQLite fallback status when the server is unavailable
- Dark SaaS-style React dashboard
- API and demo-friendly SQLite persistence

## Project structure
- `backend/` — FastAPI service and business logic
- `frontend/` — Vite + React dashboard
- `IMPLEMENTATION_PLAN.md` — rollout and audit notes

## Local setup

### 1. Python backend
```bash
Set-Location D:\IncidentIQ\backend
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

### 2. Frontend
```bash
Set-Location D:\IncidentIQ\frontend
npm install
npm run dev
```

### 3. Configure Hindsight and Groq
IncidentIQ includes the `hindsight-client` SDK. Hindsight mode requires a running Hindsight server; the app retains its SQLite fallback when the server is unavailable. Set these variables in the backend process environment or in `backend\.env`:
```env
DATABASE_URL=sqlite:///./incidentiq.db
HINDSIGHT_BASE_URL=http://127.0.0.1:8888
HINDSIGHT_API_KEY=
HINDSIGHT_BANK_ID=incidentiq-memory
GROQ_API_KEY=your-key
GROQ_MODEL=llama-3.3-70b-versatile
INCIDENTIQ_APPROVAL_TOKEN=generate-a-long-random-value
```
`DATABASE_URL` supports persistent SQLite files only. Relative SQLite paths resolve from `backend\`; absolute paths are supported. Other database engines and in-memory SQLite are rejected.

Do not commit `backend\.env`. Hindsight credentials are needed only when using an authenticated Hindsight deployment. Without `INCIDENTIQ_APPROVAL_TOKEN`, action approvals and execution confirmations remain disabled.

### Human approval gate
In an investigation, propose an operational action; it is recorded as `pending`. An operator with the configured server token must approve or reject it. Only an approved proposal may be marked executed, and the same token is required. IncidentIQ never executes shell commands or infrastructure changes: an operator performs approved work outside the application and then records that it happened.

The API enforces the state transitions:

- `pending` → `approved` or `rejected`
- `approved` → `executed` (operator confirms external execution)
- Rejected/executed actions cannot be approved or executed again.

The token is a shared approval secret, not per-user identity or a replacement for production authentication; use a strong secret and protect the deployment with HTTPS and access controls.

## Run Hindsight on Windows

The official Hindsight server quick start uses Docker and a persistent Docker volume. Install Docker Desktop for Windows from the [official instructions](https://docs.docker.com/desktop/setup/install/windows-install/) (or, where WinGet is available, run `winget install --exact --id Docker.DockerDesktop`), then start Docker Desktop and verify that `docker version` shows both Client and Server.

The following starts Hindsight's API on port `8888` and UI on port `9999`. This uses the slim image with OpenAI embeddings and the built-in RRF reranker, avoiding local model dependencies. OpenAI usage may incur provider charges. Keep the key in the process environment and never commit it:

```powershell
$env:OPENAI_API_KEY = '<YOUR_OPENAI_API_KEY>'
docker run --detach --pull always --name hindsight --restart unless-stopped --publish 8888:8888 --publish 9999:9999 --env "HINDSIGHT_API_LLM_PROVIDER=openai" --env "HINDSIGHT_API_LLM_API_KEY=$env:OPENAI_API_KEY" --env "HINDSIGHT_API_EMBEDDINGS_PROVIDER=openai" --env "HINDSIGHT_API_RERANKER_PROVIDER=rrf" --volume hindsight-data:/home/hindsight/.pg0 ghcr.io/vectorize-io/hindsight:latest-slim
docker ps --filter name=hindsight
docker logs --tail 50 hindsight
```

In the PowerShell window used to run IncidentIQ, configure the client and start the backend:

```powershell
$env:HINDSIGHT_BASE_URL = 'http://127.0.0.1:8888'
$env:HINDSIGHT_BANK_ID = 'incidentiq-memory'
# Set HINDSIGHT_API_KEY only if your Hindsight deployment requires client authentication.
Set-Location 'D:\IncidentIQ\backend'
python -m uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Verify the actual SDK health check—not just the container state—with:

```powershell
Invoke-RestMethod 'http://127.0.0.1:8000/api/memory/health' | Format-List
```

Hindsight is connected only when the response reports `reachable: True` and `mode: hindsight`. The dashboard uses the same live check and labels SQLite fallback separately. Hindsight's LLM provider key (`HINDSIGHT_API_LLM_API_KEY`) belongs to the Hindsight container; `HINDSIGHT_API_KEY` is a separate optional client-auth credential. `GROQ_API_KEY` is unrelated to Hindsight memory retention.

## Hackathon demo
1. Start Hindsight using the Windows instructions above and verify `/api/memory/health` reports `reachable: True`; otherwise IncidentIQ clearly uses its SQLite fallback.
2. Set `GROQ_API_KEY` to enable Groq. The dashboard will report the provider as connected only after a successful incident-analysis request.
3. Create and investigate Incident A using Nginx connection-refused logs for the configured upstream `127.0.0.1:8001`; do not assume the port is incorrect unless listener evidence supports that conclusion.
4. Record the successful fix in the investigation view. The resolution and outcome are retained as a success.
5. Create and investigate similar Incident B. Review the historical-memory indicator and recommendation, then record whether the recommendation succeeded or failed.
6. Investigate a third similar incident to see recorded failed attempts included in the recommendation.

## Validation
```bash
Set-Location D:\IncidentIQ\backend
python -m pytest -q
```

```bash
Set-Location D:\IncidentIQ\frontend
npm run build
```

### Optional real-provider integration checks
The default test suite uses mocks and local SQLite; it makes no provider calls. If credentials and services are configured, opt in explicitly to real integration tests:

```powershell
Set-Location 'D:\IncidentIQ\backend'
$env:PYTHONPATH='D:\IncidentIQ\backend'
$env:HINDSIGHT_BASE_URL='http://127.0.0.1:8888'
$env:HINDSIGHT_BANK_ID='incidentiq-memory-test'
$env:INCIDENTIQ_RUN_LIVE_INTEGRATION_TESTS='1'
python -m pytest -q tests\test_live_integrations.py
```

Two live Hindsight tests check retain/recall and an API A→B workflow across a backend process restart. They write unique records; the restart test uses a suffixed bank and temporary SQLite database. The Groq test sends a real analysis request and may incur provider usage. Do not run these tests against a production memory bank unless those test records are acceptable. Set `HINDSIGHT_API_KEY` only when client authentication is enabled.
