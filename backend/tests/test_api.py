from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from contextlib import closing
from types import SimpleNamespace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app
from app.services import groq_ai
from app.services.incident_agent import investigate_incident
from app.services.memory import HindsightMemoryService

client = TestClient(app)


@pytest.fixture(autouse=True)
def isolate_database(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "incidentiq-test.db")
    monkeypatch.setattr("app.config.settings.hindsight_base_url", None)
    monkeypatch.setattr("app.config.settings.hindsight_api_key", None)
    monkeypatch.setattr("app.config.settings.groq_api_key", None)
    monkeypatch.setattr("app.config.settings.incidentiq_approval_token", None)
    monkeypatch.delenv("HINDSIGHT_BASE_URL", raising=False)
    monkeypatch.delenv("HINDSIGHT_API_KEY", raising=False)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    db.init_db()
    monkeypatch.setattr("app.main.memory", HindsightMemoryService())
    monkeypatch.setattr(groq_ai, "_last_request_succeeded", None)


def test_health_and_diagnostics_report_real_provider_state() -> None:
    health = client.get("/health")
    assert health.status_code == 200
    assert health.json()["hindsight"] == "local-fallback"

    diagnostics = client.get("/api/diagnostics")
    assert diagnostics.status_code == 200
    payload = diagnostics.json()
    assert payload["memory"]["mode"] == "local-fallback"
    assert payload["memory"]["available"] is False
    assert payload["llm"]["connected"] is False


def test_incident_workflow() -> None:
    response = client.post(
        "/api/incidents",
        json={
            "title": "Checkout API latency spike",
            "summary": "Users are seeing timeouts while checking out orders.",
            "severity": "High",
            "services": "api,payment",
            "source": "paste",
            "logs": "ERROR api timeout after 30s\nCRITICAL payment db connection refused\nWARNING cache invalidation issue",
        },
    )
    assert response.status_code == 200, response.text
    incident_id = response.json()["incident_id"]

    analysis = client.post(
        f"/api/incidents/{incident_id}/analyze",
        json={
            "logs": "ERROR api timeout after 30s\nCRITICAL payment db connection refused\nWARNING cache invalidation issue",
            "services": "api,payment",
            "title": "Checkout API latency spike",
        },
    )
    assert analysis.status_code == 200, analysis.text
    payload = analysis.json()
    assert payload["root_cause"]
    assert payload["memory_status"] == "local-fallback"
    assert payload["provider"] == "fallback"

    dashboard = client.get("/api/dashboard")
    assert dashboard.status_code == 200
    assert dashboard.json()["summary"]["total_incidents"] == 1


def test_nginx_connection_refused_api_response_identifies_upstream_endpoint() -> None:
    logs = (
        '2026/11/07 09:30:00 [error] connect() failed (111: Connection refused) '
        'while connecting to upstream, upstream: "http://127.0.0.1:8001/"'
    )
    incident = client.post(
        "/api/incidents",
        json={
            "title": "Nginx HTTP 502 Bad Gateway",
            "summary": "Nginx cannot connect to the application.",
            "severity": "High",
            "services": "nginx, web-application",
            "source": "manual",
            "logs": logs,
        },
    )
    assert incident.status_code == 200, incident.text
    incident_id = incident.json()["incident_id"]

    response = client.post(f"/api/incidents/{incident_id}/analyze", json={})
    assert response.status_code == 200, response.text
    payload = response.json()

    assert "connection refused" in payload["root_cause"].lower()
    assert "127.0.0.1" in payload["root_cause"]
    assert "8001" in payload["root_cause"]
    assert "not accepting connections" in payload["root_cause"].lower()
    assert "does not establish that the port is incorrect" in payload["root_cause"].lower()
    assert "forwarding to an incorrect upstream port" not in payload["root_cause"].lower()
    assert "application listener" in payload["suggested_resolution"].lower()
    assert "bind address" in payload["suggested_resolution"].lower()
    assert "reachability" in payload["suggested_resolution"].lower()
    assert "8001" in payload["suggested_resolution"]
    assert "does not establish that the port is incorrect" in payload["suggested_resolution"].lower()

    stored = client.get(f"/api/incidents/{incident_id}")
    assert stored.status_code == 200
    assert stored.json()["root_cause"] == payload["root_cause"]
    assert stored.json()["recommended_resolution"] == payload["suggested_resolution"]


def test_nginx_incident_learning_survives_restart_and_surfaces_successful_outcome(
    tmp_path, monkeypatch
) -> None:
    database_path = tmp_path / "nginx-learning.db"
    database_url = f"sqlite:///{database_path.as_posix()}"
    monkeypatch.setattr("app.config.settings.database_url", database_url)
    db.init_db()
    logs_a = (
        '2026/11/07 09:30:00 [error] connect() failed (111: Connection refused) '
        'while connecting to upstream, upstream: "http://127.0.0.1:8001/"'
    )
    incident_a = client.post(
        "/api/incidents",
        json={
            "title": "Nginx HTTP 502 Bad Gateway",
            "summary": "Nginx cannot connect to the application service.",
            "severity": "High",
            "services": "nginx,web-application",
            "logs": logs_a,
        },
    )
    assert incident_a.status_code == 200, incident_a.text
    incident_a_id = incident_a.json()["incident_id"]

    analysis_a = client.post(f"/api/incidents/{incident_a_id}/analyze", json={})
    assert analysis_a.status_code == 200, analysis_a.text
    assert analysis_a.json()["memory_status"] == "local-fallback"

    recorded_outcome = (
        "The application listener was started and an HTTP 200 response was verified."
    )
    outcome_response = client.post(
        f"/api/incidents/{incident_a_id}/outcome",
        json={
            "status": "resolved",
            "success": True,
            "outcome": recorded_outcome,
        },
    )
    assert outcome_response.status_code == 200, outcome_response.text

    restart_env = os.environ.copy()
    restart_env["DATABASE_URL"] = database_url
    restart_env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    restart_code = """
import json
import sys
from app.db import get_incident, read_memory_events

incident_id = sys.argv[1]
incident = get_incident(incident_id)
events = read_memory_events(category="resolution", bank_id="incidentiq-memory")
resolution_events = [event for event in events if event["incident_id"] == incident_id]
print(json.dumps({"incident": incident, "resolution_events": resolution_events}))
"""
    restarted = subprocess.run(
        [sys.executable, "-c", restart_code, incident_a_id],
        cwd=Path(__file__).resolve().parents[1],
        env=restart_env,
        check=True,
        capture_output=True,
        text=True,
    )
    persisted = json.loads(restarted.stdout)
    assert persisted["incident"]["status"] == "resolved"
    assert persisted["incident"]["outcome_notes"] == recorded_outcome
    assert any(event["success"] == 1 for event in persisted["resolution_events"])
    assert any(
        json.loads(event["content"])["outcome"] == recorded_outcome
        for event in persisted["resolution_events"]
    )
    persisted_resolution = next(
        json.loads(event["content"])
        for event in persisted["resolution_events"]
        if json.loads(event["content"])["outcome"] == recorded_outcome
    )
    assert persisted_resolution["title"] == "Nginx HTTP 502 Bad Gateway"
    assert "connect() failed" in persisted_resolution["logs"]

    async def add_newer_unrelated_memory() -> None:
        for index in range(6):
            incident_id = f"INC-UNRELATED-NGINX-{index}"
            await app_main_memory.retain_incident(
                {
                    "incident_id": incident_id,
                    "title": "Nginx static asset cache refresh",
                    "summary": "Static assets were served from a stale cache.",
                    "services": "nginx,static-cache",
                    "logs": "ERROR nginx static asset cache miss",
                }
            )
        for index in range(4):
            incident_id = f"INC-UNRELATED-RESOLUTION-{index}"
            await app_main_memory.update_incident_outcome(
                incident_id,
                "Static asset cache refresh completed.",
                True,
                services=["nginx", "static-cache"],
                recommended_resolution="Refresh the static asset cache.",
            )

    from app.main import memory as app_main_memory

    asyncio.run(add_newer_unrelated_memory())
    with closing(db.get_connection()) as connection:
        for index in range(6):
            connection.execute(
                "UPDATE memory_events SET created_at = ? WHERE incident_id = ? AND category = 'incident'",
                (f"2099-01-01 00:00:{index:02d}", f"INC-UNRELATED-NGINX-{index}"),
            )
        for index in range(4):
            connection.execute(
                "UPDATE memory_events SET created_at = ? WHERE incident_id = ? AND category = 'resolution'",
                (f"2099-01-01 00:01:{index:02d}", f"INC-UNRELATED-RESOLUTION-{index}"),
            )
        connection.commit()

    logs_b = (
        '2026/11/07 10:30:00 [error] connect() failed (111: Connection refused) '
        'while connecting to upstream, upstream: "http://127.0.0.1:8001/api/orders"'
    )
    incident_b = client.post(
        "/api/incidents",
        json={
            "title": "Nginx upstream connection failure",
            "summary": "Nginx cannot connect to the application service.",
            "severity": "High",
            "services": "nginx,orders-api",
            "logs": logs_b,
        },
    )
    assert incident_b.status_code == 200, incident_b.text
    incident_b_id = incident_b.json()["incident_id"]

    analysis_b = client.post(f"/api/incidents/{incident_b_id}/analyze", json={})
    assert analysis_b.status_code == 200, analysis_b.text
    payload_b = analysis_b.json()
    assert payload_b["memory_status"] == "local-fallback"
    assert any(
        match["incident_id"] == incident_a_id
        for match in payload_b["historical_matches"]
    )
    assert all(
        not match["incident_id"].startswith("INC-UNRELATED-NGINX-")
        for match in payload_b["historical_matches"]
    )
    assert payload_b["successful_resolution_memory"]["incident_id"] == incident_a_id
    assert payload_b["successful_resolution_memory"]["success"] is True
    assert payload_b["successful_resolution_memory"]["outcome"] == recorded_outcome
    assert payload_b["successful_resolution_memory"]["resolution"]
    reference_a = next(
        reference
        for reference in payload_b["memory_references"]
        if reference["incident_id"] == incident_a_id
    )
    assert reference_a["success"] is True
    assert reference_a["outcome"] == recorded_outcome
    assert reference_a["resolution"] == payload_b["successful_resolution_memory"]["resolution"]
    assert not any(
        token in reference_a["reason"]
        for token in ("2026", "11", "07", "09", "30", "10")
    )
    assert incident_a_id in payload_b["memory_impact"]
    assert "successful" in payload_b["memory_impact"].lower()
    persisted_b = client.get(f"/api/incidents/{incident_b_id}")
    assert persisted_b.status_code == 200
    persisted_match = next(
        match
        for match in persisted_b.json()["historical_matches"]
        if match["incident_id"] == incident_a_id
    )
    assert persisted_match["outcome"] == recorded_outcome
    assert persisted_match["resolution"] == reference_a["resolution"]

    unrelated = client.post(
        "/api/incidents",
        json={
            "title": "Payroll database connection failure",
            "summary": "The payroll database is refusing connections.",
            "severity": "High",
            "services": "payroll,postgres",
            "logs": "ERROR postgres connection refused by the payroll database",
        },
    )
    assert unrelated.status_code == 200, unrelated.text
    unrelated_analysis = client.post(
        f"/api/incidents/{unrelated.json()['incident_id']}/analyze",
        json={},
    )
    assert unrelated_analysis.status_code == 200, unrelated_analysis.text
    unrelated_payload = unrelated_analysis.json()
    assert all(
        match["incident_id"] != incident_a_id
        for match in unrelated_payload["historical_matches"]
    )
    assert (
        unrelated_payload["successful_resolution_memory"] is None
        or unrelated_payload["successful_resolution_memory"]["incident_id"] != incident_a_id
    )


def test_hindsight_client_mock_verifies_connection_retain_and_recall(monkeypatch) -> None:
    retained: list[dict] = []

    class FakeHindsight:
        def __init__(self, base_url: str, api_key: str | None, **kwargs) -> None:
            assert base_url == "https://hindsight.example"
            assert api_key == "test-key-not-a-real-secret"
            assert kwargs["timeout"] in {5.0, 300.0}
            assert kwargs["max_attempts"] == 1

        def get_version(self) -> dict:
            return {"version": "test"}

        async def aretain(self, **kwargs) -> SimpleNamespace:
            retained.append(kwargs)
            return SimpleNamespace(success=True)

        async def arecall(self, **kwargs) -> SimpleNamespace:
            assert kwargs["bank_id"] == "incidentiq-memory"
            requested_tags = set(kwargs.get("tags") or [])
            matching = [
                {
                    "text": item["content"],
                    "metadata": item.get("metadata", {}),
                    "tags": item.get("tags", []),
                    "id": str(index),
                }
                for index, item in enumerate(retained)
                if requested_tags.issubset(set(item.get("tags", [])))
            ]
            return SimpleNamespace(results=matching)

    monkeypatch.setattr("app.config.settings.hindsight_base_url", "https://hindsight.example")
    monkeypatch.setattr("app.config.settings.hindsight_api_key", "test-key-not-a-real-secret")
    monkeypatch.setitem(sys.modules, "hindsight_client", SimpleNamespace(Hindsight=FakeHindsight))

    async def scenario() -> None:
        memory = HindsightMemoryService()
        assert memory.mode == "local-fallback"
        assert memory.available is False
        health = await memory.check_connection()
        assert health["reachable"] is True
        assert memory.mode == "hindsight"
        incident = {
            "incident_id": "INC-HINDSIGHT-MOCK",
            "title": "Nginx upstream 502",
            "summary": "Nginx returned 502 for the API.",
            "services": ["nginx", "api"],
            "logs": "ERROR nginx upstream returned HTTP 502",
        }
        assert await memory.retain_incident(incident) == "hindsight"
        assert await memory.update_incident_outcome(
            incident["incident_id"],
            "Corrected the upstream listener port and verified recovery.",
            True,
            services=["nginx", "api"],
            recommended_resolution="Set the upstream port to 8080.",
        ) == "hindsight"
        assert await memory.update_incident_outcome(
            incident["incident_id"],
            "Restarting the proxy did not resolve the refusal.",
            False,
            services=["nginx", "api"],
            recommended_resolution="Restart the proxy.",
        ) == "hindsight"

        recalled = await memory.recall_similar_incidents(
            {**incident, "incident_id": "INC-HINDSIGHT-MOCK-QUERY"}
        )
        successful = await memory.retrieve_successful_resolutions(["nginx"])
        failed = await memory.retrieve_failed_attempts(["nginx"])
        assert any(item["incident_id"] == incident["incident_id"] for item in recalled)
        assert successful[0]["resolution"] == "Set the upstream port to 8080."
        assert failed[0]["resolution"] == "Restart the proxy."
        assert failed[0]["success"] is False
        assert retained[0]["document_id"] == "incidentiq:incident:INC-HINDSIGHT-MOCK"
        assert retained[1]["document_id"].startswith("incidentiq:resolution:INC-HINDSIGHT-MOCK:success:")
        assert retained[2]["document_id"].startswith("incidentiq:resolution:INC-HINDSIGHT-MOCK:failure:")
        assert all(item["update_mode"] == "replace" for item in retained)

    asyncio.run(scenario())
    assert len(retained) == 3
    assert retained[0]["tags"] == ["incident", "sre"]
    assert retained[1]["metadata"]["success"] == "true"
    assert retained[2]["metadata"]["success"] == "false"


def test_hindsight_api_a_to_b_recall_survives_service_restart_and_filters_unrelated(
    monkeypatch,
) -> None:
    retained: dict[str, dict] = {}

    class FakeHindsight:
        def __init__(self, **kwargs) -> None:
            assert kwargs["base_url"] == "https://hindsight.example"

        def get_version(self) -> dict:
            return {"version": "test"}

        async def aretain(self, **kwargs) -> SimpleNamespace:
            retained[kwargs["document_id"]] = kwargs
            return SimpleNamespace(success=True)

        async def arecall(self, **kwargs) -> SimpleNamespace:
            requested_tags = set(kwargs.get("tags") or [])
            results = [
                {
                    "text": item["content"],
                    "metadata": {} if "resolution" in item["tags"] else item["metadata"],
                    "tags": item["tags"],
                    "id": item["document_id"],
                }
                for item in retained.values()
                if requested_tags.issubset(set(item["tags"]))
            ]
            return SimpleNamespace(results=results)

    monkeypatch.setattr("app.config.settings.hindsight_base_url", "https://hindsight.example")
    monkeypatch.setattr("app.config.settings.hindsight_api_key", None)
    monkeypatch.setitem(sys.modules, "hindsight_client", SimpleNamespace(Hindsight=FakeHindsight))

    memory_a = HindsightMemoryService()
    monkeypatch.setattr("app.main.memory", memory_a)
    incident_a = client.post(
        "/api/incidents",
        json={
            "title": "Nginx upstream connection refused",
            "summary": "Nginx receives a refused connection from the application endpoint.",
            "severity": "High",
            "services": "nginx,api",
            "logs": (
                'connect() failed (111: Connection refused) while connecting to upstream, '
                'upstream: "http://127.0.0.1:8001/"'
            ),
        },
    ).json()
    analysis_a = client.post(
        f"/api/incidents/{incident_a['incident_id']}/analyze",
        json={},
    )
    assert analysis_a.status_code == 200, analysis_a.text
    assert analysis_a.json()["memory_status"] == "hindsight"
    outcome_a = client.post(
        f"/api/incidents/{incident_a['incident_id']}/outcome",
        json={
            "status": "resolved",
            "success": True,
            "outcome": "Verified the application listener and endpoint reachability; service recovered.",
        },
    )
    assert outcome_a.status_code == 200, outcome_a.text

    # A new service instance models a backend restart; retained records belong to Hindsight.
    memory_b = HindsightMemoryService()
    monkeypatch.setattr("app.main.memory", memory_b)
    incident_b = client.post(
        "/api/incidents",
        json={
            "title": "Orders Nginx HTTP 502",
            "summary": "Orders receives a refused Nginx upstream connection.",
            "severity": "High",
            "services": "nginx,orders",
            "logs": (
                'connect() failed (111: Connection refused) while connecting to upstream, '
                'upstream: "http://127.0.0.1:8001/api/orders"'
            ),
        },
    ).json()
    analysis_b = client.post(f"/api/incidents/{incident_b['incident_id']}/analyze", json={})
    assert analysis_b.status_code == 200, analysis_b.text
    payload_b = analysis_b.json()
    assert payload_b["memory_status"] == "hindsight"
    assert any(match["incident_id"] == incident_a["incident_id"] for match in payload_b["historical_matches"])
    assert payload_b["successful_resolution_memory"]["incident_id"] == incident_a["incident_id"]
    assert incident_a["incident_id"] in payload_b["memory_impact"]
    assert payload_b["memory_influenced"] is True
    unrelated = client.post(
        "/api/incidents",
        json={
            "title": "PostgreSQL connection refused",
            "summary": "The database listener refused a connection.",
            "severity": "High",
            "services": "postgresql",
            "logs": "FATAL connection refused by PostgreSQL database listener",
        },
    ).json()
    unrelated_analysis = client.post(
        f"/api/incidents/{unrelated['incident_id']}/analyze",
        json={},
    )
    assert unrelated_analysis.status_code == 200, unrelated_analysis.text
    unrelated_payload = unrelated_analysis.json()
    assert all(
        match["incident_id"] != incident_a["incident_id"]
        for match in unrelated_payload["historical_matches"]
    )
    assert (
        unrelated_payload["successful_resolution_memory"] is None
        or unrelated_payload["successful_resolution_memory"]["incident_id"] != incident_a["incident_id"]
    )


def test_hindsight_connection_failure_uses_sqlite_fallback(monkeypatch) -> None:
    class UnavailableHindsight:
        def __init__(self, **kwargs) -> None:
            assert kwargs["base_url"] == "http://127.0.0.1:8888"

        def get_version(self) -> None:
            raise ConnectionError("connection refused")

        async def aretain(self, **kwargs) -> None:
            raise ConnectionError("connection refused")

        async def arecall(self, **kwargs) -> None:
            raise ConnectionError("connection refused")

    monkeypatch.setattr("app.config.settings.hindsight_base_url", "http://127.0.0.1:8888")
    monkeypatch.setattr("app.config.settings.hindsight_api_key", None)
    monkeypatch.setitem(sys.modules, "hindsight_client", SimpleNamespace(Hindsight=UnavailableHindsight))

    async def scenario() -> None:
        memory = HindsightMemoryService()
        health = await memory.check_connection()
        assert memory.mode == "local-fallback"
        assert memory.available is False
        assert memory.connection_error == "ConnectionError"
        assert health["configured"] is True
        assert health["reachable"] is False
        await memory.retain_incident({"incident_id": "INC-FALLBACK", "title": "Local record"})
        events = db.read_memory_events(category="incident", bank_id=memory.bank_id)
        assert len(events) == 1

    asyncio.run(scenario())


def test_hindsight_retain_rejection_uses_sqlite_fallback(monkeypatch) -> None:
    class RejectingHindsight:
        def __init__(self, **kwargs) -> None:
            pass

        def get_version(self) -> dict:
            return {"version": "test"}

        async def aretain(self, **kwargs) -> SimpleNamespace:
            return SimpleNamespace(success=False)

    monkeypatch.setattr("app.config.settings.hindsight_base_url", "https://hindsight.example")
    monkeypatch.setattr("app.config.settings.hindsight_api_key", None)
    monkeypatch.setitem(sys.modules, "hindsight_client", SimpleNamespace(Hindsight=RejectingHindsight))

    async def scenario() -> None:
        memory = HindsightMemoryService()
        mode = await memory.retain_incident(
            {"incident_id": "INC-RETAIN-REJECTED", "title": "Rejected remote retain"}
        )
        assert mode == "local-fallback"
        assert memory.connection_error == "RuntimeError"
        events = db.read_memory_events(category="incident", bank_id=memory.bank_id)
        assert [event["incident_id"] for event in events] == ["INC-RETAIN-REJECTED"]

    asyncio.run(scenario())


def test_hindsight_health_check_reports_configured_connectivity(monkeypatch) -> None:
    class ReachableHindsight:
        def __init__(self, base_url: str, api_key: str | None, **kwargs) -> None:
            assert base_url == "https://hindsight.example"
            assert api_key == "mock-key"

        def get_version(self) -> dict:
            return {"version": "test"}

    monkeypatch.setattr("app.config.settings.hindsight_base_url", "https://hindsight.example")
    monkeypatch.setattr("app.config.settings.hindsight_api_key", "mock-key")
    monkeypatch.setitem(sys.modules, "hindsight_client", SimpleNamespace(Hindsight=ReachableHindsight))
    monkeypatch.setattr("app.main.memory", HindsightMemoryService())

    response = client.get("/api/memory/health")
    assert response.status_code == 200
    assert response.json() == {
        "configured": True,
        "reachable": True,
        "mode": "hindsight",
        "bank_id": "incidentiq-memory",
        "error": None,
    }


def test_hindsight_retain_failure_falls_back_and_api_reports_actual_mode(monkeypatch) -> None:
    class RetainFailureHindsight:
        def __init__(self, base_url: str, api_key: str | None, **kwargs) -> None:
            assert base_url == "https://hindsight.example"
            assert api_key is None

        def get_version(self) -> dict:
            return {"version": "test"}

        async def arecall(self, **kwargs) -> SimpleNamespace:
            return SimpleNamespace(results=[])

        async def aretain(self, **kwargs) -> SimpleNamespace:
            raise RuntimeError("private endpoint detail")

    monkeypatch.setattr("app.config.settings.hindsight_base_url", "https://hindsight.example")
    monkeypatch.setattr("app.config.settings.hindsight_api_key", None)
    monkeypatch.setitem(sys.modules, "hindsight_client", SimpleNamespace(Hindsight=RetainFailureHindsight))
    memory = HindsightMemoryService()
    monkeypatch.setattr("app.main.memory", memory)

    incident = client.post(
        "/api/incidents",
        json={
            "title": "Hindsight operation failure",
            "summary": "Retain fails after the connection check.",
            "services": "api",
            "logs": "ERROR API returned HTTP 502",
        },
    ).json()
    response = client.post(f"/api/incidents/{incident['incident_id']}/analyze", json={})
    assert response.status_code == 200
    assert response.json()["memory_status"] == "local-fallback"
    assert memory.mode == "local-fallback"
    assert memory.connection_error == "RuntimeError"
    assert "private endpoint detail" not in memory.connection_error
    assert db.read_memory_events(category="incident", bank_id=memory.bank_id)


def test_action_approval_requires_server_token_and_enforces_states(monkeypatch) -> None:
    monkeypatch.setattr("app.config.settings.incidentiq_approval_token", "audit-approval-token")
    incident = client.post(
        "/api/incidents",
        json={"title": "Risky change", "summary": "Needs careful operator review", "services": "api"},
    ).json()

    def propose(description: str) -> dict:
        response = client.post(
            f"/api/incidents/{incident['incident_id']}/actions",
            json={"description": description, "rationale": "Validate after manual execution."},
        )
        assert response.status_code == 200
        assert response.json()["status"] == "pending"
        return response.json()

    headers = {"Authorization": "Bearer audit-approval-token"}
    pending = propose("Change production Nginx upstream configuration")
    pending_path = f"/api/incidents/{incident['incident_id']}/actions/{pending['action_id']}"
    assert client.post(
        f"{pending_path}/execution",
        json={"execution_note": "Tried to bypass pending approval"},
        headers=headers,
    ).status_code == 409
    assert client.post(
        f"{pending_path}/approval",
        json={"decision": "approved"},
    ).status_code == 401
    assert client.post(
        f"{pending_path}/approval",
        json={"decision": "approved"},
        headers={"Authorization": "Bearer wrong-token"},
    ).status_code == 403

    approved = client.post(
        f"{pending_path}/approval",
        json={"decision": "approved", "note": "Reviewed rollback and validation plan."},
        headers=headers,
    )
    assert approved.status_code == 200
    assert approved.json()["status"] == "approved"
    assert client.post(
        f"/api/incidents/{incident['incident_id']}/outcome",
        json={"status": "resolved", "success": True, "outcome": "Attempt to bypass approval."},
    ).status_code == 409
    assert client.post(
        f"{pending_path}/execution",
        json={"execution_note": "Unauthorized external execution report."},
    ).status_code == 401
    assert client.post(
        f"{pending_path}/execution",
        json={"execution_note": "Invalid approver attempted execution."},
        headers={"Authorization": "Bearer wrong-token"},
    ).status_code == 403
    executed = client.post(
        f"{pending_path}/execution",
        json={"execution_note": "Operator applied change and verified health."},
        headers=headers,
    )
    assert executed.status_code == 200
    assert executed.json()["status"] == "executed"
    assert client.post(
        f"/api/incidents/{incident['incident_id']}/outcome",
        json={"status": "resolved", "success": True, "outcome": "Approved change succeeded."},
    ).status_code == 200
    assert client.post(
        f"{pending_path}/approval",
        json={"decision": "rejected"},
        headers=headers,
    ).status_code == 409

    rejected = propose("Disable the unhealthy route after approval")
    rejected_path = f"/api/incidents/{incident['incident_id']}/actions/{rejected['action_id']}"
    rejection = client.post(
        f"{rejected_path}/approval",
        json={"decision": "rejected", "note": "Risk not mitigated."},
        headers=headers,
    )
    assert rejection.status_code == 200
    assert rejection.json()["status"] == "rejected"
    assert client.post(
        f"{rejected_path}/execution",
        json={"execution_note": "Should not be allowed"},
        headers=headers,
    ).status_code == 409


def test_action_execution_is_disabled_without_server_approval_configuration() -> None:
    incident = client.post(
        "/api/incidents",
        json={"title": "Approval missing", "summary": "Server token is not configured"},
    ).json()
    action = client.post(
        f"/api/incidents/{incident['incident_id']}/actions",
        json={"description": "Perform a potentially risky production change"},
    ).json()
    response = client.post(
        f"/api/incidents/{incident['incident_id']}/actions/{action['action_id']}/approval",
        json={"decision": "approved"},
        headers={"Authorization": "Bearer arbitrary"},
    )
    assert response.status_code == 503


def test_groq_mock_reports_connected_only_after_successful_provider_call(monkeypatch) -> None:
    expected = {"root_cause": "Mock provider diagnosis"}

    class FakeGroq:
        def __init__(self, api_key: str) -> None:
            assert api_key == "test-groq-key-not-a-real-secret"
            self.chat = SimpleNamespace(
                completions=SimpleNamespace(
                    create=self.create_completion
                )
            )

        @staticmethod
        def create_completion(**kwargs) -> SimpleNamespace:
            assert kwargs["model"] == "incidentiq-audit-model"
            assert kwargs["response_format"] == {"type": "json_object"}
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(expected)))]
            )

    monkeypatch.setattr("app.config.settings.groq_api_key", "test-groq-key-not-a-real-secret")
    monkeypatch.setattr("app.config.settings.groq_model", "incidentiq-audit-model")
    monkeypatch.setitem(sys.modules, "groq", SimpleNamespace(Groq=FakeGroq))

    async def scenario() -> None:
        analyzer = groq_ai.GroqIncidentAnalyzer()
        result = await analyzer.analyze({"logs": "HTTP 502"})
        assert result["provider"] == "groq"
        assert result["root_cause"] == expected["root_cause"]

    asyncio.run(scenario())
    assert groq_ai.get_groq_status()["status"] == "connected"
    assert groq_ai.get_groq_status()["connected"] is True
    assert groq_ai.get_groq_status()["model"] == "incidentiq-audit-model"


def test_groq_failed_request_falls_back_without_claiming_connection(monkeypatch) -> None:
    class FailingGroq:
        def __init__(self, api_key: str) -> None:
            assert api_key == "test-groq-key"
            self.chat = SimpleNamespace(
                completions=SimpleNamespace(create=self.fail_completion)
            )

        @staticmethod
        def fail_completion(**kwargs) -> None:
            assert kwargs["model"] == "llama-3.3-70b-versatile"
            raise ConnectionError()

    monkeypatch.setattr("app.config.settings.groq_api_key", "test-groq-key")
    monkeypatch.setitem(sys.modules, "groq", SimpleNamespace(Groq=FailingGroq))

    async def scenario() -> None:
        result = await groq_ai.GroqIncidentAnalyzer().analyze({"logs": "HTTP 502"})
        assert result["provider"] == "fallback"

    asyncio.run(scenario())
    assert groq_ai.get_groq_status()["status"] == "request-failed"
    assert groq_ai.get_groq_status()["connected"] is False


def test_groq_missing_credentials_is_not_configured(monkeypatch) -> None:
    monkeypatch.setattr("app.config.settings.groq_api_key", None)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    analyzer = groq_ai.GroqIncidentAnalyzer()
    assert analyzer.enabled is False
    assert groq_ai.get_groq_status()["status"] == "not-configured"


def test_fallback_outcomes_are_classified_and_retrievable() -> None:
    async def scenario() -> None:
        memory = HindsightMemoryService()
        assert memory.mode == "local-fallback"
        await memory.update_incident_outcome(
            "INC-SUCCESS",
            "The port correction restored service.",
            True,
            services=["nginx", "api"],
            recommended_resolution="Set Nginx upstream to port 8080.",
        )
        await memory.update_incident_outcome(
            "INC-FAILURE",
            "Restarting the proxy did not resolve the 502.",
            False,
            services=["nginx", "api"],
            recommended_resolution="Restart the proxy.",
        )

        successes = await memory.retrieve_successful_resolutions(["nginx"])
        failures = await memory.retrieve_failed_attempts(["nginx"])
        assert successes[0]["resolution"] == "Set Nginx upstream to port 8080."
        assert failures[0]["resolution"] == "Restart the proxy."
        assert failures[0]["outcome"] == "Restarting the proxy did not resolve the 502."

    asyncio.run(scenario())


def test_memory_recall_does_not_report_the_current_incident_as_history() -> None:
    async def scenario() -> None:
        memory = HindsightMemoryService()
        incident = {
            "incident_id": "INC-SELF-RECALL",
            "title": "Nginx HTTP 502",
            "summary": "The upstream port is incorrect.",
            "services": "nginx",
            "logs": "ERROR nginx HTTP 502 upstream port mismatch",
        }
        await memory.retain_incident(incident)
        await memory.update_incident_outcome(
            incident["incident_id"],
            "The fix resolved this case.",
            True,
            services=["nginx"],
            recommended_resolution="Align the upstream port.",
        )
        result = await investigate_incident(incident, memory)
        assert result["historical_matches"] == []
        assert result["memory_influenced"] is False

    asyncio.run(scenario())


def test_nginx_upstream_connection_refused_uses_port_and_relevant_unique_history() -> None:
    async def scenario() -> None:
        memory = HindsightMemoryService()
        prior_incident = {
            "incident_id": "INC-NGINX-REFUSED-PRIOR",
            "title": "Nginx upstream connection refused",
            "summary": "HTTP 502 when the Nginx upstream listener refused the connection.",
            "services": "nginx,api",
            "logs": (
                'ERROR nginx connect() failed (111: Connection refused) while connecting to upstream, '
                'upstream: "http://127.0.0.1:8001/"'
            ),
        }
        await memory.retain_incident(prior_incident)
        await memory.retain_incident(prior_incident)
        await memory.retain_incident(
            {
                "incident_id": "INC-CHECKOUT-LATENCY",
                "title": "Checkout API latency spike",
                "summary": "Checkout API latency spike returned HTTP 502 through the gateway.",
                "services": "api,checkout",
                "logs": "ERROR checkout API latency exceeded threshold; HTTP 502 at gateway",
            }
        )

        incident = {
            "incident_id": "INC-NGINX-REFUSED-CURRENT",
            "title": "Nginx HTTP 502",
            "summary": "Nginx cannot connect to its upstream.",
            "severity": "High",
            "services": "nginx,api",
            "logs": (
                'ERROR nginx connect() failed (111: Connection refused) while connecting to upstream, '
                'upstream: "http://127.0.0.1:8001/"'
            ),
        }
        result = await investigate_incident(incident, memory)

        assert "connection refused" in result["root_cause"].lower()
        assert "127.0.0.1:8001" in result["root_cause"]
        assert "forwarding to an incorrect upstream port" not in result["root_cause"].lower()
        assert "8001" in result["suggested_resolution"]
        assert "does not establish that the port is incorrect" in result["suggested_resolution"].lower()
        assert [match["incident_id"] for match in result["historical_matches"]] == [
            "INC-NGINX-REFUSED-PRIOR"
        ]
        assert result["memory_references"][0]["title"] == "Nginx upstream connection refused"
        assert len(result["memory_impact"]) < 200

        unverified_port_incident = {
            **incident,
            "incident_id": "INC-NGINX-PORT-UNVERIFIED",
            "logs": "ERROR nginx HTTP 502; configured upstream port 8001 is reported as incorrect",
        }
        unverified_result = await investigate_incident(unverified_port_incident, memory)
        assert "incorrect upstream port" not in unverified_result["root_cause"].lower()

        class FakeHindsight:
            @staticmethod
            async def arecall(**kwargs) -> dict:
                assert "8001" in kwargs["query"]
                return {
                    "results": [
                        {"text": json.dumps(prior_incident), "metadata": {"incident_id": prior_incident["incident_id"]}},
                        {"text": json.dumps(prior_incident), "metadata": {"incident_id": prior_incident["incident_id"]}},
                        {
                            "text": json.dumps({
                                "incident_id": "INC-CHECKOUT-LATENCY",
                                "title": "Checkout API latency spike",
                                "summary": "Checkout API returned HTTP 502 via gateway.",
                                "logs": "ERROR checkout API latency exceeded threshold; HTTP 502 at gateway",
                            }),
                            "metadata": {"incident_id": "INC-CHECKOUT-LATENCY"},
                        },
                    ]
                }

        hindsight_memory = HindsightMemoryService()
        hindsight_memory.configured = True
        hindsight_memory._hindsight_client = FakeHindsight()
        hindsight_matches = await hindsight_memory.recall_similar_incidents(incident)
        assert [match["incident_id"] for match in hindsight_matches] == [
            "INC-NGINX-REFUSED-PRIOR"
        ]
        assert hindsight_matches[0]["title"] == "Nginx upstream connection refused"

    asyncio.run(scenario())


def test_complete_nginx_502_learning_workflow_across_incidents() -> None:
    incident_a = client.post(
        "/api/incidents",
        json={
            "title": "Checkout gateway HTTP 502 - Incident A",
            "summary": "Nginx returns HTTP 502 because the configured upstream port is incorrect.",
            "severity": "Critical",
            "services": "nginx,api",
            "logs": (
                "ERROR nginx HTTP 502 Bad Gateway\n"
                "CRITICAL nginx upstream port mismatch: configured upstream port 8081, "
                "but service listens on port 8080"
            ),
        },
    ).json()
    analysis_a = client.post(f"/api/incidents/{incident_a['incident_id']}/analyze", json={}).json()
    assert "incorrect upstream port" in analysis_a["root_cause"].lower()
    assert "8080" in analysis_a["suggested_resolution"]

    recorded_a = client.post(
        f"/api/incidents/{incident_a['incident_id']}/outcome",
        json={
            "status": "resolved",
            "success": True,
            "outcome": "Changed the Nginx upstream port to 8080; HTTP 200 responses confirmed recovery.",
        },
    )
    assert recorded_a.status_code == 200

    incident_b = client.post(
        "/api/incidents",
        json={
            "title": "Checkout gateway HTTP 502 - Incident B",
            "summary": "Similar HTTP 502 symptoms on the Nginx gateway and checkout API.",
            "severity": "High",
            "services": "nginx,api",
            "logs": "ERROR nginx HTTP 502 Bad Gateway\nWARNING upstream connection refused on checkout API",
        },
    ).json()
    analysis_b = client.post(f"/api/incidents/{incident_b['incident_id']}/analyze", json={}).json()
    assert any(match.get("incident_id") == incident_a["incident_id"] for match in analysis_b["historical_matches"])
    assert analysis_b["memory_influenced"] is True
    assert analysis_b["successful_resolution_memory"]["incident_id"] == incident_a["incident_id"]
    assert "application listener" in analysis_b["suggested_resolution"].lower()
    assert "8080" not in analysis_b["suggested_resolution"]

    failed_outcome = client.post(
        f"/api/incidents/{incident_b['incident_id']}/outcome",
        json={
            "status": "investigating",
            "success": False,
            "outcome": "The recommended port correction did not resolve this incident.",
        },
    )
    assert failed_outcome.status_code == 200

    incident_c = client.post(
        "/api/incidents",
        json={
            "title": "Checkout gateway HTTP 502 - Incident C",
            "summary": "Another similar 502 incident for checkout.",
            "severity": "High",
            "services": "nginx,api",
            "logs": "ERROR nginx HTTP 502 Bad Gateway\nERROR upstream connection refused",
        },
    ).json()
    analysis_c = client.post(f"/api/incidents/{incident_c['incident_id']}/analyze", json={}).json()
    assert analysis_c["failed_resolution_memories"]
    assert "application listener" in analysis_c["suggested_resolution"].lower()
    assert "did not resolve this incident" not in analysis_c["suggested_resolution"]
    assert analysis_c["memory_influenced"] is True


def test_database_url_is_honored_and_memory_persists_in_new_process(tmp_path, monkeypatch) -> None:
    database_path = tmp_path / "configured-database.db"
    database_url = f"sqlite:///{database_path.as_posix()}"
    monkeypatch.setattr("app.config.settings.database_url", database_url)
    db.init_db()
    db.insert_memory_event(
        incident_id="INC-DATABASE-URL",
        title="Database URL persistence check",
        content='{"outcome":"survives process restart"}',
        bank_id="audit-bank",
        category="resolution",
        resolution="Verified test persistence",
        success=True,
    )

    backend_dir = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["DATABASE_URL"] = database_url
    env["PYTHONPATH"] = str(backend_dir)
    code = (
        "from app.db import read_memory_events; "
        "rows = read_memory_events(category='resolution', bank_id='audit-bank'); "
        "print(rows[0]['incident_id'])"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=backend_dir,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout.strip() == "INC-DATABASE-URL"


def test_database_url_rejects_non_sqlite_backends(monkeypatch) -> None:
    monkeypatch.setattr("app.config.settings.database_url", "postgresql://localhost/incidents")
    with pytest.raises(ValueError, match="Only SQLite"):
        db.configured_db_path()
