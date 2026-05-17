"""Curated biomedical terminology metadata for the FHIR → OMOP demo.

Purpose
-------
FHIR defines the *structure* of clinical data; terminologies define the
*meaning* of the codes inside it. This module provides:

  * Static metadata for the handful of recognized standard coding systems
    used in this portfolio demo (LOINC, SNOMED CT, ICD-10 / ICD-10-CM,
    RxNorm) plus MeSH as an educational reference.
  * A small **curated** code-to-display mapping per system — just the codes
    used in the bundled sample data. A real terminology service would be
    backed by the full vocabulary tables (OHDSI Athena, UMLS, NLM RxNorm
    files, etc.) or by a terminology server (HL7 TS / SNOMED Snowstorm /
    LOINC FHIR endpoint).
  * Pure helper functions to extract codings from FHIR resources, classify
    them, and roll them up into per-coding rows for the Clinical Terminology
    Explorer tab.

Everything in this module is intentionally lightweight and offline:
no network calls, no terminology server, no licensed downloads.
"""
from __future__ import annotations

import logging
from collections import Counter
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Recognized coding-system metadata
# ---------------------------------------------------------------------------
# `target_table` is the OMOP-inspired demo table that resources coded with
# this system flow into. `where_in_fhir` documents the typical FHIR path
# where this system shows up in our sample data.
TERMINOLOGY_SYSTEMS: Dict[str, Dict[str, str]] = {
    "http://loinc.org": {
        "name": "LOINC",
        "category": "Labs / observations",
        "target_table": "fhir_demo_measurement",
        "where_in_fhir": "Observation.code.coding[].system",
        "what_it_is": (
            "Logical Observation Identifiers Names and Codes — a universal "
            "standard for identifying laboratory and clinical observations."
        ),
        "demo_usage": (
            "LOINC-coded FHIR Observation resources are mapped into "
            "fhir_demo_measurement and drive the measurements-over-time chart."
        ),
        "analytics_use": "Lab and vital-sign trend analytics.",
    },
    "http://snomed.info/sct": {
        "name": "SNOMED CT",
        "category": "Clinical conditions / findings",
        "target_table": "fhir_demo_condition_occurrence",
        "where_in_fhir": "Condition.code.coding[].system",
        "what_it_is": (
            "Systematized Nomenclature of Medicine — Clinical Terms. A "
            "comprehensive clinical terminology for diagnoses, findings, "
            "procedures, and body structures."
        ),
        "demo_usage": (
            "SNOMED-coded FHIR Condition resources are mapped into "
            "fhir_demo_condition_occurrence and drive the conditions-by-"
            "frequency chart."
        ),
        "analytics_use": "Diagnosis and clinical-finding cohort analytics.",
    },
    "http://hl7.org/fhir/sid/icd-10-cm": {
        "name": "ICD-10-CM",
        "category": "Diagnosis classification (US clinical modification)",
        "target_table": "fhir_demo_condition_occurrence",
        "where_in_fhir": "Condition.code.coding[].system",
        "what_it_is": (
            "International Classification of Diseases, 10th Revision, "
            "Clinical Modification — the US billing/diagnosis classification "
            "system."
        ),
        "demo_usage": (
            "ICD-10-CM-coded Condition resources flow into the same "
            "fhir_demo_condition_occurrence table as SNOMED-coded ones."
        ),
        "analytics_use": "Diagnosis-driven analytics and billing-aligned cohorts.",
    },
    "http://hl7.org/fhir/sid/icd-10": {
        "name": "ICD-10",
        "category": "Diagnosis classification (WHO international)",
        "target_table": "fhir_demo_condition_occurrence",
        "where_in_fhir": "Condition.code.coding[].system",
        "what_it_is": (
            "International Classification of Diseases, 10th Revision — "
            "the WHO's international diagnosis classification."
        ),
        "demo_usage": (
            "ICD-10-coded Condition resources flow into the same "
            "fhir_demo_condition_occurrence table as SNOMED-coded ones."
        ),
        "analytics_use": "Diagnosis-driven analytics; less granular than ICD-10-CM.",
    },
    "http://www.nlm.nih.gov/research/umls/rxnorm": {
        "name": "RxNorm",
        "category": "Medications",
        "target_table": "fhir_demo_drug_exposure",
        "where_in_fhir": "MedicationRequest.medicationCodeableConcept.coding[].system",
        "what_it_is": (
            "NLM's normalized naming system for clinical drugs — branded and "
            "generic, with strength, dose form, and ingredient relationships."
        ),
        "demo_usage": (
            "RxNorm-coded MedicationRequest resources are mapped into "
            "fhir_demo_drug_exposure and drive the drug-exposures chart."
        ),
        "analytics_use": "Medication prescribing and adherence analytics.",
    },
}

# ---------------------------------------------------------------------------
# Curated demo code → display name mappings
# ---------------------------------------------------------------------------
# Just the codes we expect to see in the sample bundles + the codes called
# out in the spec. A real ETL would not hard-code these — it would resolve
# every (system, code) pair through the appropriate vocabulary table.
DEMO_CODE_MAPPINGS: Dict[str, Dict[str, str]] = {
    "http://loinc.org": {
        "4548-4":  "Hemoglobin A1c",
        "8480-6":  "Systolic blood pressure",
        "8462-4":  "Diastolic blood pressure",
        "2339-0":  "Glucose [Mass/volume] in Blood",
        # also present in the bundled sample data
        "29463-7": "Body weight",
        "39156-5": "Body mass index (BMI)",
        "2160-0":  "Creatinine [Mass/volume] in Serum or Plasma",
        "33914-3": "Glomerular filtration rate (eGFR)",
        "13457-7": "Cholesterol in LDL",
    },
    "http://snomed.info/sct": {
        "44054006":  "Diabetes mellitus type 2",
        "38341003":  "Hypertensive disorder",
        "195967001": "Asthma",
        # also present in the bundled sample data
        "59621000":  "Essential hypertension",
        "433144002": "Chronic kidney disease stage 3",
        "55822004":  "Hyperlipidemia",
    },
    "http://hl7.org/fhir/sid/icd-10-cm": {
        "E11.9":   "Type 2 diabetes mellitus without complications",
        "I10":     "Essential (primary) hypertension",
        "J45.909": "Unspecified asthma, uncomplicated",
    },
    "http://hl7.org/fhir/sid/icd-10": {
        # Same diagnoses, WHO codes (often identical to ICD-10-CM at this level)
        "E11":  "Type 2 diabetes mellitus",
        "I10":  "Essential (primary) hypertension",
        "J45":  "Asthma",
    },
    "http://www.nlm.nih.gov/research/umls/rxnorm": {
        "860975": "Metformin 500 MG",
        "312961": "Lisinopril 10 MG",
        "197361": "Amlodipine 5 MG",
        # also present in the bundled sample data
        "29046":  "Lisinopril 10 MG Oral Tablet",
        "6809":   "Metformin 500 MG Oral Tablet",
        "83367":  "Atorvastatin 20 MG Oral Tablet",
        "35208":  "Ramipril 5 MG Oral Capsule",
    },
}

# MeSH — included for educational awareness only. Not part of the
# transformation pipeline because MeSH indexes biomedical literature
# (PubMed/MEDLINE articles), not patient-level clinical events.
MESH_INFO: Dict[str, str] = {
    "name": "MeSH",
    "category": "Biomedical literature indexing",
    "target_table": "(not used by this pipeline)",
    "what_it_is": (
        "Medical Subject Headings — the National Library of Medicine's "
        "controlled vocabulary used to index articles in PubMed/MEDLINE."
    ),
    "demo_usage": (
        "Included as educational reference. MeSH appears in literature "
        "metadata, not in patient-level FHIR resources, so this pipeline "
        "does not consume it."
    ),
    "analytics_use": "Literature search / evidence retrieval (out of scope here).",
}


# Resource type → which FHIR field holds its CodeableConcept(s).
# Encounter.type[] is a *list* of CodeableConcepts; the others are single.
_RESOURCE_CODE_FIELDS: Dict[str, Dict[str, str]] = {
    "Condition":         {"field": "code",                       "shape": "single"},
    "Observation":       {"field": "code",                       "shape": "single"},
    "MedicationRequest": {"field": "medicationCodeableConcept",  "shape": "single"},
    "Encounter":         {"field": "type",                       "shape": "list"},
}


# ---------------------------------------------------------------------------
# Classification result statuses (returned by classify_coding)
# ---------------------------------------------------------------------------
STATUS_MAPPED              = "mapped"
STATUS_SYSTEM_KNOWN_CODE_UNKNOWN = "system_known_code_unknown"
STATUS_UNSUPPORTED_SYSTEM  = "unsupported_system"
STATUS_MISSING             = "missing"


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------
def normalize_system_url(system: Optional[str]) -> str:
    """Lowercase + strip trailing slash so trivial URL differences don't miss."""
    return (system or "").strip().lower().rstrip("/")


_NORMALIZED_SYSTEM_INDEX: Dict[str, str] = {
    normalize_system_url(url): url for url in TERMINOLOGY_SYSTEMS
}


def identify_terminology_system(system: Optional[str]) -> Dict[str, Optional[str]]:
    """Return metadata dict for a coding system, or `recognized=False` if unknown."""
    canonical = _NORMALIZED_SYSTEM_INDEX.get(normalize_system_url(system))
    if canonical:
        meta = TERMINOLOGY_SYSTEMS[canonical]
        return {"recognized": True, "system_url": canonical, **meta}
    return {
        "recognized":    False,
        "system_url":    system,
        "name":          None,
        "category":      None,
        "target_table":  None,
        "where_in_fhir": None,
        "what_it_is":    None,
        "demo_usage":    None,
        "analytics_use": None,
    }


def lookup_demo_code(system: Optional[str], code: Optional[str]) -> Dict[str, Optional[str]]:
    """Check whether `(system, code)` is in our curated demo mapping table."""
    if not code:
        return {"in_demo_mapping": False, "demo_display": None}
    canonical = _NORMALIZED_SYSTEM_INDEX.get(normalize_system_url(system))
    if not canonical:
        return {"in_demo_mapping": False, "demo_display": None}
    codes = DEMO_CODE_MAPPINGS.get(canonical, {})
    if code in codes:
        return {"in_demo_mapping": True, "demo_display": codes[code]}
    return {"in_demo_mapping": False, "demo_display": None}


def classify_coding(system: Optional[str], code: Optional[str]) -> Dict[str, str]:
    """Return one of four classification statuses + a human-friendly note.

    Used by both the Clinical Terminology Explorer and the legacy
    fhir_demo_code_mapping_report so the two views agree on what counts as
    'mapped'.
    """
    sys_info = identify_terminology_system(system)
    has_system = bool(system)
    has_code = bool(code)

    if not has_system and not has_code:
        return {
            "status":              STATUS_MISSING,
            "mapped_successfully": False,
            "notes":               "No coding system or code supplied on the resource",
        }
    if not has_system:
        return {
            "status":              STATUS_MISSING,
            "mapped_successfully": False,
            "notes":               "Code supplied without a coding system",
        }
    if not sys_info["recognized"]:
        return {
            "status":              STATUS_UNSUPPORTED_SYSTEM,
            "mapped_successfully": False,
            "notes":               f"Unsupported coding system: {system}",
        }
    if not has_code:
        return {
            "status":              STATUS_MISSING,
            "mapped_successfully": False,
            "notes":               f"{sys_info['name']} system supplied without a code",
        }
    demo = lookup_demo_code(system, code)
    if demo["in_demo_mapping"]:
        return {
            "status":              STATUS_MAPPED,
            "mapped_successfully": True,
            "notes":               f"{sys_info['name']} code recognized in demo mapping table",
        }
    return {
        "status":              STATUS_SYSTEM_KNOWN_CODE_UNKNOWN,
        "mapped_successfully": False,
        "notes":               f"{sys_info['name']} system recognized, code {code!r} not in demo mappings",
    }


# ---------------------------------------------------------------------------
# Resource → codings
# ---------------------------------------------------------------------------
def extract_codings_from_resource(resource: dict) -> List[Dict[str, Optional[str]]]:
    """Pull every coding out of a single FHIR resource's known coded fields.

    Returns one dict per Coding entry. Resource types not in
    ``_RESOURCE_CODE_FIELDS`` are silently ignored — keeping this pure
    happy-path. Encounter.type is handled as a list of CodeableConcepts
    (per FHIR R4) — the others are single CodeableConcept.
    """
    rtype = resource.get("resourceType")
    spec = _RESOURCE_CODE_FIELDS.get(rtype)
    if not spec:
        return []

    field = spec["field"]
    if spec["shape"] == "list":
        concepts = resource.get(field) or []
    else:
        concept = resource.get(field)
        concepts = [concept] if concept else []

    rid = resource.get("id")
    out: List[Dict[str, Optional[str]]] = []
    for concept in concepts:
        for coding in (concept.get("coding") or []):
            out.append({
                "resource_type": rtype,
                "resource_id":   rid,
                "field":         field,
                "system":        coding.get("system"),
                "code":          coding.get("code"),
                "display":       coding.get("display") or concept.get("text"),
            })
    return out


def build_terminology_rows(resources: List[dict]) -> List[Dict[str, Optional[str]]]:
    """Produce one normalized row per coding across a collection of resources.

    Each row has the fields specified in the explorer requirements:

        resource_type, resource_id, terminology_name, terminology_category,
        system_url, code, display, mapped_successfully, target_table,
        explanation, analytics_use, status

    The combination of ``status`` + ``mapped_successfully`` is what the UI
    metric cards filter on (mapped / system-known / unsupported / missing).
    """
    rows: List[Dict[str, Optional[str]]] = []
    for resource in resources:
        for coding in extract_codings_from_resource(resource):
            system = coding.get("system")
            code = coding.get("code")
            sys_info = identify_terminology_system(system)
            classification = classify_coding(system, code)

            rows.append({
                "resource_type":        coding["resource_type"],
                "resource_id":          coding["resource_id"],
                "terminology_name":     sys_info["name"]          or "(unsupported)",
                "terminology_category": sys_info["category"]      or "(unknown)",
                "system_url":           system,
                "code":                 code,
                "display":              coding.get("display"),
                "mapped_successfully":  classification["mapped_successfully"],
                "status":               classification["status"],
                "target_table":         sys_info["target_table"]  or "(none)",
                "explanation":          _build_explanation(coding, sys_info, classification),
                "analytics_use":        sys_info["analytics_use"] or "(out of scope here)",
            })
    return rows


def _build_explanation(
    coding: Dict[str, Optional[str]],
    sys_info: Dict[str, Optional[str]],
    classification: Dict[str, str],
) -> str:
    """Construct a friendly one-paragraph explanation for a row."""
    if classification["status"] == STATUS_MAPPED:
        return (
            f"{sys_info['name']} codes identify {sys_info['category'].lower()}. "
            f"This code maps to the OMOP-inspired {sys_info['target_table']} table "
            f"and is used for {sys_info['analytics_use'].lower()}."
        )
    if classification["status"] == STATUS_SYSTEM_KNOWN_CODE_UNKNOWN:
        return (
            f"{sys_info['name']} is a recognized terminology, but this specific "
            f"code is not in the curated demo mapping table. It would still flow "
            f"into {sys_info['target_table']} in this pipeline — production ETL "
            f"would resolve it via the full vocabulary."
        )
    if classification["status"] == STATUS_UNSUPPORTED_SYSTEM:
        return (
            f"The coding system {coding.get('system')!r} is not one of the "
            f"recognized standard vocabularies in this demo (LOINC, SNOMED CT, "
            f"ICD-10/ICD-10-CM, RxNorm). Real ETL would flag this for mapping."
        )
    # STATUS_MISSING
    return (
        "The resource has no coding system or no code value. Production "
        "pipelines surface these as data-quality issues for upstream review."
    )


# ---------------------------------------------------------------------------
# Summary metrics — for the Explorer's metric cards
# ---------------------------------------------------------------------------
def summarize_terminology_rows(rows: List[Dict[str, Optional[str]]]) -> Dict[str, int]:
    """Roll up rows into the five counts shown on the metric cards."""
    statuses = Counter(r["status"] for r in rows)
    known_systems = {
        r["terminology_name"] for r in rows
        if r["status"] in (STATUS_MAPPED, STATUS_SYSTEM_KNOWN_CODE_UNKNOWN)
    }
    return {
        "total_codings":      len(rows),
        "known_systems":      len(known_systems),
        "mapped":             statuses.get(STATUS_MAPPED, 0),
        "unsupported":        statuses.get(STATUS_UNSUPPORTED_SYSTEM, 0),
        "missing_or_unknown": statuses.get(STATUS_MISSING, 0)
                              + statuses.get(STATUS_SYSTEM_KNOWN_CODE_UNKNOWN, 0),
    }
