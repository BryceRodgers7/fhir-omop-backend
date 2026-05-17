# Architecture

How the FHIR-OMOP backend is put together and the reasoning behind each
decision.

---

## 1. Background

The FHIR-OMOP page in the parent Streamlit portfolio (`pages/fhir_omop.py`)
originally did its database work in-process. Symptoms in production
(Fly.io → Supabase):

* Blank white page hangs after clicking **Load** or **Run Transformation**.
* No log lines from the click handler — the script was blocked inside
  `psycopg2.connect()` in C, so stdout buffers never flushed.
* Each page rerun opened ~17 separate Supabase connections (one per
  rendered tab / chart), amplifying the chance of a stalled handshake.

The Streamlit page holding a websocket open during a slow / flaky DB
operation is fragile by design. Pulling the work into a separate service
makes the failure modes:

* **Surfaceable** — connection errors become HTTP 5xx, not blank screens.
* **Bounded** — `connect_timeout` + retry budget at the backend.
* **Idempotent** — clients can safely retry without duplicating data.

---

## 2. System context

```
   ┌─────────────────────────────┐         ┌─────────────────────────────┐
   │ Streamlit portfolio page    │  HTTP   │ This service                │
   │ (pages/fhir_omop.py)        │ ──────▶ │ FHIR-OMOP backend (FastAPI) │
   │ - api_client.py             │  JSON   │ - app/main.py routes        │
   │ - terminology.py (local)    │ ◀────── │ - psycopg2 to Supabase      │
   └─────────────────────────────┘         └──────────────┬──────────────┘
                                                          │ psycopg2
                                                          ▼
                                              ┌─────────────────────────┐
                                              │ Supabase Postgres        │
                                              │ fhir_demo_* tables       │
                                              └─────────────────────────┘
```

The Streamlit page used to talk to Supabase directly from inside its
websocket handler. This service owns the database connection so failures
become HTTP 5xx and the page can render an error state without losing its
session.

The Streamlit side is a dumb HTTP client. All DB writes flow through
`/ingest`, `/ingest/sample`, `/transform`, or `/reset`. The whole page is
fed by a single `GET /dashboard` per rerun.

---

## 3. Stack

| Layer | Choice | Notes |
| --- | --- | --- |
| Web framework | FastAPI 0.115 | Pydantic-typed routes, auto-OpenAPI at `/docs`. |
| ASGI server | Uvicorn 0.30 (standard) | Single worker is enough for a single viewer. |
| DB driver | psycopg2-binary 2.9 | No ORM. The pipeline is short SQL — no abstraction layer earns its keep. |
| Validation | pydantic v2 | Bundled with FastAPI. |
| Runtime | Python 3.11-slim Docker | Matches the parent app's image. |
| Deploy target | Fly.io | One small machine, optional auto-stop. |

No SQLAlchemy, no Celery/RQ, no migration framework. The schema is owned
by `sql/001_create_tables.sql` and applied with `psql` once per environment.

---

## 4. Module layout

```
app/
├── main.py          # FastAPI routes + error envelope + optional X-API-Key auth
├── db.py            # Sole psycopg2 entry point: get_connection() w/ retry
├── pipeline.py      # Single-transaction ingest + transform
├── analytics.py     # Read-side queries returning JSON-serializable dicts
├── idempotency.py   # Thread-safe in-process LRU cache
├── schemas.py       # pydantic request/response models
├── transformers.py  # Pure-compute FHIR → OMOP row builders
├── fhir_loader.py   # Pure-compute bundle parsing + grouping
├── terminology.py   # Pure-compute terminology classifier
└── sample_data/     # Three Synthea-style FHIR Bundles bundled with the image
sql/
└── 001_create_tables.sql
tests/
├── conftest.py      # `clean_db` fixture — TRUNCATEs before/after each test
├── test_pipeline.py # Real DB, asserts contract counts
├── test_idempotency.py
└── test_api.py      # httpx TestClient over the FastAPI app
```

**Layering rule:** only `app/db.py` imports `psycopg2.connect`. Every other
module receives an open `conn` from `get_connection()`. That keeps the
connection lifecycle in one place and makes it impossible for a route to
accidentally bypass the retry policy.

---

## 5. Request lifecycle

```
HTTP request
    │
    ▼
FastAPI route (app/main.py)
    │   - Pydantic validates request body / headers
    │   - require_api_key dependency runs if API_SHARED_SECRET is set
    │   - Idempotency-Key lookup for /ingest and /transform
    │
    ▼
get_connection()  (app/db.py — contextmanager)
    │   - psycopg2.connect(..., connect_timeout=2)
    │   - retry up to 3 attempts on OperationalError during handshake
    │   - exponential backoff (200/400/800 ms)
    │
    ▼
pipeline / analytics  (app/pipeline.py, app/analytics.py)
    │   - All work for one request runs inside ONE connection
    │   - Writes are one transaction → one COMMIT
    │   - Reads are multiple SELECTs on the same conn
    │
    ▼
Response model serialized by FastAPI
    │   - Pydantic shape guaranteed by the route's response_model
    │   - Idempotency cache stored on first successful write
    │
    ▼
HTTP response
```

Errors collapse to a uniform envelope: `{"error": "...", "detail": "..."}`,
status code per the failure mode (validation errors via FastAPI's default
422, anything else 500).

---

## 6. Connection management and retry

`app/db.py:get_connection()` is the only place psycopg2 is touched. Three
parameters control its behavior:

| Env var | Default | Effect |
| --- | --- | --- |
| `DB_CONNECT_TIMEOUT` | 2 (seconds) | Per-handshake `connect_timeout`. |
| `DB_MAX_ATTEMPTS` | 3 | Total handshake attempts before raising. |
| `DB_BACKOFF_MS` | 200 | Initial sleep between retries; doubles each attempt. |

Worst-case latency before raising is `(3 × 2)s connect + (200 + 400) ms backoff
≈ 6.6 s`.

**What is and isn't retried:** retries apply only to
`psycopg2.OperationalError` raised *during* `connect()`. Query failures
after the handshake propagate immediately so a partial write is never
silently re-issued.

---

## 7. Single-transaction pipeline

Two public entry points in `app/pipeline.py`, each one transaction:

### `bulk_ingest(conn, bundles, source_label) → (run_id, raw_count, bundle_count)`

```
INSERT  fhir_demo_ingestion_run (status='in_progress')   RETURNING id
INSERT  fhir_demo_raw_fhir_resource (...) VALUES ... -- execute_values bulk
UPDATE  fhir_demo_ingestion_run SET completed_at=now(), status='loaded'
COMMIT
```

Three round-trips collapsed into one transaction so the user-perceived
latency is dominated by Supabase handshake, not by row volume.

### `run_transform(conn) → counts`

```
TRUNCATE fhir_demo_person, fhir_demo_visit_occurrence,
         fhir_demo_condition_occurrence, fhir_demo_measurement,
         fhir_demo_drug_exposure, fhir_demo_code_mapping_report
         RESTART IDENTITY CASCADE

SELECT   resource_type, resource_json FROM fhir_demo_raw_fhir_resource

-- per-row INSERT ... ON CONFLICT (source_patient_id) DO UPDATE RETURNING id
-- to build the {source_patient_id → person_id} lookup
INSERT   fhir_demo_person                  (...) ...

-- bulk inserts on the same cursor
INSERT   fhir_demo_visit_occurrence        (...) -- execute_values
INSERT   fhir_demo_condition_occurrence    (...)
INSERT   fhir_demo_measurement             (...)
INSERT   fhir_demo_drug_exposure           (...)
INSERT   fhir_demo_code_mapping_report     (...)

COMMIT
```

Everything is one COMMIT — readers either see the previous derivation or
the new one, never a partial state.

---

## 8. Rebuild semantics

The OMOP-side tables (`fhir_demo_person`, `visit_occurrence`,
`condition_occurrence`, `measurement`, `drug_exposure`,
`code_mapping_report`) are treated as a **deterministic projection of
`fhir_demo_raw_fhir_resource`**. Every call to `run_transform` truncates
the OMOP tables and re-derives them from whatever is currently in raw.

Raw and `fhir_demo_ingestion_run` are **append-only history**. They are
only cleared by `/reset`.

### Why

Earlier behavior treated the OMOP tables as accumulating: each transform
appended new rows derived from new raw. That's appealing for incremental
ingest but it leaks any duplicate raw rows directly into the dashboard.
The Patient table happened to deduplicate (because of the `UNIQUE
(source_patient_id)` constraint and `ON CONFLICT DO UPDATE`) but every
other table did not, so loading the sample bundle twice produced one
patient and N×2 encounters / conditions / measurements / drugs.

Rebuilding-from-raw resolves that:

* The dashboard always reflects "what's currently in raw."
* Duplicate raw rows do not amplify into the OMOP side — running the
  transform after a duplicate load is a no-op for the user beyond the
  ingestion_run row.
* No surrogate-key invention needed in the schema; conditions and
  measurements never had a natural unique key, so per-table ON CONFLICT
  was never viable anyway.

### Cost

`person_id` and the other OMOP serial PKs renumber every transform. That
is acceptable here because nothing outside this service holds those IDs.
If a downstream consumer ever needs stable OMOP IDs, switch to a hash-based
deterministic key derived from raw source IDs.

### Reset vs Transform

| Endpoint | What it wipes | What stays |
| --- | --- | --- |
| `POST /reset` | All `fhir_demo_*` tables (raw + ingestion_run + OMOP) | Nothing |
| `POST /transform` | OMOP tables only | Raw + ingestion_run |

The Streamlit page's "Reset Demo Data" button maps to `/reset`. No client
change is required for the rebuild-on-transform behavior — `/transform`
keeps the same request and response shape.

---

## 9. Idempotency

Two writes can corrupt data on naive retry: `/ingest` and `/transform`.
`/reset` is naturally idempotent (TRUNCATE).

### Mechanism

Clients send `Idempotency-Key: <uuid4>`. The backend caches
`(key → response)` in an in-process LRU (256 entries, 10-minute TTL,
thread-safe — see `app/idempotency.py`). On retry with the same key the
cached response is returned without re-running the operation.

This is fine for a single-instance deployment. If the service is ever
scaled horizontally, move the cache to Postgres:

```sql
CREATE TABLE fhir_demo_idempotency (
    key         TEXT PRIMARY KEY,
    response    JSONB NOT NULL,
    expires_at  TIMESTAMP NOT NULL
);
```

### Note on rebuild + idempotency

Because `/transform` is now idempotent on raw (same raw → same OMOP), the
Idempotency-Key header is less load-bearing than it used to be. It still
prevents an accidental double-charge of transform latency, but the data
hazard it originally guarded against — duplicate inserts — is gone.

---

## 10. One fat `/dashboard` endpoint

The Streamlit page renders ~17 panels per rerun. Earlier designs gave it
per-resource endpoints (`/raw`, `/omop/person`, etc.). That fan-out
recreated the same N-connections-per-rerun problem we built the service
to escape.

`GET /dashboard` runs every SELECT inside a single connection and returns
the full page payload:

```json
{
  "summary": {
    "raw_resources": 24, "patients": 3, "encounters": 5,
    "conditions": 8, "measurements": 12, "drug_exposures": 6
  },
  "mapping_success_rate": 78.5,
  "raw_resources_by_type": { "Patient": [], "Encounter": [] },
  "omop_tables": {
    "fhir_demo_person": [],
    "fhir_demo_visit_occurrence": [],
    "fhir_demo_condition_occurrence": [],
    "fhir_demo_measurement": [],
    "fhir_demo_drug_exposure": []
  },
  "mapping_report": [],
  "analytics": {
    "conditions_by_frequency": [{"condition": "...", "occurrences": 4}],
    "measurements_over_time":  [{"date": "2024-01-01", "measurements": 3}],
    "encounters_by_type":      [{"visit_type": "...", "encounters": 2}],
    "drug_counts":             [{"drug": "...", "prescriptions": 1}]
  }
}
```

Empty tables return empty arrays; the client renders "no data" states.

This is the right shape **for this demo's scale**. If a second consumer
ever needs slices of the dashboard, add per-resource endpoints then —
don't preempt.

---

## 11. Endpoint surface

| Method | Path | Body / headers | Returns |
| --- | --- | --- | --- |
| POST | `/reset` | — | `{ok, elapsed_ms}` |
| POST | `/ingest/sample` | `Idempotency-Key?` | `{run_id, raw_count, bundle_count, elapsed_ms}` |
| POST | `/ingest` | `IngestRequest`, `Idempotency-Key?` | same as `/ingest/sample` |
| POST | `/transform` | `Idempotency-Key?` | `{counts, elapsed_ms}` |
| GET | `/dashboard` | — | full dashboard payload |
| GET | `/healthz` | — | `{ok, db}` |

Auto-generated OpenAPI lives at `/docs` and `/openapi.json`. The pydantic
models in `app/schemas.py` are the source of truth for shapes.

### Example responses

`POST /ingest/sample`:
```json
{ "run_id": 7, "raw_count": 24, "bundle_count": 3, "elapsed_ms": 856 }
```

`POST /transform`:
```json
{
  "counts": {
    "persons": 3, "visits": 5, "conditions": 8,
    "measurements": 12, "drug_exposures": 6, "mapping_report": 26
  },
  "elapsed_ms": 1240
}
```

`POST /reset`:
```json
{ "ok": true, "elapsed_ms": 412 }
```

`POST /ingest` body shape:
```json
{
  "bundles": [{ "resourceType": "Bundle", "entry": [] }],
  "source_label": "Loaded uploaded bundles"
}
```

All errors use a single envelope: `{"error": "...", "detail": "..."}`.

---

## 12. Database schema

Defined in `sql/001_create_tables.sql`. Two zones:

**Raw / history (append-only):**
* `fhir_demo_ingestion_run` — one row per load
* `fhir_demo_raw_fhir_resource` — every parsed FHIR resource as JSONB

**OMOP-inspired (rebuilt from raw):**
* `fhir_demo_person` — patients, `UNIQUE(source_patient_id)`
* `fhir_demo_visit_occurrence` — encounters
* `fhir_demo_condition_occurrence` — diagnoses
* `fhir_demo_measurement` — numeric observations (LOINC)
* `fhir_demo_drug_exposure` — medication orders (RxNorm)
* `fhir_demo_code_mapping_report` — terminology coverage report

CASCADE is configured on the FK chain so `/reset` works without manual
dependency chasing. The transform's TRUNCATE uses RESTART IDENTITY so
serial PKs reset between runs.

---

## 13. Configuration

| Env var | Required | Default | Purpose |
| --- | --- | --- | --- |
| `SUPADATABASE_URL` | yes | — | Postgres DSN (Supabase). |
| `DB_CONNECT_TIMEOUT` | no | 2 | Seconds per psycopg2 handshake. |
| `DB_MAX_ATTEMPTS` | no | 3 | Total handshake attempts before raising. |
| `DB_BACKOFF_MS` | no | 200 | Initial retry backoff; doubles each attempt. |
| `LOG_LEVEL` | no | INFO | Standard logging level. |
| `API_SHARED_SECRET` | no | — | If set, all routes require `X-API-Key`. |

`SUPADATABASE_URL` is read at request time inside `get_connection()`, not
at import time, so the service can boot for `/docs` exploration even if
secrets aren't wired up yet. (It will still fail any non-healthcheck
request without a DB URL.)

---

## 14. Auth

Two modes, controlled by whether `API_SHARED_SECRET` is set:

**Off (default):** every route is open. Pair this with Fly's internal
6PN network — set `internal_port = 8080` in `fly.toml`, point the
Streamlit page at `http://fhir-omop-api.flycast/dashboard`. Nothing
outside the org can reach the service.

**On (set `API_SHARED_SECRET`):** every route requires `X-API-Key:
<value>`. The check is wired as a FastAPI dependency on the `FastAPI`
constructor, so it covers every route automatically including `/healthz`.

For a portfolio demo the trade-off is: Off is simpler; On is more
demonstrable from a localhost browser without a tunnel.

---

## 15. Deployment

Single Fly machine in `ord`, autoscale 0–1. Fly's load balancer cold-starts
the machine on the first request after idle (~1s added latency).
Set `min_machines_running = 1` in `fly.toml` if cold starts are
unacceptable (~$2/mo).

### Initial deploy

```bash
fly launch --copy-config --no-deploy
fly secrets set SUPADATABASE_URL="postgres://..."
fly deploy
```

### Schema bootstrap (one-time, against Supabase directly)

```bash
psql "$SUPADATABASE_URL" -f sql/001_create_tables.sql
```

There's no migration framework. The CREATE TABLE statements are
`IF NOT EXISTS`, so re-running the script is safe.

---

## 16. Local development

```powershell
python -m venv .venv
.venv\Scripts\activate                   # Windows
# source .venv/bin/activate              # macOS/Linux
pip install -r requirements.txt
$env:SUPADATABASE_URL = "postgres://..."  # PowerShell
# export SUPADATABASE_URL=...             # bash
uvicorn app.main:app --reload --port 8080
```

OpenAPI explorer: <http://localhost:8080/docs>

### Smoke tests

```bash
curl -X POST http://localhost:8080/reset
curl -X POST http://localhost:8080/ingest/sample \
     -H "Idempotency-Key: $(uuidgen)"
curl -X POST http://localhost:8080/transform \
     -H "Idempotency-Key: $(uuidgen)"
curl http://localhost:8080/dashboard | jq .summary
```

---

## 17. Streamlit client integration

The parent Streamlit page is a dumb HTTP client of this service. Its
integration contract:

* `projects/fhir_omop/pipeline/api_client.py` mirrors the public surface
  in §11. New endpoints here should be mirrored into the client.
* The page makes **one** `GET /dashboard` call per rerun to render all
  tabs — no per-tab DB calls.
* Write requests (`/ingest/*`, `/transform`) send `Idempotency-Key`
  headers, reusing the same key across automatic retries.
* The client retries once on `requests.ConnectionError` / 5xx with a
  2-second pause and surfaces the result via `st.status`.
* The page shows a configuration banner if `FHIR_OMOP_API_URL` is unset.
* `app/terminology.py` is duplicated on the Streamlit side (pure compute)
  so the Clinical Terminology Explorer tab can run client-side on the
  dashboard payload without another round-trip.

---

## 18. Concurrency model

Single uvicorn worker; FastAPI routes are sync functions; psycopg2 is
synchronous. Each request occupies one OS thread for the duration of its
DB work.

This is sufficient for the demo (one viewer, small data, transforms in
~1s). Two simultaneous transforms could race — both would truncate the
OMOP side, both would re-derive, and the result is whichever committed
last. If the service ever sees real concurrency, wrap `run_transform` in
a Postgres advisory lock (`SELECT pg_advisory_xact_lock(42)`) so only one
transform runs at a time.

The idempotency cache is `threading.Lock`-protected, so concurrent
requests with the same key are safe.

---

## 19. Testing strategy

All tests hit a real Postgres (Supabase). The `clean_db` fixture in
`tests/conftest.py` TRUNCATEs every `fhir_demo_*` table before and after
each test so cases are independent.

> Don't mock the database in the pipeline tests. Mocked DB tests passed
> while a real migration broke production once already; we're not doing
> that again.

The three test files cover, in order:

* `test_pipeline.py` — end-to-end ingest+transform against the bundled
  sample data, asserts the contract counts (3 persons, etc).
* `test_idempotency.py` — LRU behaviour + endpoint-level "same key twice"
  test.
* `test_api.py` — every route through `httpx.TestClient`, shape
  validation against the pydantic response models.

---

## 20. What this service doesn't do

* **Concept-ID resolution.** Real OMOP ETL maps every (system, code) pair
  through OHDSI vocabulary tables. We classify codings via the curated
  `app/terminology.py` mapping and surface coverage in
  `fhir_demo_code_mapping_report`. The output is OMOP-*inspired*, not OMOP.
* **Sustained Supabase outages.** Retries help one-off blips, not 30
  seconds of packet loss. Those surface as 5xx and the client shows
  "backend unreachable, retrying…". That's the floor.
* **Fly.io cold starts.** With `min_machines_running = 0`, the first
  request after idle wakes the machine (~1s).
* **Horizontal scaling.** Single instance, single worker. The in-process
  idempotency cache and the lack of a transform-level advisory lock are
  both single-instance assumptions.
* **Schema migrations.** `sql/001_create_tables.sql` is the only schema;
  the assumption is the demo schema is stable.

---

## 21. Extending the service

* **New endpoint:** add a pydantic model in `app/schemas.py`, write the
  query in `app/analytics.py` (read) or `app/pipeline.py` (write), wire
  the route in `app/main.py`. Mirror the public surface into
  `projects/fhir_omop/pipeline/api_client.py` on the Streamlit side.
* **New OMOP table:** add it to `sql/001_create_tables.sql`, add an entry
  to `OMOP_TABLES_IN_RESET_ORDER` in `app/pipeline.py` so the rebuild
  picks it up, add the corresponding `INSERT` block in `run_transform`,
  and surface it in `/dashboard`.
* **Different terminology source:** `app/terminology.py` is the only
  place that decides what counts as a recognized coding system. Swapping
  in a real terminology server (LOINC FHIR, SNOMED Snowstorm) means
  replacing the curated dict with a call out — the call site in
  `transformers.py` does not change.
