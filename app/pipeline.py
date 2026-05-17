"""Single-transaction ingest + transform for the FHIR-OMOP backend.

Two public entry points, each one connection and one COMMIT:

* :func:`bulk_ingest` — lands raw FHIR resources in
  ``fhir_demo_raw_fhir_resource``, bookended by an ``fhir_demo_ingestion_run``
  row.
* :func:`run_transform` — reads the raw landing zone, builds OMOP-inspired
  rows, and writes person + visit + condition + measurement + drug_exposure
  + mapping_report in one transaction.

Callers pass in the open psycopg2 connection (from ``app.db.get_connection``).
That keeps psycopg2 imports out of the route layer and lets the routes own
the request-scoped transaction.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Dict, Iterable, List, Tuple

import psycopg2.extras

from app import transformers
from app.fhir_loader import group_bundles_by_resource_type

logger = logging.getLogger(__name__)


# Listed children-before-parents so TRUNCATE works even without CASCADE
# chasing. CASCADE is still used in the SQL because of the FK chain through
# ingestion_run + person.
FHIR_DEMO_TABLES_IN_RESET_ORDER: List[str] = [
    "fhir_demo_code_mapping_report",
    "fhir_demo_drug_exposure",
    "fhir_demo_measurement",
    "fhir_demo_condition_occurrence",
    "fhir_demo_visit_occurrence",
    "fhir_demo_person",
    "fhir_demo_raw_fhir_resource",
    "fhir_demo_ingestion_run",
]


# ---------------------------------------------------------------------------
# Reset
# ---------------------------------------------------------------------------
def reset_demo_data(conn) -> None:
    """Truncate every fhir_demo_* table and restart their SERIAL sequences."""
    sql = (
        "TRUNCATE TABLE "
        + ", ".join(FHIR_DEMO_TABLES_IN_RESET_ORDER)
        + " RESTART IDENTITY CASCADE;"
    )
    with conn.cursor() as cur:
        cur.execute(sql)
    conn.commit()
    logger.info("fhir_demo_* tables truncated")


# ---------------------------------------------------------------------------
# Ingest
# ---------------------------------------------------------------------------
def bulk_ingest(
    conn,
    bundles: List[dict],
    source_label: str,
) -> Tuple[int, int, int]:
    """Land FHIR Bundles' resources in the landing zone — single transaction.

    Args:
        conn: open psycopg2 connection (caller owns lifecycle).
        bundles: list of FHIR Bundle dicts.
        source_label: free-text label stored on the ingestion run row.

    Returns:
        (ingestion_run_id, raw_count, bundle_count)
    """
    grouped = group_bundles_by_resource_type(bundles)
    resources: List[dict] = []
    for items in grouped.values():
        resources.extend(items)
    raw_count = len(resources)
    bundle_count = len(bundles)

    t_open = time.perf_counter()
    logger.info(
        "bulk_ingest: %d bundle(s), %d resource(s) prepared (source=%r)",
        bundle_count, raw_count, source_label,
    )

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO fhir_demo_ingestion_run (status, notes) "
            "VALUES (%s, %s) RETURNING ingestion_run_id",
            ("in_progress", source_label),
        )
        run_id = cur.fetchone()[0]
        logger.info("bulk_ingest: opened ingestion run #%d", run_id)

        if raw_count:
            rows = [
                (run_id, r.get("resourceType", "Unknown"), json.dumps(r))
                for r in resources
            ]
            psycopg2.extras.execute_values(
                cur,
                "INSERT INTO fhir_demo_raw_fhir_resource "
                "(ingestion_run_id, resource_type, resource_json) VALUES %s",
                rows,
            )

        cur.execute(
            """
            UPDATE fhir_demo_ingestion_run
            SET completed_at = CURRENT_TIMESTAMP,
                status       = %s,
                notes        = %s
            WHERE ingestion_run_id = %s
            """,
            ("loaded", f"{source_label} - {raw_count} raw resources", run_id),
        )
    conn.commit()
    logger.info(
        "bulk_ingest: committed run #%d (%d row(s), %.3fs)",
        run_id, raw_count, time.perf_counter() - t_open,
    )
    return run_id, raw_count, bundle_count


# ---------------------------------------------------------------------------
# Transform
# ---------------------------------------------------------------------------
def _fetch_raw_grouped(cur) -> Dict[str, List[dict]]:
    cur.execute(
        "SELECT resource_type, resource_json "
        "FROM fhir_demo_raw_fhir_resource ORDER BY resource_id"
    )
    grouped: Dict[str, List[dict]] = {}
    for resource_type, resource_json in cur.fetchall():
        grouped.setdefault(resource_type, []).append(resource_json)
    return grouped


def run_transform(conn) -> Dict[str, int]:
    """Run the full OMOP-inspired transform in ONE connection / ONE tx.

    The previous flow opened up to seven separate Supabase connections per
    click. Collapsing the writes into a single connection eliminates the
    fan-out and turns the whole pipeline into one COMMIT.
    """
    t_open = time.perf_counter()
    counts: Dict[str, int] = {
        "persons": 0, "visits": 0, "conditions": 0,
        "measurements": 0, "drug_exposures": 0, "mapping_report": 0,
    }
    person_lookup: Dict[str, int] = {}

    with conn.cursor() as cur:
        grouped = _fetch_raw_grouped(cur)
        logger.info(
            "run_transform: starting (Patient=%d, Encounter=%d, Condition=%d, "
            "Observation=%d, MedicationRequest=%d)",
            len(grouped.get("Patient", [])),
            len(grouped.get("Encounter", [])),
            len(grouped.get("Condition", [])),
            len(grouped.get("Observation", [])),
            len(grouped.get("MedicationRequest", [])),
        )

        person_rows = [
            transformers.transform_patient(p) for p in grouped.get("Patient", [])
        ]

        # Per-row upsert is required because we need (source_id -> person_id)
        # correlation row-by-row to wire up child events.
        for p in person_rows:
            cur.execute(
                """
                INSERT INTO fhir_demo_person (source_patient_id, gender, birth_date)
                VALUES (%s, %s, %s)
                ON CONFLICT (source_patient_id) DO UPDATE
                    SET gender = EXCLUDED.gender, birth_date = EXCLUDED.birth_date
                RETURNING person_id, source_patient_id
                """,
                (p["source_patient_id"], p["gender"], p["birth_date"]),
            )
            person_id, source_id = cur.fetchone()
            person_lookup[source_id] = person_id
        counts["persons"] = len(person_lookup)

        def _t_many(resources: Iterable[dict], fn) -> List[dict]:
            return [r for r in (fn(x, person_lookup) for x in resources) if r is not None]

        visits = _t_many(grouped.get("Encounter", []), transformers.transform_encounter)
        conditions = _t_many(grouped.get("Condition", []), transformers.transform_condition)
        measures = _t_many(grouped.get("Observation", []), transformers.transform_observation)
        drugs = _t_many(grouped.get("MedicationRequest", []), transformers.transform_medication_request)
        mapping = transformers.build_mapping_report_rows(grouped)

        def _bulk(stmt: str, values: list) -> int:
            if not values:
                return 0
            psycopg2.extras.execute_values(cur, stmt, values)
            return len(values)

        counts["visits"] = _bulk(
            "INSERT INTO fhir_demo_visit_occurrence "
            "(person_id, encounter_id, visit_start_date, visit_end_date, visit_type) VALUES %s",
            [(r["person_id"], r["encounter_id"], r["visit_start_date"],
              r["visit_end_date"], r["visit_type"]) for r in visits],
        )
        counts["conditions"] = _bulk(
            "INSERT INTO fhir_demo_condition_occurrence "
            "(person_id, condition_code, condition_display, coding_system, condition_start_date) VALUES %s",
            [(r["person_id"], r["condition_code"], r["condition_display"],
              r["coding_system"], r["condition_start_date"]) for r in conditions],
        )
        counts["measurements"] = _bulk(
            "INSERT INTO fhir_demo_measurement "
            "(person_id, measurement_code, measurement_display, value_numeric, unit, measurement_date) VALUES %s",
            [(r["person_id"], r["measurement_code"], r["measurement_display"],
              r["value_numeric"], r["unit"], r["measurement_date"]) for r in measures],
        )
        counts["drug_exposures"] = _bulk(
            "INSERT INTO fhir_demo_drug_exposure "
            "(person_id, drug_code, drug_display, coding_system, start_date) VALUES %s",
            [(r["person_id"], r["drug_code"], r["drug_display"],
              r["coding_system"], r["start_date"]) for r in drugs],
        )
        counts["mapping_report"] = _bulk(
            "INSERT INTO fhir_demo_code_mapping_report "
            "(resource_type, source_code, coding_system, mapped_successfully, notes) VALUES %s",
            [(r["resource_type"], r["source_code"], r["coding_system"],
              r["mapped_successfully"], r["notes"]) for r in mapping],
        )

    conn.commit()
    logger.info(
        "run_transform: committed in %.3fs - counts=%s",
        time.perf_counter() - t_open, counts,
    )
    return counts
