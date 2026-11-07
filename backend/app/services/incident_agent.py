from __future__ import annotations

import json
import re
from typing import Any

from app.services.groq_ai import GroqIncidentAnalyzer
from app.services.log_analysis import analyze_logs
from app.services.memory import HindsightMemoryService


def determine_severity(score: float, incident_severity: str) -> str:
    if score >= 0.8 or incident_severity.upper() == "CRITICAL":
        return "Critical"
    if score >= 0.6 or incident_severity.upper() == "HIGH":
        return "High"
    if score >= 0.4 or incident_severity.upper() == "MEDIUM":
        return "Medium"
    return "Low"


async def investigate_incident(incident: dict[str, Any], memory: HindsightMemoryService) -> dict[str, Any]:
    services = [item.strip() for item in str(incident.get("services") or "").split(",") if item.strip()]
    log_summary = analyze_logs(str(incident.get("logs") or ""), services)
    historical_matches = await memory.recall_similar_incidents(incident)
    historical_matches = memory.rank_relevant_matches(incident, historical_matches)
    current_incident_id = str(incident.get("incident_id") or "")
    successful_resolutions = [
        item
        for item in await memory.retrieve_successful_resolutions(services)
        if item.get("incident_id") != current_incident_id
    ]
    successful_resolutions = memory.rank_relevant_matches(incident, successful_resolutions)
    failed_attempts = [
        item
        for item in await memory.retrieve_failed_attempts(services)
        if item.get("incident_id") != current_incident_id
    ]
    failed_attempts = memory.rank_relevant_matches(incident, failed_attempts)
    outcome_by_incident: dict[str, dict[str, Any]] = {}
    for outcome in [*successful_resolutions, *failed_attempts]:
        incident_id = str(outcome.get("incident_id") or "")
        if incident_id and incident_id not in outcome_by_incident:
            outcome_by_incident[incident_id] = outcome
    historical_incident_ids = {
        str(match.get("incident_id") or "")
        for match in historical_matches
    }
    for incident_id, outcome in outcome_by_incident.items():
        if incident_id and incident_id not in historical_incident_ids:
            historical_matches.append(
                {
                    **outcome,
                    "reason": (
                        "Relevant prior incident with a recorded successful outcome."
                        if outcome.get("success") is True
                        else "Relevant prior incident with a recorded unsuccessful outcome."
                    ),
                }
            )
    historical_matches = [
        {
            **match,
            **(
                {
                    "success": outcome_by_incident[str(match.get("incident_id"))].get("success"),
                    "resolution": outcome_by_incident[str(match.get("incident_id"))].get("resolution"),
                    "outcome": outcome_by_incident[str(match.get("incident_id"))].get("outcome"),
                }
                if str(match.get("incident_id") or "") in outcome_by_incident
                else {}
            ),
        }
        for match in historical_matches
    ]

    error_messages = [item["message"] for item in log_summary["error_events"]]
    http_errors = log_summary["http_errors"]
    stack_traces = log_summary["stack_traces"]
    service_names = log_summary["service_names"] or services

    root_cause_candidates = []
    lower_logs = str(incident.get("logs") or "").lower()
    listener_port = re.search(r"(?:service listens on port|listens on port|listening on port)\s*(\d+)", lower_logs)
    nginx_port_mismatch = (
        "nginx" in lower_logs
        and "upstream" in lower_logs
        and "port" in lower_logs
        and listener_port is not None
        and any(term in lower_logs for term in ("incorrect", "mismatch", "but service listens", "expected port"))
    )
    upstream_url = re.search(r"\bupstream:\s*[\"']?(https?://[^/\"'\s,]+)", str(incident.get("logs") or ""), re.IGNORECASE)
    upstream_target = upstream_url.group(1).removeprefix("http://").removeprefix("https://") if upstream_url else ""
    nginx_in_scope = "nginx" in lower_logs or any("nginx" in service.lower() for service in service_names)
    nginx_upstream_refused = (
        nginx_in_scope
        and "upstream" in lower_logs
        and "connection refused" in lower_logs
    )
    if nginx_port_mismatch:
        root_cause_candidates.append("Nginx is forwarding to an incorrect upstream port, which does not match the application listener.")
    if nginx_upstream_refused:
        target_detail = f" at {upstream_target}" if upstream_target else ""
        root_cause_candidates.insert(
            0,
                f"Connection refused while Nginx connected to its upstream{target_detail}. The application is not accepting connections at this endpoint; the log does not establish that the port is incorrect.",
        )
    elif any("connection" in msg.lower() for msg in error_messages):
        root_cause_candidates.append("Network connectivity or dependency timeout is preventing downstream service access.")
    if any("database" in msg.lower() or "sql" in msg.lower() for msg in error_messages):
        root_cause_candidates.append("A database or persistence layer issue is producing repeated failures.")
    if any("timeout" in msg.lower() for msg in error_messages):
        root_cause_candidates.append("Timeouts are occurring under load, likely due to saturation or slow dependency responses.")
    if any(status.startswith("5") for status in http_errors):
        root_cause_candidates.append("The application is returning server-side failures indicating a backend or infrastructure fault.")
    if any("cache" in name.lower() for name in service_names):
        root_cause_candidates.append("A cache inconsistency or invalidation issue is likely affecting the service flow.")

    evidence = []
    if error_messages:
        evidence.extend(error_messages[:3])
    if http_errors:
        evidence.extend(http_errors[:3])
    if stack_traces:
        evidence.append(stack_traces[0])

    severity_score = 0.5
    if "critical" in incident.get("severity", "").lower() or "critical" in " ".join(error_messages).lower():
        severity_score += 0.2
    if http_errors:
        severity_score += 0.1
    if stack_traces:
        severity_score += 0.15
    severity_score = min(severity_score, 0.95)

    recommended_steps = [
        "Validate the first error signature against the current service health checks.",
        "Confirm whether dependencies are healthy and reachable from the affected services.",
        "Inspect recent deploys or config changes related to the impacted services.",
        "Verify whether the failure is isolated to one region or environment before approving a rollout reversal.",
    ]
    if service_names:
        recommended_steps.insert(0, f"Prioritize the failing services: {', '.join(service_names)}.")
    if nginx_port_mismatch:
        recommended_steps.insert(0, "Compare the Nginx upstream port with the application's listening port and correct only the mismatched upstream setting.")
    if nginx_upstream_refused:
        recommended_steps = [
            f"Check whether the application listener is running at {upstream_target or 'the configured Nginx upstream'}.",
            "Verify the application's bind address and reachability from the Nginx host.",
        ]

    suggested_resolution = (
        f"Connection refused means the application is not accepting connections at {upstream_target or 'the configured Nginx upstream'}. Check the application listener, bind address, and reachability from the Nginx host; the log does not establish that the port is incorrect."
        if nginx_upstream_refused
        else
        f"Set the Nginx upstream port to {listener_port.group(1)} to match the application's listening port, validate the configuration, reload Nginx, and confirm successful HTTP responses."
        if nginx_port_mismatch and listener_port
        else "Set the Nginx upstream port to match the application's listening port, validate the configuration, reload Nginx, and confirm successful HTTP responses."
        if nginx_port_mismatch
        else "Verify upstream and dependency health, inspect recent configuration or deployment changes, and confirm the failing component before selecting a recovery action."
    )
    if successful_resolutions and not nginx_upstream_refused:
        candidate_resolution = successful_resolutions[0].get("resolution")
        if candidate_resolution:
            suggested_resolution = candidate_resolution
    failed_resolution_notes = []
    for failed in failed_attempts:
        resolution = failed.get("resolution") or "the recorded resolution"
        outcome = failed.get("outcome") or failed.get("summary") or "it did not resolve the incident"
        failed_resolution_notes.append({"resolution": resolution, "outcome": outcome, "incident_id": failed.get("incident_id")})
    if failed_resolution_notes and not nginx_upstream_refused:
        failed = failed_resolution_notes[0]
        suggested_resolution += f" Do not repeat the unsuccessful action '{failed['resolution']}' from {failed.get('incident_id') or 'a prior incident'}; its recorded outcome was: {failed['outcome']}."

    confidence = round(severity_score, 2)
    human_required = bool(confidence < 0.75 or not root_cause_candidates)

    memory_references = [
        {
            "incident_id": match.get("incident_id") or match.get("id") or "unknown",
            "title": match.get("title") or "Historical incident",
            "summary": match.get("summary") or "",
            "reason": match.get("reason") or "Semantic match from Hindsight/local memory.",
            "source": match.get("source") or memory.mode,
            "success": match.get("success"),
            "resolution": match.get("resolution"),
            "outcome": match.get("outcome"),
        }
        for match in historical_matches[:5]
    ]

    if not historical_matches:
        memory_references = []
    provider_memory = list(historical_matches)
    provider_memory.extend(
        {
            **item,
            "title": f"Verified successful resolution for {item.get('incident_id') or 'a prior incident'}",
            "reason": "This resolution was explicitly recorded as successful.",
        }
        for item in successful_resolutions
    )
    provider_memory.extend(
        {
            **item,
            "title": f"Failed resolution for {item.get('incident_id') or 'a prior incident'}",
            "reason": "This resolution was explicitly recorded as unsuccessful.",
        }
        for item in failed_attempts
    )
    historical_incident_details = []
    for match in historical_matches:
        raw_text = match.get("text") or ""
        try:
            details = json.loads(raw_text)
        except (TypeError, json.JSONDecodeError):
            details = {}
        if isinstance(details, dict):
            historical_incident_details.append(details)

    analyzer = GroqIncidentAnalyzer()
    ai_payload = await analyzer.analyze(
        {**incident, "supporting_evidence": evidence, "affected_services": service_names},
        provider_memory,
    )

    if not isinstance(ai_payload, dict):
        ai_payload = {}

    use_groq_result = ai_payload.get("provider") == "groq"
    local_root_cause = root_cause_candidates[0] if root_cause_candidates else "Evidence is insufficient to confidently attribute a single root cause. The observed logs suggest a production issue that requires human investigation."
    final_root_cause = (ai_payload.get("root_cause") if use_groq_result else None) or local_root_cause
    final_resolution = (ai_payload.get("suggested_resolution") if use_groq_result else None) or suggested_resolution
    final_steps = ai_payload.get("recommended_investigation_steps") or recommended_steps
    if nginx_upstream_refused:
        final_root_cause = local_root_cause
        final_resolution = suggested_resolution
        final_steps = recommended_steps
    final_confidence = float(ai_payload.get("confidence", confidence))
    final_human_required = bool(ai_payload.get("human_investigation_required", human_required))
    final_evidence = ai_payload.get("supporting_evidence") or evidence
    final_severity = ai_payload.get("severity_assessment") or determine_severity(final_confidence, str(incident.get("severity") or "High"))
    final_facts = ai_payload.get("facts") or [
        f"Incident severity was recorded as {incident.get('severity', 'Unknown')}",
        f"Affected services: {', '.join(service_names) if service_names else 'Not specified'}",
    ]
    if error_messages:
        final_facts.append(f"Observed log signatures: {'; '.join(error_messages[:2])}")
    if http_errors:
        final_facts.append(f"HTTP error signatures: {', '.join(http_errors)}")
    final_hypotheses = ai_payload.get("hypotheses") or (root_cause_candidates[:2] if root_cause_candidates else ["A dependency latency or configuration drift may be causing the issue, but evidence is incomplete."])
    final_summary = ai_payload.get("summary") or incident.get("summary", "Production incident requires triage.")

    if successful_resolutions:
        prior = successful_resolutions[0]
        prior_id = prior.get("incident_id") or "a prior incident"
        memory_impact = (
            f"Prior incident {prior_id} was recorded as successfully resolved; "
            "use its recorded resolution as a lead and verify the current listener and reachability."
        )
    elif memory_references:
        memory_impact = f"{len(memory_references)} relevant prior incident(s) matched. Closest match: {memory_references[0]['title']}."
    elif successful_resolutions or failed_attempts:
        memory_impact = "Relevant recorded resolution outcomes were considered."
    else:
        memory_impact = "No sufficiently relevant prior incident or resolution was available."

    result = {
        "status": "investigated",
        "incident_id": str(incident.get("incident_id", "unknown")),
        "summary": final_summary,
        "root_cause": final_root_cause,
        "supporting_evidence": final_evidence,
        "affected_services": service_names,
        "severity_assessment": final_severity,
        "recommended_investigation_steps": final_steps,
        "suggested_resolution": final_resolution,
        "confidence": round(final_confidence, 2),
        "human_investigation_required": final_human_required,
        "historical_matches": historical_matches,
        "memory_status": memory.mode,
        "memory_source": memory.mode,
        "memory_references": memory_references,
        "memory_impact": memory_impact,
        "memory_influenced": bool(provider_memory),
        "successful_resolution_memory": successful_resolutions[0] if successful_resolutions else None,
        "failed_resolution_memories": failed_resolution_notes,
        "historical_incident_details": historical_incident_details,
        "facts": final_facts,
        "hypotheses": final_hypotheses,
        "provider": ai_payload.get("provider", "fallback"),
    }
    return result
