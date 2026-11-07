# IncidentIQ Implementation Plan

## Repository audit
- The repository was empty at the start of the task, so there was no existing application, tests, or commit history to preserve.
- This means the project has to be created from scratch in the working directory while following the HackWithBangalore 3.0 brief.

## Build approach
- Backend: Python + FastAPI + SQLite + Pydantic
- AI / log analysis: rule-based structured analysis with optional Groq/Hindsight integration if environment credentials are configured
- Memory: optional Hindsight SDK client wrapped in a clean fallback that stores local incident memory when the external service is unavailable
- Frontend: React + Vite + TypeScript + a dark enterprise dashboard interface

## Key features to implement
1. Manual incident creation and status tracking
2. Log upload or pasted log analysis with severity extraction and repeated-pattern detection
3. Structured investigation output with evidence / hypothesis separation and confidence scoring
4. Hindsight-inspired memory retention and recall with graceful fallback behavior
5. Human approval requirements before risky actions
6. Dashboard with metrics and recent incident activity

## Validation strategy
- Backend API tests via pytest
- Frontend build validation via npm run build
- Memory and analysis checks using seeded incident data

## Risks / dependencies
- Hindsight Cloud or self-hosted server is optional; the app should degrade gracefully when not configured.
- Groq API integration is optional and should not block core functionality.
- The code will be intentionally resilient to missing external services so demo and local usage work without paid accounts.
