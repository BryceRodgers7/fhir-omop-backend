"""Idempotency cache + endpoint behavior."""
from __future__ import annotations

import time

from app.idempotency import IdempotencyCache


def test_cache_returns_stored_value():
    cache = IdempotencyCache()
    cache.put("key-a", {"hello": "world"})
    assert cache.get("key-a") == {"hello": "world"}


def test_cache_expires_after_ttl():
    cache = IdempotencyCache(ttl_seconds=0)
    cache.put("key", {"v": 1})
    time.sleep(0.01)
    assert cache.get("key") is None


def test_cache_evicts_oldest_over_capacity():
    cache = IdempotencyCache(max_entries=2)
    cache.put("a", {"v": 1})
    cache.put("b", {"v": 2})
    cache.put("c", {"v": 3})
    assert cache.get("a") is None
    assert cache.get("b") == {"v": 2}
    assert cache.get("c") == {"v": 3}


def test_endpoint_reuses_cached_response(clean_db):
    """Hitting /ingest/sample twice with the same key must not double-ingest."""
    from fastapi.testclient import TestClient

    from app.db import get_connection
    from app.main import app

    client = TestClient(app)
    key = "11111111-1111-1111-1111-111111111111"

    first = client.post("/ingest/sample", headers={"Idempotency-Key": key})
    assert first.status_code == 200, first.text
    second = client.post("/ingest/sample", headers={"Idempotency-Key": key})
    assert second.status_code == 200, second.text

    assert first.json() == second.json()

    # Verify the DB only contains one ingestion run's worth of raw rows.
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM fhir_demo_raw_fhir_resource")
            raw_count = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM fhir_demo_ingestion_run")
            run_count = cur.fetchone()[0]
    assert raw_count == first.json()["raw_count"]
    assert run_count == 1
