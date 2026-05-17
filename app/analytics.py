"""SQL-backed analytics for the FHIR-OMOP dashboard.

Every function accepts an open psycopg2 connection (the route opens one
connection per /dashboard request) and returns plain Python lists/dicts/
scalars. The whole dashboard collapses ~17 separate Supabase round-trips
into a single connection.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import psycopg2.extras

logger = logging.getLogger(__name__)


def _fetch_all_dicts(conn, sql: str, params: tuple = ()) -> List[Dict[str, Any]]:
    """Run a SELECT and return rows as a list of plain dicts (JSON-serializable)."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params)
        return [dict(row) for row in cur.fetchall()]


def get_summary_counts(conn) -> Dict[str, int]:
    """Counts for the metric cards."""
    counts: Dict[str, int] = {}
    with conn.cursor() as cur:
        for label, table in [
            ("raw_resources",  "fhir_demo_raw_fhir_resource"),
            ("patients",       "fhir_demo_person"),
            ("encounters",     "fhir_demo_visit_occurrence"),
            ("conditions",     "fhir_demo_condition_occurrence"),
            ("measurements",   "fhir_demo_measurement"),
            ("drug_exposures", "fhir_demo_drug_exposure"),
        ]:
            cur.execute(f"SELECT COUNT(*) FROM {table}")
            counts[label] = cur.fetchone()[0]
    return counts


def get_mapping_success_rate(conn) -> Optional[float]:
    """Percentage of mapping_report rows where mapped_successfully = TRUE.

    Returns None when no mapping rows exist yet, so the UI renders '—'
    instead of a misleading '0.0%'.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                SUM(CASE WHEN mapped_successfully THEN 1 ELSE 0 END)::float AS mapped,
                COUNT(*)::float AS total
            FROM fhir_demo_code_mapping_report
            """
        )
        mapped, total = cur.fetchone()
    if not total:
        return None
    return round((mapped / total) * 100.0, 1)


def get_conditions_by_frequency(conn) -> List[Dict[str, Any]]:
    return _fetch_all_dicts(
        conn,
        """
        SELECT condition_display AS condition, COUNT(*) AS occurrences
        FROM fhir_demo_condition_occurrence
        WHERE condition_display IS NOT NULL
        GROUP BY condition_display
        ORDER BY occurrences DESC
        """,
    )


def get_measurements_over_time(conn) -> List[Dict[str, Any]]:
    return _fetch_all_dicts(
        conn,
        """
        SELECT measurement_date AS date, COUNT(*) AS measurements
        FROM fhir_demo_measurement
        WHERE measurement_date IS NOT NULL
        GROUP BY measurement_date
        ORDER BY measurement_date
        """,
    )


def get_encounter_counts_by_type(conn) -> List[Dict[str, Any]]:
    return _fetch_all_dicts(
        conn,
        """
        SELECT COALESCE(visit_type, 'unspecified') AS visit_type, COUNT(*) AS encounters
        FROM fhir_demo_visit_occurrence
        GROUP BY visit_type
        ORDER BY encounters DESC
        """,
    )


def get_drug_counts(conn) -> List[Dict[str, Any]]:
    return _fetch_all_dicts(
        conn,
        """
        SELECT drug_display AS drug, COUNT(*) AS prescriptions
        FROM fhir_demo_drug_exposure
        WHERE drug_display IS NOT NULL
        GROUP BY drug_display
        ORDER BY prescriptions DESC
        """,
    )


def get_mapping_report(conn) -> List[Dict[str, Any]]:
    return _fetch_all_dicts(
        conn,
        """
        SELECT resource_type, source_code, coding_system, mapped_successfully, notes
        FROM fhir_demo_code_mapping_report
        ORDER BY mapped_successfully ASC, resource_type, source_code
        """,
    )


# ---------------------------------------------------------------------------
# Helpers used by /dashboard to surface the raw + OMOP tables themselves.
# ---------------------------------------------------------------------------
def fetch_table(conn, table_name: str) -> List[Dict[str, Any]]:
    """SELECT * FROM <fhir_demo_*> table, guarded by a prefix whitelist."""
    if not table_name.startswith("fhir_demo_"):
        raise ValueError(f"Refusing to query non-demo table: {table_name}")
    return _fetch_all_dicts(conn, f"SELECT * FROM {table_name} ORDER BY 1")


def fetch_raw_resources_by_type(conn) -> Dict[str, List[Dict[str, Any]]]:
    """Group every row in fhir_demo_raw_fhir_resource by resource_type."""
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "SELECT resource_id, ingestion_run_id, resource_type, resource_json "
            "FROM fhir_demo_raw_fhir_resource ORDER BY resource_id"
        )
        for row in cur.fetchall():
            grouped.setdefault(row["resource_type"], []).append(dict(row))
    return grouped
