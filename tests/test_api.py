"""Smoke every endpoint through the FastAPI TestClient.

Validates HTTP shapes against the pydantic models the routes declare.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_healthz(clean_db):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"ok": True, "db": "reachable"}


def test_reset(clean_db):
    r = client.post("/reset")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert isinstance(body["elapsed_ms"], int)


def test_full_happy_path(clean_db):
    ingest = client.post("/ingest/sample", headers={"Idempotency-Key": "k-ingest"})
    assert ingest.status_code == 200, ingest.text
    ingest_body = ingest.json()
    assert ingest_body["bundle_count"] == 3
    assert ingest_body["raw_count"] > 0

    transform = client.post("/transform", headers={"Idempotency-Key": "k-tx"})
    assert transform.status_code == 200, transform.text
    counts = transform.json()["counts"]
    assert counts["persons"] == 3

    dashboard = client.get("/dashboard")
    assert dashboard.status_code == 200, dashboard.text
    body = dashboard.json()
    assert body["summary"]["patients"] == 3
    assert isinstance(body["analytics"]["conditions_by_frequency"], list)
    assert "fhir_demo_person" in body["omop_tables"]
    assert isinstance(body["mapping_report"], list)


def test_ingest_with_caller_supplied_bundle(clean_db):
    # Minimal valid Bundle with one Patient.
    payload = {
        "bundles": [
            {
                "resourceType": "Bundle",
                "entry": [
                    {
                        "resource": {
                            "resourceType": "Patient",
                            "id": "patient-test-001",
                            "gender": "male",
                            "birthDate": "1980-01-01",
                        }
                    }
                ],
            }
        ],
        "source_label": "test-upload",
    }
    r = client.post("/ingest", json=payload)
    assert r.status_code == 200, r.text
    assert r.json()["raw_count"] == 1
    assert r.json()["bundle_count"] == 1
