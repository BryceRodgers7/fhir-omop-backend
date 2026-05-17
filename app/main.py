"""FastAPI entry point for the FHIR-OMOP backend.

Endpoints (see FHIR_OMOP_BACKEND_PLAN.md §4):
    POST /reset           - TRUNCATE every fhir_demo_* table
    POST /ingest/sample   - load bundled sample patients
    POST /ingest          - load caller-supplied bundles
    POST /transform       - run the OMOP-inspired transform
    GET  /dashboard       - everything the Streamlit page needs to render
    GET  /healthz         - liveness + DB reachability probe

All errors return {"error": str, "detail": str|None}.
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse

from app import analytics, pipeline
from app.db import get_connection
from app.fhir_loader import discover_bundle_files, load_bundle
from app.idempotency import cache as idem_cache
from app.schemas import (
    DashboardAnalytics,
    DashboardOmopTables,
    DashboardResponse,
    ErrorResponse,
    HealthResponse,
    IngestRequest,
    IngestResponse,
    ResetResponse,
    SummaryCounts,
    TransformCounts,
    TransformResponse,
)

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

SAMPLE_DATA_DIR = Path(__file__).resolve().parent / "sample_data"
API_SHARED_SECRET = os.getenv("API_SHARED_SECRET")


# ---------------------------------------------------------------------------
# Optional shared-secret auth (no-op if API_SHARED_SECRET is unset).
# ---------------------------------------------------------------------------
def require_api_key(x_api_key: Optional[str] = Header(default=None, alias="X-API-Key")) -> None:
    if not API_SHARED_SECRET:
        return
    if x_api_key != API_SHARED_SECRET:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or missing X-API-Key")


app = FastAPI(
    title="FHIR-OMOP Backend",
    description="Owns all DB I/O for the FHIR -> OMOP demo.",
    version="1.0.0",
    dependencies=[Depends(require_api_key)],
)


# ---------------------------------------------------------------------------
# Error envelope
# ---------------------------------------------------------------------------
@app.exception_handler(HTTPException)
async def http_exception_handler(_: Request, exc: HTTPException) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content=ErrorResponse(error=str(exc.detail or "HTTP error"), detail=None).model_dump(),
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(_: Request, exc: Exception) -> JSONResponse:
    logger.exception("Unhandled exception: %s", exc)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content=ErrorResponse(error="internal_error", detail=str(exc)).model_dump(),
    )


# ---------------------------------------------------------------------------
# Liveness
# ---------------------------------------------------------------------------
@app.get("/healthz", response_model=HealthResponse)
def healthz() -> HealthResponse:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
    return HealthResponse(ok=True, db="reachable")


# ---------------------------------------------------------------------------
# Write endpoints
# ---------------------------------------------------------------------------
@app.post("/reset", response_model=ResetResponse)
def reset() -> ResetResponse:
    t0 = time.perf_counter()
    with get_connection() as conn:
        pipeline.reset_demo_data(conn)
    return ResetResponse(ok=True, elapsed_ms=int((time.perf_counter() - t0) * 1000))


def _maybe_cached(idem_key: Optional[str]) -> Optional[dict]:
    if not idem_key:
        return None
    return idem_cache.get(idem_key)


def _store_idempotent(idem_key: Optional[str], payload: dict) -> None:
    if idem_key:
        idem_cache.put(idem_key, payload)


@app.post("/ingest/sample", response_model=IngestResponse)
def ingest_sample(
    idem_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
) -> dict:
    cached = _maybe_cached(idem_key)
    if cached is not None:
        return cached

    t0 = time.perf_counter()
    bundle_paths = discover_bundle_files(SAMPLE_DATA_DIR)
    bundles = [load_bundle(p) for p in bundle_paths]
    with get_connection() as conn:
        run_id, raw_count, bundle_count = pipeline.bulk_ingest(
            conn, bundles, source_label="Loaded sample data"
        )

    payload = IngestResponse(
        run_id=run_id,
        raw_count=raw_count,
        bundle_count=bundle_count,
        elapsed_ms=int((time.perf_counter() - t0) * 1000),
    ).model_dump()
    _store_idempotent(idem_key, payload)
    return payload


@app.post("/ingest", response_model=IngestResponse)
def ingest(
    body: IngestRequest,
    idem_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
) -> dict:
    cached = _maybe_cached(idem_key)
    if cached is not None:
        return cached

    t0 = time.perf_counter()
    with get_connection() as conn:
        run_id, raw_count, bundle_count = pipeline.bulk_ingest(
            conn, body.bundles, source_label=body.source_label
        )

    payload = IngestResponse(
        run_id=run_id,
        raw_count=raw_count,
        bundle_count=bundle_count,
        elapsed_ms=int((time.perf_counter() - t0) * 1000),
    ).model_dump()
    _store_idempotent(idem_key, payload)
    return payload


@app.post("/transform", response_model=TransformResponse)
def transform(
    idem_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
) -> dict:
    cached = _maybe_cached(idem_key)
    if cached is not None:
        return cached

    t0 = time.perf_counter()
    with get_connection() as conn:
        counts = pipeline.run_transform(conn)

    payload = TransformResponse(
        counts=TransformCounts(**counts),
        elapsed_ms=int((time.perf_counter() - t0) * 1000),
    ).model_dump()
    _store_idempotent(idem_key, payload)
    return payload


# ---------------------------------------------------------------------------
# Read: consolidated dashboard
# ---------------------------------------------------------------------------
@app.get("/dashboard", response_model=DashboardResponse)
def dashboard() -> DashboardResponse:
    """Everything the Streamlit page needs to render, in one connection."""
    with get_connection() as conn:
        summary = analytics.get_summary_counts(conn)
        mapping_rate = analytics.get_mapping_success_rate(conn)
        raw_by_type = analytics.fetch_raw_resources_by_type(conn)
        omop = DashboardOmopTables(
            fhir_demo_person=analytics.fetch_table(conn, "fhir_demo_person"),
            fhir_demo_visit_occurrence=analytics.fetch_table(conn, "fhir_demo_visit_occurrence"),
            fhir_demo_condition_occurrence=analytics.fetch_table(conn, "fhir_demo_condition_occurrence"),
            fhir_demo_measurement=analytics.fetch_table(conn, "fhir_demo_measurement"),
            fhir_demo_drug_exposure=analytics.fetch_table(conn, "fhir_demo_drug_exposure"),
        )
        mapping_report = analytics.get_mapping_report(conn)
        ana = DashboardAnalytics(
            conditions_by_frequency=analytics.get_conditions_by_frequency(conn),
            measurements_over_time=analytics.get_measurements_over_time(conn),
            encounters_by_type=analytics.get_encounter_counts_by_type(conn),
            drug_counts=analytics.get_drug_counts(conn),
        )

    return DashboardResponse(
        summary=SummaryCounts(**summary),
        mapping_success_rate=mapping_rate,
        raw_resources_by_type=raw_by_type,
        omop_tables=omop,
        mapping_report=mapping_report,
        analytics=ana,
    )
