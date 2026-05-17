"""FHIR R4 → OMOP-inspired record transformers.

Each function takes a FHIR resource (and, for non-Patient resources, the
{source_patient_id: person_id} lookup produced by inserting Patients first)
and returns a dict shaped for the corresponding `fhir_demo_*` table.

Simplified on purpose:
    * Happy-path field layouts only — no defensive parsing of edge cases.
    * No concept-id resolution (real OMOP ETL would map every source code
      through OHDSI vocabulary tables to a canonical concept_id).
    * `first_coding` returns only the first coding from a CodeableConcept.
"""
import logging
from datetime import date, datetime
from typing import Dict, List, Optional, Tuple

from app import terminology

logger = logging.getLogger(__name__)


# Kept for backwards compatibility — the source-of-truth for recognized
# systems now lives in `terminology.TERMINOLOGY_SYSTEMS`. This view exists
# so existing callers and tests don't break.
RECOGNIZED_CODING_SYSTEMS: Dict[str, str] = {
    url: meta["name"] for url, meta in terminology.TERMINOLOGY_SYSTEMS.items()
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def patient_ref_to_id(subject_ref: Optional[str]) -> Optional[str]:
    """Pull the id off a FHIR reference like 'Patient/patient-smith-001'."""
    if not subject_ref:
        return None
    return subject_ref.split("/", 1)[1] if "/" in subject_ref else subject_ref


def first_coding(codeable_concept: Optional[dict]) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Return `(code, display, system)` for the first coding in a CodeableConcept.

    Falls back to the concept's `.text` for `display` when there are no
    codings at all — covers the case where a source system only sent free text.
    """
    if not codeable_concept:
        return None, None, None
    codings = codeable_concept.get("coding") or []
    if not codings:
        return None, codeable_concept.get("text"), None
    first = codings[0]
    return first.get("code"), first.get("display"), first.get("system")


def parse_fhir_date(date_str: Optional[str]) -> Optional[date]:
    """Parse a FHIR date or dateTime string into a Python `date`.

    FHIR allows YYYY, YYYY-MM, YYYY-MM-DD, or a full ISO-8601 datetime. We
    take the first 10 chars as a fallback when the full ISO parser fails.
    """
    if not date_str:
        return None
    try:
        return datetime.fromisoformat(date_str.replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return datetime.strptime(date_str[:10], "%Y-%m-%d").date()
        except ValueError:
            logger.warning("Could not parse FHIR date: %s", date_str)
            return None


def _resolve_person(resource: dict, person_id_lookup: Dict[str, int]) -> Optional[int]:
    source_patient = patient_ref_to_id((resource.get("subject") or {}).get("reference"))
    person_id = person_id_lookup.get(source_patient)
    if person_id is None:
        logger.warning(
            "%s %s references unknown patient %s — skipping",
            resource.get("resourceType"), resource.get("id"), source_patient,
        )
    return person_id


# ---------------------------------------------------------------------------
# Resource transformers
# ---------------------------------------------------------------------------
def transform_patient(resource: dict) -> dict:
    """Patient → fhir_demo_person row."""
    return {
        "source_patient_id": resource.get("id"),
        "gender":            resource.get("gender"),
        "birth_date":        parse_fhir_date(resource.get("birthDate")),
    }


def transform_encounter(resource: dict, person_id_lookup: Dict[str, int]) -> Optional[dict]:
    """Encounter → fhir_demo_visit_occurrence row, or None if the patient is unknown."""
    person_id = _resolve_person(resource, person_id_lookup)
    if person_id is None:
        return None
    period = resource.get("period") or {}
    klass = resource.get("class") or {}
    return {
        "person_id":        person_id,
        "encounter_id":     resource.get("id"),
        "visit_start_date": parse_fhir_date(period.get("start")),
        "visit_end_date":   parse_fhir_date(period.get("end")),
        "visit_type":       klass.get("display") or klass.get("code"),
    }


def transform_condition(resource: dict, person_id_lookup: Dict[str, int]) -> Optional[dict]:
    """Condition → fhir_demo_condition_occurrence row."""
    person_id = _resolve_person(resource, person_id_lookup)
    if person_id is None:
        return None
    code, display, system = first_coding(resource.get("code"))
    return {
        "person_id":           person_id,
        "condition_code":      code,
        "condition_display":   display,
        "coding_system":       system,
        "condition_start_date": parse_fhir_date(
            resource.get("onsetDateTime") or resource.get("recordedDate")
        ),
    }


def transform_observation(resource: dict, person_id_lookup: Dict[str, int]) -> Optional[dict]:
    """Observation → fhir_demo_measurement row.

    Only numeric (`valueQuantity`) observations are surfaced. Coded/string
    observations are out of scope for this demo.
    """
    person_id = _resolve_person(resource, person_id_lookup)
    if person_id is None:
        return None
    code, display, _system = first_coding(resource.get("code"))
    quantity = resource.get("valueQuantity") or {}
    return {
        "person_id":           person_id,
        "measurement_code":    code,
        "measurement_display": display,
        "value_numeric":       quantity.get("value"),
        "unit":                quantity.get("unit"),
        "measurement_date":    parse_fhir_date(resource.get("effectiveDateTime")),
    }


def transform_medication_request(resource: dict, person_id_lookup: Dict[str, int]) -> Optional[dict]:
    """MedicationRequest → fhir_demo_drug_exposure row."""
    person_id = _resolve_person(resource, person_id_lookup)
    if person_id is None:
        return None
    code, display, system = first_coding(resource.get("medicationCodeableConcept"))
    return {
        "person_id":     person_id,
        "drug_code":     code,
        "drug_display":  display,
        "coding_system": system,
        "start_date":    parse_fhir_date(resource.get("authoredOn")),
    }


# ---------------------------------------------------------------------------
# Code mapping report
# ---------------------------------------------------------------------------
def build_mapping_report_row(
    resource_type: str,
    source_code: Optional[str],
    coding_system: Optional[str],
) -> dict:
    """One row of fhir_demo_code_mapping_report.

    Delegates to :func:`terminology.classify_coding` so the report agrees
    with the Clinical Terminology Explorer on what counts as 'mapped' and
    distinguishes the four statuses: ``mapped``, ``system_known_code_unknown``,
    ``unsupported_system``, ``missing``. The ``mapped_successfully`` boolean
    is preserved as the row-level summary the metric card reads.
    """
    classification = terminology.classify_coding(coding_system, source_code)
    return {
        "resource_type":       resource_type,
        "source_code":         source_code,
        "coding_system":       coding_system,
        "mapped_successfully": classification["mapped_successfully"],
        "notes":               classification["notes"],
    }


# (resource_type, FHIR field that holds the coded concept)
_CODED_RESOURCE_FIELDS = [
    ("Condition",         "code"),
    ("Observation",       "code"),
    ("MedicationRequest", "medicationCodeableConcept"),
]


def build_mapping_report_rows(grouped: Dict[str, List[dict]]) -> List[dict]:
    """Produce one mapping-report row per coded resource across all groups."""
    rows: List[dict] = []
    for resource_type, code_field in _CODED_RESOURCE_FIELDS:
        for resource in grouped.get(resource_type, []):
            code, _display, system = first_coding(resource.get(code_field))
            rows.append(build_mapping_report_row(resource_type, code, system))
    return rows
