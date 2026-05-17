"""Pydantic request/response models for the FHIR-OMOP backend."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Common
# ---------------------------------------------------------------------------
class ErrorResponse(BaseModel):
    error: str
    detail: Optional[str] = None


# ---------------------------------------------------------------------------
# /healthz
# ---------------------------------------------------------------------------
class HealthResponse(BaseModel):
    ok: bool
    db: str


# ---------------------------------------------------------------------------
# /reset
# ---------------------------------------------------------------------------
class ResetResponse(BaseModel):
    ok: bool
    elapsed_ms: int


# ---------------------------------------------------------------------------
# /ingest, /ingest/sample
# ---------------------------------------------------------------------------
class IngestRequest(BaseModel):
    bundles: List[Dict[str, Any]] = Field(..., description="List of FHIR Bundle resources.")
    source_label: str = Field(default="Loaded uploaded bundles")


class IngestResponse(BaseModel):
    run_id: int
    raw_count: int
    bundle_count: int
    elapsed_ms: int


# ---------------------------------------------------------------------------
# /transform
# ---------------------------------------------------------------------------
class TransformCounts(BaseModel):
    persons: int
    visits: int
    conditions: int
    measurements: int
    drug_exposures: int
    mapping_report: int


class TransformResponse(BaseModel):
    counts: TransformCounts
    elapsed_ms: int


# ---------------------------------------------------------------------------
# /dashboard
# ---------------------------------------------------------------------------
class SummaryCounts(BaseModel):
    raw_resources: int
    patients: int
    encounters: int
    conditions: int
    measurements: int
    drug_exposures: int


class DashboardOmopTables(BaseModel):
    fhir_demo_person: List[Dict[str, Any]]
    fhir_demo_visit_occurrence: List[Dict[str, Any]]
    fhir_demo_condition_occurrence: List[Dict[str, Any]]
    fhir_demo_measurement: List[Dict[str, Any]]
    fhir_demo_drug_exposure: List[Dict[str, Any]]


class DashboardAnalytics(BaseModel):
    conditions_by_frequency: List[Dict[str, Any]]
    measurements_over_time: List[Dict[str, Any]]
    encounters_by_type: List[Dict[str, Any]]
    drug_counts: List[Dict[str, Any]]


class DashboardResponse(BaseModel):
    summary: SummaryCounts
    mapping_success_rate: Optional[float]
    raw_resources_by_type: Dict[str, List[Dict[str, Any]]]
    omop_tables: DashboardOmopTables
    mapping_report: List[Dict[str, Any]]
    analytics: DashboardAnalytics
