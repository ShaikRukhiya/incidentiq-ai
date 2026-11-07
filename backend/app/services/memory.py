from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from typing import Any

from app.config import settings
from app.db import insert_memory_event, read_memory_events


class HindsightMemoryService:
    def __init__(self) -> None:
        self.base_url = settings.hindsight_base_url or os.getenv("HINDSIGHT_BASE_URL")
        self.api_key = settings.hindsight_api_key or os.getenv("HINDSIGHT_API_KEY")
        self.bank_id = settings.hindsight_bank_id
        self.configured = bool(self.base_url)
        self.available = False
        self.mode = "local-fallback"
        self.connection_error: str | None = "not-checked" if self.configured else "not-configured"
        self._hindsight_client: Any = None
        self._health_client: Any = None

        if self.configured:
            try:
                self._hindsight_client = self._create_hindsight_client()
            except Exception as exc:
                self.connection_error = type(exc).__name__

    def _create_hindsight_client(self, timeout: float = 300.0) -> Any:
        from hindsight_client import Hindsight

        return Hindsight(
            base_url=self.base_url,
            api_key=self.api_key,
            timeout=timeout,
            max_attempts=1,
        )

    def _mark_hindsight_available(self) -> None:
        self.available = True
        self.mode = "hindsight"
        self.connection_error = None

    async def check_connection(self) -> dict[str, Any]:
        if not self.base_url:
            self.available = False
            self.mode = "local-fallback"
            self.connection_error = "not-configured"
            return {
                "configured": False,
                "reachable": False,
                "mode": self.mode,
                "bank_id": self.bank_id,
                "error": self.connection_error,
            }
        try:
            client = self._health_client or self._create_hindsight_client(timeout=5.0)
            await asyncio.to_thread(client.get_version)
            self._health_client = client
            self._mark_hindsight_available()
        except Exception as exc:
            self._use_fallback(exc)
        return {
            "configured": self.configured,
            "reachable": self.available,
            "mode": self.mode,
            "bank_id": self.bank_id,
            "error": self.connection_error,
        }

    def _normalize_recall_results(self, response: Any) -> list[dict[str, Any]]:
        if response is None:
            return []
        if isinstance(response, dict):
            results = response.get("results") or []
        else:
            results = getattr(response, "results", []) or []
        normalized = []
        for item in results:
            if isinstance(item, dict):
                payload = item
            elif hasattr(item, "model_dump"):
                payload = item.model_dump()
            else:
                payload = item.__dict__ if hasattr(item, "__dict__") else {"text": str(item)}
            text = payload.get("text") or payload.get("summary") or ""
            try:
                details = json.loads(text)
            except (TypeError, json.JSONDecodeError):
                details = {}
            if not isinstance(details, dict):
                details = {}
            metadata = payload.get("metadata") or {}
            if not isinstance(metadata, dict):
                metadata = {}
            tags = payload.get("tags") or []
            category = metadata.get("category") or details.get("category")
            if not category:
                if "resolution" in tags:
                    category = "resolution"
                elif "incident" in tags:
                    category = "incident"
            if category:
                metadata = {**metadata, "category": category}
            success_value = metadata.get("success", details.get("success"))
            if success_value is None:
                if "successful" in tags:
                    success_value = True
                elif "failed" in tags:
                    success_value = False
            title = details.get("title") or payload.get("title") or payload.get("context")
            incident_id = (
                metadata.get("incident_id")
                or payload.get("incident_id")
                or details.get("incident_id")
            )
            if not incident_id:
                id_match = re.search(r"\bINC-[A-Z0-9-]+\b", str(text), re.IGNORECASE)
                incident_id = id_match.group(0).upper() if id_match else None
            summary = (
                details.get("summary")
                or details.get("root_cause")
                or payload.get("summary")
                or next((line.strip() for line in str(details.get("logs") or "").splitlines() if line.strip()), "")
            )
            normalized.append(
                {
                    "incident_id": incident_id,
                    "title": title or "Historical incident",
                    "summary": str(summary or "Historical incident memory")[:240],
                    "reason": "Semantic match from Hindsight memory bank.",
                    "source": "hindsight",
                    "text": text,
                    "metadata": metadata,
                    "success": self._parse_success(success_value),
                    "resolution": (
                        details.get("recommended_resolution")
                        or details.get("suggested_resolution")
                        or details.get("resolution")
                        or metadata.get("resolution")
                    ),
                    "outcome": (
                        details.get("outcome")
                        or metadata.get("outcome")
                        or (text if category == "resolution" else None)
                    ),
                    "id": payload.get("id") or payload.get("document_id"),
                }
            )
        return normalized

    @staticmethod
    def _incident_query(incident: dict[str, Any]) -> str:
        services = incident.get("affected_services") or incident.get("services") or []
        if isinstance(services, str):
            services_text = services
        else:
            services_text = " ".join(str(service) for service in services)
        return " ".join(
            str(value)
            for value in (
                incident.get("title"),
                incident.get("summary"),
                incident.get("root_cause"),
                services_text,
                incident.get("logs"),
            )
            if value
        )

    @classmethod
    def _relevance_score(
        cls,
        query: str,
        candidate: dict[str, Any],
        query_services: set[str] | None = None,
    ) -> tuple[float, list[str]]:
        def tokens(value: str) -> set[str]:
            normalized = re.sub(
                r"\b\d{4}[/-]\d{1,2}[/-]\d{1,2}(?:[ T]\d{1,2}:\d{2}:\d{2})?\b",
                " ",
                value.lower(),
            )
            normalized = re.sub(r"\b\d{1,2}:\d{2}:\d{2}\b", " ", normalized)
            normalized = re.sub(r"https?://[^/\s\"']+(?:/[^\s\"']*)?", " ", normalized)
            return {
                token
                for token in re.findall(r"[a-z0-9]+", normalized)
                if not token.isdigit() or token in {"200", "400", "401", "403", "404", "408", "429", "500", "502", "503", "504"}
            }

        query_tokens = tokens(query)
        candidate_text = " ".join(
            str(candidate.get(field) or "")
            for field in ("title", "summary", "text", "resolution", "outcome")
        )
        try:
            details = json.loads(str(candidate.get("text") or ""))
        except (TypeError, json.JSONDecodeError):
            details = {}
        if isinstance(details, dict):
            candidate_text += " " + " ".join(
                str(details.get(field) or "")
                for field in ("title", "summary", "root_cause", "services", "logs", "outcome", "recommended_resolution")
            )
        candidate_tokens = tokens(candidate_text)
        shared = query_tokens & candidate_tokens
        stop_words = {
            "a", "an", "and", "are", "as", "at", "be", "by", "for", "from",
            "in", "is", "it", "of", "on", "or", "the", "to", "was", "while",
            "with", "error", "failed", "failure", "http",
        }
        specific_tokens = shared - stop_words
        details_services = details.get("services", []) if isinstance(details, dict) else []
        if isinstance(details_services, str):
            details_services = re.split(r"[,;\s]+", details_services)
        incident_services = candidate.get("services") or []
        if isinstance(incident_services, str):
            incident_services = re.split(r"[,;\s]+", incident_services)
        candidate_services = {
            token
            for service in [*(details_services or []), *incident_services]
            for token in re.findall(r"[a-z0-9]+", str(service).lower())
            if token not in {"api", "app", "application", "backend", "frontend", "service", "web"}
        }
        context_anchors = {"nginx", "upstream", "proxy"}
        if not (
            specific_tokens & context_anchors
            or (query_services or set()) & candidate_services
        ):
            return 0.0, sorted(specific_tokens)

        weights = {
            "nginx": 4.0,
            "upstream": 3.5,
            "refused": 5.0,
            "refusal": 5.0,
            "502": 2.5,
            "connection": 1.5,
            "gateway": 0.5,
            "api": 0.4,
        }
        score = sum(
            weights.get(token, 1.0)
            for token in specific_tokens
        )
        if "connection refused" in query.lower() and "connection refused" in candidate_text.lower():
            score += 2.0
        if "upstream" in query.lower() and "upstream" in candidate_text.lower():
            score += 1.0
        return score, sorted(
            specific_tokens,
            key=lambda token: weights.get(token, 1.0),
            reverse=True,
        )

    @classmethod
    def rank_relevant_matches(
        cls,
        incident: dict[str, Any],
        matches: list[dict[str, Any]],
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        query = cls._incident_query(incident)
        raw_services = incident.get("affected_services") or incident.get("services") or []
        if isinstance(raw_services, str):
            raw_services = re.split(r"[,;\s]+", raw_services)
        query_services = {
            token
            for service in raw_services
            for token in re.findall(r"[a-z0-9]+", str(service).lower())
            if token not in {"api", "app", "application", "backend", "frontend", "service", "web"}
        }
        ranked: list[tuple[float, dict[str, Any], list[str]]] = []
        best_by_id: dict[str, tuple[float, dict[str, Any], list[str]]] = {}
        for match in matches:
            score, matched_terms = cls._relevance_score(query, match, query_services)
            if score < 5.0:
                continue
            identity = str(match.get("incident_id") or match.get("id") or "")
            if not identity:
                identity = re.sub(r"\W+", " ", str(match.get("text") or match.get("summary") or "").lower()).strip()
            candidate = (score, match, matched_terms)
            previous = best_by_id.get(identity)
            if previous is None or score > previous[0]:
                best_by_id[identity] = candidate
        ranked = sorted(best_by_id.values(), key=lambda item: item[0], reverse=True)
        results = []
        for score, match, matched_terms in ranked[:limit]:
            result = dict(match)
            result["relevance_score"] = round(score, 2)
            if matched_terms:
                result["reason"] = f"Matched relevant evidence: {', '.join(matched_terms[:5])}."
            results.append(result)
        return results

    @staticmethod
    def _parse_success(value: Any) -> bool | None:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            if value.lower() == "true":
                return True
            if value.lower() == "false":
                return False
        return None

    def _use_fallback(self, exc: Exception) -> None:
        self.connection_error = type(exc).__name__
        self.mode = "local-fallback"
        self.available = False

    def _fallback_query(self, query: str, category: str) -> list[dict[str, Any]]:
        events = read_memory_events(category=category, bank_id=self.bank_id)
        if not events:
            return []
        matches = []
        for event in events:
            content = event.get("content") or ""
            try:
                details = json.loads(content)
            except json.JSONDecodeError:
                details = {}
            if isinstance(details, dict):
                summary = (
                    details.get("summary")
                    or details.get("root_cause")
                    or next((line.strip() for line in str(details.get("logs") or "").splitlines() if line.strip()), "")
                )
                title = details.get("title") or event.get("title") or "Local memory"
            else:
                summary = ""
                title = event.get("title") or "Local memory"
            if content:
                matches.append(
                    {
                        "incident_id": event.get("incident_id"),
                        "title": title,
                        "summary": str(summary or title)[:240],
                        "reason": f"Local memory match for {category}.",
                        "source": "local-fallback",
                        "text": content,
                        "metadata": {"category": category},
                        "resolution": event.get("resolution"),
                        "success": bool(event.get("success")) if category == "resolution" else None,
                        "id": str(event.get("id")),
                    }
                )
        return matches

    def _fallback_resolutions(self, success: bool, services: list[str] | None = None) -> list[dict[str, Any]]:
        requested_services = {service.strip().lower() for service in services or [] if service.strip()}
        matches = []
        for event in read_memory_events(category="resolution", bank_id=self.bank_id):
            if bool(event.get("success")) is not success:
                continue
            try:
                details = json.loads(event.get("content") or "{}")
            except json.JSONDecodeError:
                details = {}
            raw_services = details.get("services", [])
            if isinstance(raw_services, str):
                raw_services = raw_services.split(",")
            event_services = {str(service).strip().lower() for service in raw_services}
            if requested_services and event_services and requested_services.isdisjoint(event_services):
                continue
            matches.append(
                {
                    "incident_id": event.get("incident_id"),
                    "title": details.get("title") or event.get("title") or "Recorded resolution",
                    "summary": details.get("outcome") or event.get("content") or "",
                    "text": event.get("content") or "",
                    "resolution": details.get("recommended_resolution") or event.get("resolution"),
                    "outcome": details.get("outcome"),
                    "success": success,
                    "source": "local-fallback",
                    "reason": "Recorded resolution outcome from local memory.",
                    "metadata": {"success": "true" if success else "false"},
                    "id": str(event.get("id")),
                }
            )
        return matches

    async def retain_incident(self, incident: dict[str, Any]) -> str:
        content = json.dumps(incident, sort_keys=True)
        if self.configured:
            try:
                client = self._hindsight_client or self._create_hindsight_client()
                response = await client.aretain(
                    bank_id=self.bank_id,
                    content=content,
                    document_id=f"incidentiq:incident:{incident.get('incident_id') or 'unknown'}",
                    metadata={
                        "incident_id": str(incident.get("incident_id") or "unknown"),
                        "category": "incident",
                    },
                    tags=["incident", "sre"],
                    update_mode="replace",
                )
                if not response.success:
                    raise RuntimeError("Hindsight incident retain was not successful")
                self._hindsight_client = client
                self._mark_hindsight_available()
                return "hindsight"
            except Exception as exc:
                self._use_fallback(exc)
        insert_memory_event(
            incident_id=str(incident.get("incident_id") or "unknown"),
            title=str(incident.get("title", "incident")),
            content=content,
            bank_id=self.bank_id,
            category="incident",
            resolution=incident.get("recommended_resolution"),
        )
        self.mode = "local-fallback"
        self.available = False
        return "local-fallback"

    async def recall_similar_incidents(self, incident: dict[str, Any]) -> list[dict[str, Any]]:
        incident_id = str(incident.get("incident_id") or "")
        query = self._incident_query(incident)
        if self.configured:
            try:
                client = self._hindsight_client or self._create_hindsight_client()
                response = await client.arecall(
                    bank_id=self.bank_id,
                    query=query,
                    budget="mid",
                    include_chunks=False,
                )
                self._hindsight_client = client
                self._mark_hindsight_available()
                results = self._normalize_recall_results(response)
                results = [
                    item
                    for item in results
                    if (item.get("metadata") or {}).get("category") in (None, "incident")
                ]
                return self.rank_relevant_matches(
                    incident,
                    [item for item in results if item.get("incident_id") != incident_id],
                )
            except Exception as exc:
                self._use_fallback(exc)
        results = self._fallback_query(query, category="incident")
        return self.rank_relevant_matches(
            incident,
            [item for item in results if item.get("incident_id") != incident_id],
        )

    async def update_incident_outcome(
        self,
        incident_id: str,
        outcome: str,
        success: bool,
        services: list[str] | None = None,
        recommended_resolution: str | None = None,
        incident: dict[str, Any] | None = None,
    ) -> str:
        details = {
            "incident_id": incident_id,
            "title": (incident or {}).get("title"),
            "summary": (incident or {}).get("summary"),
            "root_cause": (incident or {}).get("root_cause"),
            "logs": (incident or {}).get("logs"),
            "outcome": outcome,
            "success": success,
            "services": services or (incident or {}).get("services") or [],
            "recommended_resolution": recommended_resolution,
        }
        outcome_hash = hashlib.sha256(
            json.dumps(details, sort_keys=True).encode("utf-8")
        ).hexdigest()
        if self.configured:
            try:
                client = self._hindsight_client or self._create_hindsight_client()
                response = await client.aretain(
                    bank_id=self.bank_id,
                    content=json.dumps(details, sort_keys=True),
                    document_id=(
                        f"incidentiq:resolution:{incident_id}:"
                        f"{'success' if success else 'failure'}:{outcome_hash}"
                    ),
                    metadata={
                        "incident_id": incident_id,
                        "success": "true" if success else "false",
                        "category": "resolution",
                        "services": ",".join(services or []),
                        "resolution": recommended_resolution or "",
                        "outcome": outcome,
                    },
                    tags=["resolution", "successful" if success else "failed"],
                    update_mode="replace",
                )
                if not response.success:
                    raise RuntimeError("Hindsight resolution retain was not successful")
                self._hindsight_client = client
                self._mark_hindsight_available()
                return "hindsight"
            except Exception as exc:
                self._use_fallback(exc)
        insert_memory_event(
            incident_id=incident_id,
            title=str((incident or {}).get("title") or "Outcome update"),
            content=json.dumps(details, sort_keys=True),
            bank_id=self.bank_id,
            category="resolution",
            resolution=recommended_resolution or outcome,
            success=success,
        )
        self.mode = "local-fallback"
        self.available = False
        return "local-fallback"

    async def retrieve_successful_resolutions(self, services: list[str] | None = None) -> list[dict[str, Any]]:
        service_text = ", ".join(services or [])
        query = f"successful resolution for {service_text or 'incident operations'}"
        if self.configured:
            try:
                client = self._hindsight_client or self._create_hindsight_client()
                response = await client.arecall(
                    bank_id=self.bank_id,
                    query=query,
                    budget="mid",
                    tags=["resolution", "successful"],
                    tags_match="all_strict",
                )
                self._hindsight_client = client
                self._mark_hindsight_available()
                results = self._normalize_recall_results(response)
                return [item for item in results if item.get("success") is True]
            except Exception as exc:
                self._use_fallback(exc)
        return self._fallback_resolutions(success=True, services=services)

    async def retrieve_failed_attempts(self, services: list[str] | None = None) -> list[dict[str, Any]]:
        service_text = ", ".join(services or [])
        query = f"failed resolution attempts for {service_text or 'incident operations'}"
        if self.configured:
            try:
                client = self._hindsight_client or self._create_hindsight_client()
                response = await client.arecall(
                    bank_id=self.bank_id,
                    query=query,
                    budget="mid",
                    tags=["resolution", "failed"],
                    tags_match="all_strict",
                )
                self._hindsight_client = client
                self._mark_hindsight_available()
                results = self._normalize_recall_results(response)
                return [item for item in results if item.get("success") is False]
            except Exception as exc:
                self._use_fallback(exc)
        return self._fallback_resolutions(success=False, services=services)
