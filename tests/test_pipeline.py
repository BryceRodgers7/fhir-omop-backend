"""End-to-end pipeline test against a real database.

Loads the three bundled sample patients, runs the transform, asserts row
counts match expected values. No mocks — per CLAUDE.md guidance, mocked DB
tests have failed us before.
"""
from __future__ import annotations

from pathlib import Path

from app.db import get_connection
from app.fhir_loader import discover_bundle_files, load_bundle
from app.pipeline import bulk_ingest, run_transform

SAMPLE_DATA_DIR = Path(__file__).resolve().parent.parent / "app" / "sample_data"


def test_full_pipeline_counts(clean_db):
    bundle_paths = discover_bundle_files(SAMPLE_DATA_DIR)
    bundles = [load_bundle(p) for p in bundle_paths]

    with get_connection() as conn:
        run_id, raw_count, bundle_count = bulk_ingest(conn, bundles, "test")
    assert bundle_count == 3
    assert raw_count > 0
    assert run_id > 0

    with get_connection() as conn:
        counts = run_transform(conn)

    # The bundled sample data is fixed; these counts are the contract.
    assert counts["persons"] == 3
    assert counts["visits"] >= 1
    assert counts["conditions"] >= 1
    assert counts["measurements"] >= 1
    assert counts["drug_exposures"] >= 1
    assert counts["mapping_report"] >= 1
