# IncidentIQ Verification Report

Updated: 2026-11-07

## Summary

- Backend validation: **21 passed, 3 skipped**. Two Hindsight integration tests and one Groq integration test require explicit opt-in and live provider configuration.
- Frontend production build: **passed** (`tsc -b` and Vite; 31 modules transformed).
- Current Hindsight API status: **healthy and reachable** at `127.0.0.1:8888`; IncidentIQ and the frontend proxy report `mode=hindsight`.
- Live retain/recall validation was attempted three times with user approval, including after the user reported verifying credits, but both Hindsight tests still failed because the key in the container received HTTP 429 `insufficient_quota`. Hindsight health alone does not establish that memory writes work; failed operations currently fall back to SQLite. The Groq live test was skipped because `GROQ_API_KEY` is absent from `backend\.env` and is not configured in the running API.
- The approval workflow is enabled with a randomly generated token stored in the Git-ignored `backend\.env`. Runtime checks confirmed missing/invalid tokens are rejected and a valid token passes authentication.
- The user reports completing the organizer-required Band AI desktop step; it cannot be independently verified from this repository or runtime.
- Mock tests cover SDK health checks, incident and outcome retention/recall, API-level A→B recommendation influence, fallback behavior, and unrelated-incident filtering. The opt-in process-restart test requires a running Hindsight service.
- The protected action workflow requires an explicit server token for approval and execution confirmation. IncidentIQ does not execute commands or infrastructure changes.

## Hindsight integration audit and implementation status

The installed dependency is `hindsight-client==1.2.0`. Runtime inspection confirmed that its `Hindsight` client exposes `get_version()`, `aretain(...)`, and `arecall(...)`; the existing synchronous `retain(...)` and `recall(...)` arguments were valid, but those wrappers blocked inside async API handlers. The adapter now uses the SDK's async operations, applies a five-second timeout to health checks and the SDK's 300-second timeout to memory operations (one attempt each), verifies `RetainResponse.success`, and uses `document_id` plus `update_mode="replace"` so retries update a stable incident record. Outcome IDs include a deterministic hash of the recorded details: identical retries are idempotent while distinct successful/failed outcomes remain separately retained and related to their `incident_id`.

At the initial audit, the Hindsight URL and bank were not configured and Docker was unavailable. Since then, Docker Desktop storage was moved to D:, the official `latest-slim` image was verified and started, and the existing `hindsight-data` volume was preserved. The backend now uses the local Hindsight URL and bank. The configured OpenAI key has no remaining API credits, so live retain operations fail even though the Hindsight health check succeeds.

The dashboard, `/health`, `/api/dashboard`, and `/api/diagnostics` now use a live SDK version check before labeling the active provider. Health output distinguishes unconfigured, configured-but-unreachable, and reachable Hindsight. If an SDK operation fails or reports an unsuccessful retain, that operation is stored/retrieved via SQLite fallback and the result's `memory_status` identifies the fallback. There is no dual-write. Stable Hindsight document IDs prevent new duplicate writes on retries; existing SQLite history is not migrated or deleted when switching providers and only the active provider is queried.

### Required Windows setup

Official references: [Hindsight project quick start](https://github.com/vectorize-io/hindsight#quick-start) and [Docker Desktop for Windows installation](https://docs.docker.com/desktop/setup/install/windows-install/). The official Hindsight container listens on `8888` (API) and `9999` (UI) and uses a persistent volume. This example uses OpenAI for the LLM and embeddings and the built-in RRF reranker; provider usage may incur charges. Keep the key out of source control:

```powershell
winget install --exact --id Docker.DockerDesktop
# Open Docker Desktop and wait for its engine to start.
docker version
$env:OPENAI_API_KEY = '<YOUR_OPENAI_API_KEY>'
docker run --detach --pull always --name hindsight --restart unless-stopped --publish 8888:8888 --publish 9999:9999 --env "HINDSIGHT_API_LLM_PROVIDER=openai" --env "HINDSIGHT_API_LLM_API_KEY=$env:OPENAI_API_KEY" --env "HINDSIGHT_API_EMBEDDINGS_PROVIDER=openai" --env "HINDSIGHT_API_RERANKER_PROVIDER=rrf" --volume hindsight-data:/home/hindsight/.pg0 ghcr.io/vectorize-io/hindsight:latest-slim
docker ps --filter name=hindsight
docker logs --tail 50 hindsight
```

In the PowerShell window that starts IncidentIQ, configure the separate **client** settings and run the backend:

```powershell
$env:HINDSIGHT_BASE_URL = 'http://127.0.0.1:8888'
$env:HINDSIGHT_BANK_ID = 'incidentiq-memory'
# Set HINDSIGHT_API_KEY only if client authentication is enabled on the Hindsight server.
Set-Location 'D:\IncidentIQ\backend'
python -m uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

The API credential `HINDSIGHT_API_KEY` is optional for a local unauthenticated service. `HINDSIGHT_API_LLM_API_KEY` is instead the model-provider credential passed to the Hindsight container; it is not an IncidentIQ setting. `GROQ_API_KEY` is unrelated. Verify with a real request:

```powershell
Invoke-RestMethod 'http://127.0.0.1:8000/api/memory/health' | Format-List
```

Only `reachable: True` and `mode: hindsight` confirm the live SDK version check; successful retain and recall tests are separately required to verify memory operations. The current service passes the health check, but live retain/recall tests hit the OpenAI account's exhausted credit balance and therefore did not pass.

The current live API health check returned:

```text
configured : True
reachable  : True
mode       : hindsight
bank_id    : incidentiq-memory
error      : None
```

The dashboard reports Hindsight connected. This does not imply live writes are succeeding: the two live integration tests observed HTTP 429 `insufficient_quota` during Hindsight retain, and the IncidentIQ service correctly recorded those failed operations in SQLite fallback instead.

## A-to-B learning audit and root cause

The live SQLite database was inspected read-only. Incident A `INC-DA6A6F8E` exists in `incidents` with status `resolved`, its operator-recorded outcome in `outcome_notes`, and `memory_status=local-fallback`. Its resolution is also present in `memory_events` as `category=resolution`, `success=1`; two successful records exist for the same incident and are deduplicated during retrieval. The event schema stores `incident_id`, `category`, `bank_id`, `resolution`, `success`, and JSON `content`. A fresh Python process reading the same SQLite file returned the incident and successful outcome.

The cause was not missing persistence. SQLite incident and resolution memories were retrieved through separate paths. The fallback queried the newest five records in each category **before** relevance ranking. An older incident could therefore be omitted from `historical_matches` even while its successful resolution still survived the separate resolution query. The API could say that a recorded outcome was considered, while the incident-match list was empty. The frontend rendered only incident references and did not display the separate `successful_resolution_memory` object, so it could not show that outcome.

The regression reproduced that failure with Incident A older than six newer unrelated Nginx records and four newer unrelated successful outcomes: before the fix, the API test failed because A was absent from `historical_matches`, despite its outcome being retrieved. The fix ranks all fallback candidates before applying the existing score threshold and result limit, removes timestamps and URL paths from search tokens, and requires shared Nginx/upstream/proxy evidence or a shared non-generic service. The threshold was not lowered. Relevant outcome details are joined to their incident match and persisted with the analysis for the frontend.

The current live database query also confirmed A and its success remain after restarting the backend. `GET /api/incidents/INC-8D6C17F4/analyze` returned A as a relevant match and returned its recorded successful outcome. Browser verification showed the same prior incident, operator-recorded outcome, and previous resolution in Incident B's UI, including after reload.

## Environment and observed provider status

The local API was queried without printing configuration values:

```powershell
$health = Invoke-RestMethod 'http://127.0.0.1:8000/api/memory/health'
$diag = Invoke-RestMethod 'http://127.0.0.1:8000/api/diagnostics'
[pscustomobject]@{
    hindsight_configured = $health.configured
    hindsight_reachable = $health.reachable
    hindsight_mode = $health.mode
    groq_configured = $diag.llm.configured
    groq_connected = $diag.llm.connected
    groq_status = $diag.llm.status
} | Format-List
```

Observed result at the initial audit, before Docker/Hindsight setup:

```text
hindsight_configured : False
hindsight_reachable  : False
hindsight_mode       : local-fallback
groq_configured      : False
groq_connected       : False
groq_status          : not-configured
```

The preceding result is a historical audit snapshot and is superseded by the current status above. The health endpoint is `GET /api/memory/health`; it reports configured/reachable state without exposing secret values.

### Real-service checks

Live Hindsight tests were run with explicit user approval. Both attempted writes and failed with HTTP 429 `insufficient_quota`; the separate Groq test remained skipped because `GROQ_API_KEY` was unavailable. Mock and SQLite tests do not establish successful live provider operations.

When a non-production Hindsight bank and credentials are available, configure:

```dotenv
HINDSIGHT_BASE_URL=http://127.0.0.1:8888
HINDSIGHT_API_KEY=<only if client authentication is enabled>
HINDSIGHT_BANK_ID=incidentiq-memory
```

Start the Hindsight container with the server-side model key as described above, then check the health endpoint and explicitly run the opt-in tests against a non-production bank:

```powershell
Set-Location 'D:\IncidentIQ\backend'
$env:PYTHONPATH='D:\IncidentIQ\backend'
$env:HINDSIGHT_BASE_URL='http://127.0.0.1:8888'
$env:HINDSIGHT_BANK_ID='incidentiq-memory-test'
$env:INCIDENTIQ_RUN_LIVE_INTEGRATION_TESTS='1'
python -m pytest -q tests\test_live_integrations.py::test_live_hindsight_retain_and_recall tests\test_live_integrations.py::test_live_hindsight_api_learning_survives_backend_process_restart
```

These tests write unique records to the bank; the process-restart API test uses its own suffixed bank ID and a temporary SQLite database. Do not use a production bank unless retained test records are acceptable.

For Groq, configure `GROQ_API_KEY` and optionally `GROQ_MODEL`, then run:

```powershell
Set-Location 'D:\IncidentIQ\backend'
$env:PYTHONPATH='D:\IncidentIQ\backend'
$env:INCIDENTIQ_RUN_LIVE_INTEGRATION_TESTS='1'
python -m pytest -q tests\test_live_integrations.py::test_live_groq_analysis_request
```

This makes a real provider request and may incur usage. A successful result is only established by running this test against the provider; a mock success is not live verification.

### Configuration requirements

- `DATABASE_URL=sqlite:///./incidentiq.db` is the supported persistent database configuration. Relative paths resolve from `backend\`; absolute SQLite file paths are supported. Other database engines and in-memory SQLite are rejected.
- Hindsight variables: `HINDSIGHT_BASE_URL`, `HINDSIGHT_BANK_ID`, and `HINDSIGHT_API_KEY` if the server requires authentication.
- Groq variables: `GROQ_API_KEY`; optional `GROQ_MODEL` selects the model.
- `INCIDENTIQ_APPROVAL_TOKEN` must be set to enable approval decisions and execution confirmations. Use a strong random secret. It is a shared token, not per-user identity; deploy behind HTTPS and appropriate access controls.
- Keep secrets in the backend process environment or `backend\.env`; never commit or print them.

## Approval workflow and frontend verification

The API persists the action states `pending`, `approved`, `rejected`, and `executed`. Pending proposals cannot be recorded as successful resolutions. Decisions and execution confirmations require the configured `INCIDENTIQ_APPROVAL_TOKEN`. Only approved proposals may transition to executed; rejected/executed proposals are terminal.

**Executed** is an operator's confirmation that work was performed externally. The application has no shell or infrastructure-command execution capability and never runs a proposed action automatically.

Browser verification against the local frontend/API confirmed that:

- The dashboard and incident investigation load and communicate with the backend.
- The proposal flow records a pending action.
- Attempting to record success while an action is pending returns an API conflict and displays an explanatory error in the UI.
- With no approval token supplied to the browser, approval controls remain disabled.
- Recent-incidents navigation opens the history view.
- The current incident is excluded from its own historical matches.
- Incident B's investigation displays Incident A's title, recorded successful outcome, previous resolution, and a memory-impact explanation. The result remains visible after reloading the frontend.

No screenshot file is attached to this report. For a useful local capture, show the dashboard integration status and a second capture of Incident B's investigation with historical memory influence visible. Do not include secrets in screenshots.

## Reproducible Incident A → Incident B learning scenario

Automated coverage: `backend\tests\test_api.py::test_nginx_incident_learning_survives_restart_and_surfaces_successful_outcome`.

1. Create and analyze an Nginx connection-refused Incident A for `127.0.0.1:8001`.
2. Record a successful resolution through the API and verify the incident and resolution-event row in SQLite.
3. Launch a new Python process against the same `DATABASE_URL`; verify A's resolved status, outcome text, and `success=1` event survive process restart.
4. Add six newer unrelated incident memories and four newer unrelated successful outcome memories, forcing A beyond the old five-row pre-ranking windows.
5. Create Incident B with nearly identical symptoms but a different service label and `/api/orders` path. Its actual API response returns A and the successful resolution.
6. Create an unrelated PostgreSQL connection-refused incident; verify it does not retrieve A as an incident match or successful resolution.
7. Verify Incident B's UI shows A, the operator-recorded outcome, its previous resolution, and the memory-impact explanation both immediately and after reload.

This workflow was verified against **SQLite local fallback only**. It was not run against Hindsight. The successful outcome is explicitly operator-recorded; it is not independently validated by IncidentIQ.

## Nginx upstream connection-refused scenario

The exact log signature is covered by `backend\tests\test_api.py::test_nginx_upstream_connection_refused_uses_port_and_relevant_unique_history`:

```text
connect() failed (111: Connection refused) while connecting to upstream, upstream: "http://127.0.0.1:8001/"
```

The verified local diagnosis identifies a refused connection to `127.0.0.1:8001`. This log shows the configured upstream and a connection refusal; it does **not** show which port the application is listening on. IncidentIQ therefore does not declare port 8001 incorrect unless logs also provide explicit application-listener evidence. The recommendation is to verify the service listener, bind address, and reachability from Nginx. The log alone does not support restarting the gateway.

SQLite fallback retrieval now ranks candidates before limiting results, deduplicates by incident ID, ignores timestamp and URL-path noise, and requires shared upstream/proxy evidence or a shared non-generic service in addition to the existing relevance threshold. Successful and failed resolution details are linked to matched incidents and included in the saved analysis. The UI displays the recorded outcome and previous resolution; it labels them as recorded history rather than claiming independent verification.

## Startup and validation commands

Backend, in a PowerShell window:

```powershell
Set-Location 'D:\IncidentIQ\backend'
$env:PYTHONPATH='D:\IncidentIQ\backend'
python -m uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Frontend, in a second window:

```powershell
Set-Location 'D:\IncidentIQ\frontend'
npm run dev
```

Backend tests, as actually run:

```powershell
Set-Location 'D:\IncidentIQ\backend'; python -m pytest -q
```

Actual result:

```text
21 passed, 3 skipped, 1 warning in 3.23s
```

The skipped tests are the two opt-in real Hindsight tests and one opt-in Groq test. Pytest also emitted a `pytest-asyncio` fixture-loop-scope deprecation notice and a Starlette `python_multipart` pending-deprecation warning. Focused API regressions were also run:

```powershell
Set-Location 'D:\IncidentIQ\backend'; python -m pytest -q tests\test_api.py -k hindsight
```

Result: **6 passed, 15 deselected**. The SQLite A-to-B and Nginx regressions were also run:

```powershell
Set-Location 'D:\IncidentIQ\backend'; python -m pytest -q tests\test_api.py::test_nginx_incident_learning_survives_restart_and_surfaces_successful_outcome tests\test_api.py::test_hindsight_api_a_to_b_recall_survives_service_restart_and_filters_unrelated tests\test_api.py::test_nginx_upstream_connection_refused_uses_port_and_relevant_unique_history
```

Result: **3 passed**.

Frontend build, as actually run:

```powershell
Set-Location D:\IncidentIQ\frontend; npm run build
```

Actual result:

```text
> tsc -b && vite build
vite v5.4.21 building for production...
✓ 31 modules transformed.
✓ built in 1.18s
```

## Remaining limitations and manual requirements

- Real Hindsight API and Hindsight server-side LLM provider availability are still required to prove live connection, retain, recall, A→B influence, and backend-process restart persistence. The service must be started by the user; Docker is not installed here.
- SQLite-only memories are not automatically copied into a Hindsight bank. They remain intact and available in fallback mode; historical records must be re-retained or migrated separately before Hindsight-only recall can use them.
- A valid Groq key and network access are required to prove a real model request.
- SQLite persistence and learning are local to the configured database and do not synchronize with Hindsight. Provider switching does not copy or delete existing memories; the active provider is queried, and there is no dual-write.
- Fallback search currently reads and ranks all matching-category SQLite memory rows. Very large local histories may need an indexed search strategy; a small arbitrary pre-ranking cap is intentionally not used because it caused false negatives.
- Approval uses a shared server-side token, not distinct user identities or role-based access control. Production deployment needs proper authentication, HTTPS, and access controls.
- Band AI desktop usage cannot be verified from repository files or these tests. If the hackathon requires it, the user must manually download and use Band AI desktop and retain any required evidence.
