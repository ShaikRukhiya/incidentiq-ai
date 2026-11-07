from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path
from urllib.request import Request, urlopen

import pytest

from app.config import settings
from app.services.groq_ai import GroqIncidentAnalyzer
from app.services.memory import HindsightMemoryService

RUN_LIVE_INTEGRATION_TESTS = os.getenv("INCIDENTIQ_RUN_LIVE_INTEGRATION_TESTS") == "1"
HINDSIGHT_CONFIGURED = bool(
    settings.hindsight_base_url or os.getenv("HINDSIGHT_BASE_URL")
)
GROQ_CONFIGURED = bool(settings.groq_api_key or os.getenv("GROQ_API_KEY"))


@pytest.mark.skipif(
    not RUN_LIVE_INTEGRATION_TESTS or not HINDSIGHT_CONFIGURED,
    reason="Opt in with INCIDENTIQ_RUN_LIVE_INTEGRATION_TESTS=1 and configure Hindsight.",
)
def test_live_hindsight_retain_and_recall() -> None:
    async def scenario() -> None:
        memory = HindsightMemoryService()
        health = await memory.check_connection()
        assert health["mode"] == "hindsight", "Configured Hindsight endpoint did not pass get_version."
        marker = f"incidentiq-live-check-{uuid.uuid4().hex}"
        incident = {
            "incident_id": marker,
            "title": marker,
            "summary": f"Live Hindsight retain and recall verification {marker}",
            "services": ["incidentiq-live-check"],
            "root_cause": "Unique live integration test record.",
            "recommended_resolution": "No infrastructure action; test-only memory record.",
            "logs": marker,
        }
        assert await memory.retain_incident(incident) == "hindsight"
        assert await memory.update_incident_outcome(
            marker,
            f"Successful live verification outcome {marker}",
            True,
            services=["incidentiq-live-check"],
            recommended_resolution=f"Successful test resolution {marker}",
        ) == "hindsight"
        assert await memory.update_incident_outcome(
            marker,
            f"Failed live verification outcome {marker}",
            False,
            services=["incidentiq-live-check"],
            recommended_resolution=f"Failed test resolution {marker}",
        ) == "hindsight"

        restarted_memory = HindsightMemoryService()
        restarted_health = await restarted_memory.check_connection()
        assert restarted_health["reachable"] is True
        recalled = []
        recall_query = {**incident, "incident_id": f"{marker}-recall"}
        for attempt in range(10):
            recalled = await restarted_memory.recall_similar_incidents(recall_query)
            if any(marker in str(item.get("text") or item.get("summary")) for item in recalled):
                break
            if attempt < 9:
                time.sleep(1)
        assert any(marker in str(item.get("text") or item.get("summary")) for item in recalled)

        successful = []
        failed = []
        for attempt in range(10):
            successful = await restarted_memory.retrieve_successful_resolutions(["incidentiq-live-check"])
            failed = await restarted_memory.retrieve_failed_attempts(["incidentiq-live-check"])
            if (
                any(item.get("incident_id") == marker for item in successful)
                and any(item.get("incident_id") == marker for item in failed)
            ):
                break
            if attempt < 9:
                time.sleep(1)
        assert any(item.get("incident_id") == marker for item in successful)
        assert any(item.get("incident_id") == marker for item in failed)

    asyncio.run(scenario())


@pytest.mark.skipif(
    not RUN_LIVE_INTEGRATION_TESTS or not HINDSIGHT_CONFIGURED,
    reason="Opt in with INCIDENTIQ_RUN_LIVE_INTEGRATION_TESTS=1 and configure Hindsight.",
)
def test_live_hindsight_api_learning_survives_backend_process_restart(tmp_path) -> None:
    backend_dir = Path(__file__).resolve().parents[1]
    database_path = tmp_path / "live-hindsight-restart.db"
    marker = uuid.uuid4().hex[:12]
    bank_id = f"{settings.hindsight_bank_id}-test-{marker}"
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    base_url = f"http://127.0.0.1:{port}"
    environment = os.environ.copy()
    environment.update(
        {
            "PYTHONPATH": str(backend_dir),
            "DATABASE_URL": f"sqlite:///{database_path.as_posix()}",
            "HINDSIGHT_BANK_ID": bank_id,
        }
    )

    def request_json(path: str, payload: dict | None = None) -> dict:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = Request(
            f"{base_url}{path}",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST" if payload is not None else "GET",
        )
        with urlopen(request, timeout=30) as response:
            return json.loads(response.read())

    def start_backend() -> subprocess.Popen:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "app.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
            ],
            cwd=backend_dir,
            env=environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        last_error: Exception | None = None
        for _ in range(60):
            if process.poll() is not None:
                raise AssertionError(f"IncidentIQ backend exited with status {process.returncode}")
            try:
                health = request_json("/api/memory/health")
                if health.get("reachable") is True:
                    return process
            except Exception as exc:
                last_error = exc
            time.sleep(0.5)
        process.terminate()
        process.wait(timeout=10)
        raise AssertionError(f"IncidentIQ/Hindsight health check did not succeed: {last_error}")

    def stop_backend(process: subprocess.Popen) -> None:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)

    process = start_backend()
    try:
        incident_a = request_json(
            "/api/incidents",
            {
                "title": f"Nginx upstream refusal {marker} A",
                "summary": "Nginx reports a refused connection from its application upstream.",
                "severity": "High",
                "services": "nginx,api",
                "logs": (
                    'connect() failed (111: Connection refused) while connecting to upstream, '
                    'upstream: "http://127.0.0.1:8001/"'
                ),
            },
        )
        analysis_a = request_json(f"/api/incidents/{incident_a['incident_id']}/analyze", {})
        assert analysis_a["memory_status"] == "hindsight"
        outcome = request_json(
            f"/api/incidents/{incident_a['incident_id']}/outcome",
            {
                "status": "resolved",
                "success": True,
                "outcome": "Verified the application listener and reachability; service recovered.",
            },
        )
        assert outcome["memory_status"] == "hindsight"
    finally:
        stop_backend(process)

    process = start_backend()
    try:
        incident_b = request_json(
            "/api/incidents",
            {
                "title": f"Orders Nginx upstream refusal {marker} B",
                "summary": "The orders route receives a refused connection from its Nginx upstream.",
                "severity": "High",
                "services": "nginx,orders",
                "logs": (
                    'connect() failed (111: Connection refused) while connecting to upstream, '
                    'upstream: "http://127.0.0.1:8001/api/orders"'
                ),
            },
        )
        analysis_b = request_json(f"/api/incidents/{incident_b['incident_id']}/analyze", {})
        assert analysis_b["memory_status"] == "hindsight"
        assert any(
            match.get("incident_id") == incident_a["incident_id"]
            for match in analysis_b["historical_matches"]
        )
        assert (
            analysis_b["successful_resolution_memory"]["incident_id"]
            == incident_a["incident_id"]
        )
        assert incident_a["incident_id"] in analysis_b["memory_impact"]
        assert analysis_b["memory_influenced"] is True

        unrelated = request_json(
            "/api/incidents",
            {
                "title": f"PostgreSQL connection refused {marker}",
                "summary": "The PostgreSQL database listener refused a connection.",
                "severity": "High",
                "services": "postgresql",
                "logs": "FATAL connection refused by PostgreSQL database listener",
            },
        )
        unrelated_analysis = request_json(
            f"/api/incidents/{unrelated['incident_id']}/analyze",
            {},
        )
        assert all(
            match.get("incident_id") != incident_a["incident_id"]
            for match in unrelated_analysis["historical_matches"]
        )
    finally:
        stop_backend(process)


@pytest.mark.skipif(
    not RUN_LIVE_INTEGRATION_TESTS or not GROQ_CONFIGURED,
    reason="Opt in with INCIDENTIQ_RUN_LIVE_INTEGRATION_TESTS=1 and configure GROQ_API_KEY.",
)
def test_live_groq_analysis_request() -> None:
    async def scenario() -> None:
        result = await GroqIncidentAnalyzer().analyze(
            {
                "incident_id": f"INC-LIVE-{uuid.uuid4().hex[:8]}",
                "title": "Live provider connectivity check",
                "summary": "A test-only request to verify Groq credentials and model access.",
                "services": "api",
                "logs": "ERROR API returned HTTP 502 during a test-only request.",
            },
            [],
        )
        assert result.get("provider") == "groq"
        assert result.get("root_cause")

    asyncio.run(scenario())
