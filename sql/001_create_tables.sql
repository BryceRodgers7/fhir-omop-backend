-- FHIR-to-OMOP demo schema.
--
-- All tables for this portfolio demo. They share the public schema with the
-- rest of the application's tables and use the `fhir_demo_` prefix to keep
-- the namespace tidy. This is a SIMPLIFIED, OMOP-INSPIRED layout for
-- demonstration purposes only — it is not the OMOP CDM, and the concept_id
-- resolution that real OMOP ETL performs is intentionally out of scope.
--
-- Schema changes are applied manually (no migration framework). Re-running
-- this script is safe — every statement uses IF NOT EXISTS.

-- ---------------------------------------------------------------------------
-- Ingestion run tracking — one row per load of FHIR bundles.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS fhir_demo_ingestion_run (
    ingestion_run_id SERIAL PRIMARY KEY,
    started_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    completed_at     TIMESTAMP,
    status           VARCHAR(50) NOT NULL DEFAULT 'in_progress',
    notes            TEXT
);

-- ---------------------------------------------------------------------------
-- Raw landing zone — every parsed FHIR resource is dropped here as JSONB
-- before any transformation runs. This is the "schema-on-read" surface that
-- healthcare ETL typically keeps as a permanent record of what was received.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS fhir_demo_raw_fhir_resource (
    resource_id        SERIAL PRIMARY KEY,
    ingestion_run_id   INTEGER REFERENCES fhir_demo_ingestion_run(ingestion_run_id) ON DELETE CASCADE,
    resource_type      VARCHAR(100) NOT NULL,
    resource_json      JSONB NOT NULL
);

-- ---------------------------------------------------------------------------
-- OMOP-inspired patient / event tables.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS fhir_demo_person (
    person_id          SERIAL PRIMARY KEY,
    source_patient_id  VARCHAR(255) NOT NULL UNIQUE,
    gender             VARCHAR(20),
    birth_date         DATE
);

CREATE TABLE IF NOT EXISTS fhir_demo_visit_occurrence (
    visit_occurrence_id SERIAL PRIMARY KEY,
    person_id           INTEGER REFERENCES fhir_demo_person(person_id) ON DELETE CASCADE,
    encounter_id        VARCHAR(255),
    visit_start_date    DATE,
    visit_end_date      DATE,
    visit_type          VARCHAR(100)
);

CREATE TABLE IF NOT EXISTS fhir_demo_condition_occurrence (
    condition_occurrence_id SERIAL PRIMARY KEY,
    person_id               INTEGER REFERENCES fhir_demo_person(person_id) ON DELETE CASCADE,
    condition_code          VARCHAR(100),
    condition_display       TEXT,
    coding_system           VARCHAR(255),
    condition_start_date    DATE
);

CREATE TABLE IF NOT EXISTS fhir_demo_measurement (
    measurement_id      SERIAL PRIMARY KEY,
    person_id           INTEGER REFERENCES fhir_demo_person(person_id) ON DELETE CASCADE,
    measurement_code    VARCHAR(100),
    measurement_display TEXT,
    value_numeric       DECIMAL(12, 4),
    unit                VARCHAR(100),
    measurement_date    DATE
);

CREATE TABLE IF NOT EXISTS fhir_demo_drug_exposure (
    drug_exposure_id SERIAL PRIMARY KEY,
    person_id        INTEGER REFERENCES fhir_demo_person(person_id) ON DELETE CASCADE,
    drug_code        VARCHAR(100),
    drug_display     TEXT,
    coding_system    VARCHAR(255),
    start_date       DATE
);

-- ---------------------------------------------------------------------------
-- Code mapping report — one row per coded source resource, recording whether
-- the source coding system is one of the recognized standard vocabularies.
-- Real OMOP ETL maps each source code to a canonical concept_id using the
-- OHDSI vocabulary tables; this demo only flags presence vs. absence.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS fhir_demo_code_mapping_report (
    mapping_id          SERIAL PRIMARY KEY,
    resource_type       VARCHAR(100),
    source_code         VARCHAR(100),
    coding_system       VARCHAR(255),
    mapped_successfully BOOLEAN,
    notes               TEXT
);

-- ---------------------------------------------------------------------------
-- Indexes — small enough that they're not strictly required for the demo
-- data set, but they document the access patterns the dashboard relies on.
-- ---------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_fhir_demo_raw_resource_type ON fhir_demo_raw_fhir_resource(resource_type);
CREATE INDEX IF NOT EXISTS idx_fhir_demo_visit_person     ON fhir_demo_visit_occurrence(person_id);
CREATE INDEX IF NOT EXISTS idx_fhir_demo_condition_person ON fhir_demo_condition_occurrence(person_id);
CREATE INDEX IF NOT EXISTS idx_fhir_demo_measurement_person ON fhir_demo_measurement(person_id);
CREATE INDEX IF NOT EXISTS idx_fhir_demo_drug_person      ON fhir_demo_drug_exposure(person_id);
