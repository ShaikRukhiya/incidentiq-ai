from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from hmac import compare_digest

from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.db import (
    create_incident,
    create_incident_action,
    get_incident,
    get_incident_action,
    init_db,
    list_incident_actions,
    list_incidents,
    transition_incident_action,
    update_incident,
)
from app.models import (
    IncidentActionCreate,
    IncidentActionDecision,
    IncidentActionExecution,
    IncidentCreate,
    IncidentOutcomeUpdate,
    InvestigationRequest,
)
from app.services.incident_agent import investigate_incident
from app.services.groq_ai import get_groq_status
from app.services.log_analysis import analyze_logs
from app.services.memory import HindsightMemoryService

@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(title=settings.app_name, lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

memory = HindsightMemoryService()


def require_approval_token(authorization: str | None) -> None:
    expected_token = settings.incidentiq_approval_token
    if not expected_token:
        raise HTTPException(status_code=503, detail="Human approval is not configured on this server")
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="A human approval token is required")
    supplied_token = authorization.removeprefix("Bearer ")
    if not compare_digest(supplied_token, expected_token):
        raise HTTPException(status_code=403, detail="Human approval token is invalid")


@app.get("/health")
async def health() -> dict:
    memory_status = await memory.check_connection()
    return {
        "status": "ok",
        "app": settings.app_name,
        "hindsight": memory_status["mode"],
        "hindsight_available": memory_status["reachable"],
    }


@app.get(f"{settings.api_prefix}/memory/health")
async def memory_health() -> dict:
    return await memory.check_connection()


@app.get(f"{settings.api_prefix}/dashboard")
async def dashboard() -> dict:
    memory_status = await memory.check_connection()
    incidents = list_incidents()
    total = len(incidents)
    critical = sum(1 for item in incidents if item.get("severity", "").lower() == "critical")
    resolved = sum(1 for item in incidents if item.get("status", "").lower() in {"resolved", "closed"})
    open_count = total - resolved
    severities = {"Critical": 0, "High": 0, "Medium": 0, "Low": 0}
    for item in incidents:
        severity = str(item.get("severity", "Low")).title()
        if severity in severities:
            severities[severity] += 1
    recent = incidents[:5]
    error_buckets: dict[str, int] = {}
    for item in incidents:
        logs = str(item.get("logs") or "")
        words = [w.strip().lower() for w in logs.replace("/", " ").split() if len(w) > 5]
        for word in words:
            if word in {"error", "warning", "critical", "timeout", "failed", "failed", "database", "connection"}:
                error_buckets[word] = error_buckets.get(word, 0) + 1

    mttr_days = 0.0
    resolved_items = [item for item in incidents if item.get("status", "").lower() in {"resolved", "closed"}]
    if resolved_items:
        total_minutes = 0
        for item in resolved_items:
            created = item.get("created_at")
            updated = item.get("updated_at")
            if created and updated:
                try:
                    start = datetime.fromisoformat(created)
                    end = datetime.fromisoformat(updated)
                    total_minutes += max((end - start).total_seconds() / 60.0, 0)
                except ValueError:
                    pass
        mttr_days = round((total_minutes / max(len(resolved_items), 1)) / 60 / 24, 2)

    return {
        "summary": {
            "total_incidents": total,
            "critical_incidents": critical,
            "resolved_incidents": resolved,
            "open_incidents": open_count,
            "mttr_days": mttr_days,
        },
        "severity_distribution": severities,
        "recent_incidents": recent,
        "frequent_errors": sorted(error_buckets.items(), key=lambda x: x[1], reverse=True)[:8],
        "memory_activity": {
            "status": memory_status["mode"],
            "bank": settings.hindsight_bank_id,
            "available": memory_status["reachable"],
            "configured": memory_status["configured"],
            "reachable": memory_status["reachable"],
            "error": memory_status["error"],
        },
    }


@app.get(f"{settings.api_prefix}/incidents")
def get_incidents() -> list[dict]:
    return list_incidents()


@app.post(f"{settings.api_prefix}/incidents")
async def create_incident_endpoint(payload: IncidentCreate) -> dict:
    memory_status = await memory.check_connection()
    incident_id = f"INC-{uuid.uuid4().hex[:8].upper()}"
    incident = {
        "incident_id": incident_id,
        "title": payload.title,
        "summary": payload.summary,
        "severity": payload.severity,
        "services": payload.services,
        "source": payload.source,
        "logs": payload.logs,
        "status": "open",
        "memory_status": memory_status["mode"],
    }
    result = create_incident(incident)
    return result


@app.get(f"{settings.api_prefix}/incidents/{{incident_id}}")
def get_incident_endpoint(incident_id: str) -> dict:
    incident = get_incident(incident_id)
    if incident is None:
        raise HTTPException(status_code=404, detail="Incident not found")
    return incident


@app.post(f"{settings.api_prefix}/incidents/{{incident_id}}/analyze")
async def analyze_incident_endpoint(incident_id: str, payload: InvestigationRequest) -> dict:
    incident = get_incident(incident_id)
    if incident is None:
        raise HTTPException(status_code=404, detail="Incident not found")

    logs = payload.logs or incident.get("logs") or ""
    if not logs:
        raise HTTPException(status_code=400, detail="Submission requires log data")

    incident_payload = {
        **incident,
        "logs": logs,
        "incident_id": incident_id,
        "services": payload.services or incident.get("services") or "",
        "title": payload.title or incident.get("title"),
        "summary": incident.get("summary") or payload.title or "Production incident investigation",
    }
    analysis = await investigate_incident(incident_payload, memory)
    retained_mode = await memory.retain_incident({**incident_payload, **analysis})
    analysis["memory_status"] = retained_mode
    analysis["memory_source"] = retained_mode

    update_incident(
        incident_id,
        {
            "root_cause": analysis["root_cause"],
            "recommended_resolution": analysis["suggested_resolution"],
            "evidence": analysis["supporting_evidence"],
            "confidence": analysis["confidence"],
            "human_investigation_required": analysis["human_investigation_required"],
            "historical_matches": analysis["historical_matches"],
            "memory_status": analysis["memory_status"],
            "outcome_notes": analysis.get("memory_impact"),
            "status": "investigating",
            "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        },
    )
    return {"incident_id": incident_id, **analysis}


@app.post(f"{settings.api_prefix}/incidents/{{incident_id}}/outcome")
async def incident_outcome_endpoint(incident_id: str, payload: IncidentOutcomeUpdate) -> dict:
    incident = get_incident(incident_id)
    if incident is None:
        raise HTTPException(status_code=404, detail="Incident not found")
    if payload.success:
        unfinished_actions = [
            action
            for action in list_incident_actions(incident_id)
            if action["status"] in {"pending", "approved"}
        ]
        if unfinished_actions:
            raise HTTPException(
                status_code=409,
                detail="Resolve pending action approvals and confirm approved actions as executed before recording success",
            )
    status = payload.status
    services = [item.strip() for item in str(incident.get("services") or "").split(",") if item.strip()]
    outcome = payload.outcome or status
    await memory.update_incident_outcome(
        incident_id,
        outcome,
        payload.success,
        services=services,
        recommended_resolution=incident.get("recommended_resolution"),
        incident=incident,
    )
    updated = update_incident(
        incident_id,
        {
            "status": status,
            "outcome_notes": payload.outcome,
            "memory_status": memory.mode,
        },
    )
    return updated


@app.post(f"{settings.api_prefix}/incidents/{{incident_id}}/actions")
async def propose_incident_action(incident_id: str, payload: IncidentActionCreate) -> dict:
    if get_incident(incident_id) is None:
        raise HTTPException(status_code=404, detail="Incident not found")
    return create_incident_action(
        action_id=f"ACT-{uuid.uuid4().hex[:10].upper()}",
        incident_id=incident_id,
        description=payload.description,
        rationale=payload.rationale,
    )


@app.get(f"{settings.api_prefix}/incidents/{{incident_id}}/actions")
def get_incident_actions(incident_id: str) -> list[dict]:
    if get_incident(incident_id) is None:
        raise HTTPException(status_code=404, detail="Incident not found")
    return list_incident_actions(incident_id)


@app.post(f"{settings.api_prefix}/incidents/{{incident_id}}/actions/{{action_id}}/approval")
def decide_incident_action(
    incident_id: str,
    action_id: str,
    payload: IncidentActionDecision,
    authorization: str | None = Header(default=None),
) -> dict:
    require_approval_token(authorization)
    action = get_incident_action(action_id)
    if action is None or action["incident_id"] != incident_id:
        raise HTTPException(status_code=404, detail="Action not found for this incident")
    if action["status"] != "pending":
        raise HTTPException(status_code=409, detail=f"Action is already {action['status']}")
    updated = transition_incident_action(
        action_id=action_id,
        expected_status="pending",
        status=payload.decision,
        note_field="decision_note",
        note=payload.note,
        timestamp_field="decided_at",
        timestamp=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    if updated is None:
        raise HTTPException(status_code=409, detail="Action state changed; refresh and try again")
    return updated


@app.post(f"{settings.api_prefix}/incidents/{{incident_id}}/actions/{{action_id}}/execution")
def confirm_incident_action_execution(
    incident_id: str,
    action_id: str,
    payload: IncidentActionExecution,
    authorization: str | None = Header(default=None),
) -> dict:
    require_approval_token(authorization)
    action = get_incident_action(action_id)
    if action is None or action["incident_id"] != incident_id:
        raise HTTPException(status_code=404, detail="Action not found for this incident")
    if action["status"] != "approved":
        raise HTTPException(status_code=409, detail="Only an approved action can be marked executed")
    updated = transition_incident_action(
        action_id=action_id,
        expected_status="approved",
        status="executed",
        note_field="execution_note",
        note=payload.execution_note,
        timestamp_field="executed_at",
        timestamp=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    if updated is None:
        raise HTTPException(status_code=409, detail="Action state changed; refresh and try again")
    return updated


@app.get(f"{settings.api_prefix}/diagnostics")
async def diagnostics() -> dict:
    memory_status = await memory.check_connection()
    return {
        "memory": {
            "available": memory_status["reachable"],
            "mode": memory_status["mode"],
            "bank_id": settings.hindsight_bank_id,
            "base_url_configured": memory_status["configured"],
            "reachable": memory_status["reachable"],
            "connection_error": memory_status["error"],
        },
        "llm": get_groq_status(),
        "approval": {"configured": bool(settings.incidentiq_approval_token)},
        "analysis": {
            "sample": analyze_logs("ERROR api timeout after 30s\nCRITICAL db connection refused\nWARNING cache invalidation issue", ["api", "db"])
        },
    }
