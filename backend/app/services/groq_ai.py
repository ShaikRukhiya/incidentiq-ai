from __future__ import annotations

import json
import os
from typing import Any

from app.config import settings


_last_request_succeeded: bool | None = None


def get_groq_status() -> dict[str, Any]:
    configured = bool(settings.groq_api_key or os.getenv("GROQ_API_KEY"))
    if not configured:
        status = "not-configured"
    elif _last_request_succeeded is True:
        status = "connected"
    elif _last_request_succeeded is False:
        status = "request-failed"
    else:
        status = "not-yet-verified"
    return {
        "configured": configured,
        "connected": _last_request_succeeded is True,
        "status": status,
        "model": settings.groq_model,
    }


class GroqIncidentAnalyzer:
    def __init__(self) -> None:
        self.api_key = settings.groq_api_key or os.getenv("GROQ_API_KEY")
        self.model = settings.groq_model or "llama-3.3-70b-versatile"
        self.client: Any = None
        self.enabled = bool(self.api_key)

        if self.enabled:
            try:
                from groq import Groq

                self.client = Groq(api_key=self.api_key)
            except Exception:
                self.client = None

    async def analyze(self, incident: dict[str, Any], historical_matches: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        global _last_request_succeeded
        if not self.enabled or self.client is None:
            return self._fallback_analysis(incident, historical_matches)

        system_prompt = (
            "You are an SRE incident diagnosis assistant. Return only valid JSON with the keys: "
            "summary, root_cause, supporting_evidence, affected_services, severity_assessment, "
            "recommended_investigation_steps, suggested_resolution, confidence, human_investigation_required, "
            "facts, hypotheses, memory_impact, provider. "
            "Use only evidence from the logs and the supplied historical_matches. "
            "When evidence is insufficient, explicitly say so."
        )
        history_text = json.dumps(historical_matches or [], ensure_ascii=False) if historical_matches else "[]"
        user_prompt = json.dumps(
            {
                "incident": incident,
                "historical_matches": history_text,
                "instructions": "Assess the likely root cause, list confidence and actions, and explain how prior incidents influenced the recommendation if history was available.",
            },
            ensure_ascii=False,
        )

        try:
            completion = self.client.chat.completions.create(
                model=self.model,
                response_format={"type": "json_object"},
                temperature=0.2,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
            )
            content = completion.choices[0].message.content
            payload = json.loads(content)
            if not isinstance(payload, dict):
                raise ValueError("Groq response was not a JSON object")
            payload["provider"] = "groq"
            if "memory_impact" not in payload:
                payload["memory_impact"] = "No relevant prior incident was found."
            _last_request_succeeded = True
            return payload
        except Exception:
            _last_request_succeeded = False
            return self._fallback_analysis(incident, historical_matches)

    def _fallback_analysis(self, incident: dict[str, Any], historical_matches: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        services = []
        for entry in (incident.get("services") or "").split(","):
            cleaned = entry.strip()
            if cleaned:
                services.append(cleaned)

        logs = str(incident.get("logs") or "")
        lower_logs = logs.lower()
        nginx_port_mismatch = (
            "nginx" in lower_logs
            and "upstream" in lower_logs
            and "port" in lower_logs
            and any(term in lower_logs for term in ("incorrect", "mismatch", "but service listens", "expected port"))
        )
        if nginx_port_mismatch:
            root_cause = "Nginx is forwarding to an incorrect upstream port, which does not match the application listener."
            suggested_resolution = "Set the Nginx upstream port to match the application's listening port, validate the configuration, reload Nginx, and confirm successful HTTP responses."
        elif "502" in lower_logs or "gateway" in lower_logs:
            root_cause = "A gateway or upstream dependency is returning HTTP 502 responses, likely due to a failing upstream service or backend timeout."
            suggested_resolution = "Restore the upstream dependency or roll back the most recent release affecting the failing path, then validate traffic after the fix."
        elif "timeout" in lower_logs:
            root_cause = "The service is timing out under load and the failure path suggests a dependency or saturation issue."
            suggested_resolution = "Investigate the slow dependency or saturation source, then validate service health before changing production traffic."
        elif "db" in lower_logs or "database" in lower_logs:
            root_cause = "The logs point to a database or persistence layer issue blocking normal service behavior."
            suggested_resolution = "Validate database availability and connectivity, then address the confirmed persistence failure."
        else:
            root_cause = "Evidence is insufficient to confidently attribute a single root cause. Human investigation is required."
            suggested_resolution = "Collect additional evidence and validate the suspected dependency before applying a recovery action."

        memory_impact = "No previous incident was available in Hindsight memory."
        if historical_matches:
            prior = historical_matches[0]
            lookup = prior.get("text") or prior.get("summary") or prior.get("incident_id") or "previous incident"
            memory_impact = f"Previous incident {prior.get('incident_id', 'unknown')} influenced the recommendation because it matched the same HTTP 502/gateway pattern and service symptoms: {lookup}."

        return {
            "summary": incident.get("summary") or "Production incident analysis",
            "root_cause": root_cause,
            "supporting_evidence": [
                line.strip() for line in logs.splitlines() if line.strip() and any(token in line.lower() for token in ["error", "critical", "timeout", "502", "gateway"])][:4],
            "affected_services": services or ["unknown"],
            "severity_assessment": "High" if "critical" in lower_logs or "502" in lower_logs else "Medium",
            "recommended_investigation_steps": [
                "Validate upstream dependency health and the gateway response path.",
                "Review recent deploys or config changes affecting the affected service.",
                "Confirm whether the failure is isolated to one region or environment before broader mitigation.",
            ],
            "suggested_resolution": suggested_resolution,
            "confidence": 0.72,
            "human_investigation_required": True,
            "facts": [
                f"Observed service scope: {', '.join(services) if services else 'not specified'}",
                f"Evidence captured: {logs[:180]}",
            ],
            "hypotheses": [root_cause],
            "memory_impact": memory_impact,
            "provider": "fallback",
        }
