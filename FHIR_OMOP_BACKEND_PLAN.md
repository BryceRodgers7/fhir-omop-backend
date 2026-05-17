# FHIR-OMOP Backend Service — Implementation Plan

A small FastAPI service that owns **all database I/O** for the FHIR → OMOP
demo. The Streamlit portfolio page becomes a dumb HTTP client; the backend
holds the Supabase connection, runs the transforms, and exposes a stable
JSON surface.

This document is self-contained — drop it into a new repo and start
building.

---

## 1. Why this exists

The FHIR-OMOP page in the Streamlit portfolio (`pages/fhir_omop.py`)
originally did its database work in-process. Symptoms in production
(Fly.io → Supabase):

- Blank white page hangs after clicking **Load** or **Run Transformation**.
- No log lines from the click handler — the script was blocked inside
  `psycopg2.connect()` in C, so stdout buffers never flushed.
- Each page rerun opened ~17 separate Supabase connections (one per
  rendered tab / chart), amplifying the chance of a stalled handshake.

The Streamlit page holding a websocket open during a slow / flaky DB
operation is fragile by design. Pulling the work into a separate service
makes the failure modes:

- **Surfaceable** — connection errors become HTTP 5xx, not blank screens.
- **Bounded** — connect_timeout + retry budget at the backend.
- **Idempotent** — clients can safely retry without duplicating data.

---

## 2. Stack

| Piece | Choice | Why |
| --- | --- | --- |
| Web framework | FastAPI | Familiar, pydantic-typed responses, auto-OpenAPI. |
| Server | Uvicorn | Standard. Single worker is fine for demo. |
| DB driver | `psycopg2` | Reuse the existing pipeline code unchanged. |
| Validation | pydantic v2 | Comes with FastAPI. |
| Python | 3.11 | Match the parent app's Docker image. |
| Container | Docker | Deploy to Fly.io as a second app. |

No ORM. No SQLAlchemy. No Celery / RQ — the operations are short enough
to handle inline.

---

## 3. Project layout

```
fhir-omop-api/
├── app/
│   ├── __init__.py
│   ├── main.py             # FastAPI app, route definitions
│   ├── db.py               # Connection helper: connect_timeout + retry
│   ├── pipeline.py         # Single-transaction ingest + transform
│   ├── transformers.py     # FHIR → OMOP row builders (copied)
│   ├── analytics.py        # Read-side queries (copied)
│   ├── fhir_loader.py      # JSON parsing + grouping (copied)
│   ├── terminology.py      # Pure-compute terminology classifier (copied)
│   ├── idempotency.py      # Idempotency-key cache (in-process LRU)
│   ├── schemas.py          # pydantic request/response models
│   └── sample_data/        # FHIR Bundle JSON files (copied)
├── sql/
│   └── 001_create_tables.sql   # Identical to existing demo schema
├── tests/
│   ├── test_pipeline.py
│   ├── test_idempotency.py
│   └── test_api.py
├── Dockerfile
├── fly.toml
├── requirements.txt
├── .env.example
└── README.md
```

### Files to copy from the existing repo

From `c:\git\resume-app`:

| Source | Destination | Notes |
| --- | --- | --- |
| `projects/fhir_omop/pipeline/transformers.py` | `app/transformers.py` | No changes. |
| `projects/fhir_omop/pipeline/analytics.py` | `app/analytics.py` | Drop the `DatabaseManager` type hint; inject the connection helper instead. |
| `projects/fhir_omop/pipeline/fhir_loader.py` | `app/fhir_loader.py` | No changes. |
| `projects/fhir_omop/pipeline/terminology.py` | `app/terminology.py` | No changes (pure compute). |
| `projects/fhir_omop/pipeline/db.py` | `app/pipeline.py` | Drop `DatabaseManager` wrapper; use the new `db.get_connection()` directly. |
| `projects/fhir_omop/sample_data/*.json` | `app/sample_data/` | All three patient bundles. |
| `database/fhir_omop_sql/001_create_tables.sql` | `sql/001_create_tables.sql` | Verbatim. |

---

## 4. Endpoint surface

All endpoints return JSON. All error responses use `{"error": "...", "detail": "..."}`.
All write endpoints accept an optional `Idempotency-Key` header (UUIDv4 string).

### Write endpoints

#### `POST /reset`
Truncate every `fhir_demo_*` table. Naturally idempotent — no key needed.

Response:
```json
{ "ok": true, "elapsed_ms": 412 }
```

#### `POST /ingest/sample`
Read the backend's bundled sample data, land it in `fhir_demo_raw_fhir_resource`
in one transaction.

Headers: `Idempotency-Key: <uuid4>` (recommended)

Response:
```json
{
  "run_id": 7,
  "raw_count": 24,
  "bundle_count": 3,
  "elapsed_ms": 856
}
```

#### `POST /ingest`
Land arbitrary FHIR Bundles supplied by the caller.

Headers: `Idempotency-Key: <uuid4>` (recommended)

Body:
```json
{
  "bundles": [ { "resourceType": "Bundle", "entry": [...] }, ... ],
  "source_label": "Loaded uploaded bundles"
}
```

Response: same shape as `/ingest/sample`.

#### `POST /transform`
Run the full OMOP-inspired transform in a single transaction. Reads raw
resources, writes person + visit + condition + measurement + drug_exposure
+ mapping_report in one COMMIT.

Headers: `Idempotency-Key: <uuid4>` (recommended — see §5)

Response:
```json
{
  "counts": {
    "persons": 3,
    "visits": 5,
    "conditions": 8,
    "measurements": 12,
    "drug_exposures": 6,
    "mapping_report": 26
  },
  "elapsed_ms": 1240
}
```

### Read endpoints

#### `GET /dashboard`
Returns **everything the Streamlit page needs to render** in a single response.
Designed to collapse the ~17 connections-per-rerun problem.

Response:
```json
{
  "summary": {
    "raw_resources": 24, "patients": 3, "encounters": 5,
    "conditions": 8, "measurements": 12, "drug_exposures": 6
  },
  "mapping_success_rate": 78.5,
  "raw_resources_by_type": { "Patient": [...], "Encounter": [...] },
  "omop_tables": {
    "fhir_demo_person": [ {...}, ... ],
    "fhir_demo_visit_occurrence": [...],
    "fhir_demo_condition_occurrence": [...],
    "fhir_demo_measurement": [...],
    "fhir_demo_drug_exposure": [...]
  },
  "mapping_report": [ {...}, ... ],
  "analytics": {
    "conditions_by_frequency":    [ {"condition": "...", "occurrences": 4} ],
    "measurements_over_time":     [ {"date": "2024-01-01", "measurements": 3} ],
    "encounters_by_type":         [ {"visit_type": "...", "encounters": 2} ],
    "drug_counts":                [ {"drug": "...", "prescriptions": 1} ]
  }
}
```

The dashboard handler runs all SELECTs inside **one** connection. Empty
tables return empty arrays; the client renders "no data" placeholders.

#### `GET /healthz`
Liveness probe. Returns `{"ok": true, "db": "reachable"}` on success;
opens a connection, runs `SELECT 1`, closes it. Used by Fly's healthcheck.

### Why no individual `GET /raw`, `GET /omop/{table}`, etc.

Earlier drafts had per-resource endpoints. The Streamlit page renders
*all* of those on every interaction — splitting them just resurrects the
N-connection fan-out we're trying to escape. One consolidated dashboard
endpoint is the right shape **for this demo's scale** (small data, single
viewer). Add per-resource endpoints later if a second consumer appears.

---

## 5. Idempotency

Two writes can corrupt data on retry: `/ingest` and `/transform`.
`/reset` is naturally safe (TRUNCATE).

### Mechanism

Client generates a UUIDv4, sends it as `Idempotency-Key`. The backend
stores `(key → cached_response)` in a small in-process LRU (capacity ~256,
TTL ~10 minutes). On retry with the same key, the backend returns the
cached response without re-running the operation.

```python
# app/idempotency.py
from collections import OrderedDict
from time import monotonic
from threading import Lock

_TTL_SECONDS = 600
_MAX_ENTRIES = 256

class IdempotencyCache:
    def __init__(self):
        self._store: OrderedDict[str, tuple[float, dict]] = OrderedDict()
        self._lock = Lock()

    def get(self, key: str) -> dict | None:
        with self._lock:
            entry = self._store.get(key)
            if not entry:
                return None
            ts, value = entry
            if monotonic() - ts > _TTL_SECONDS:
                self._store.pop(key, None)
                return None
            self._store.move_to_end(key)
            return value

    def put(self, key: str, value: dict) -> None:
        with self._lock:
            self._store[key] = (monotonic(), value)
            self._store.move_to_end(key)
            while len(self._store) > _MAX_ENTRIES:
                self._store.popitem(last=False)
```

In-process is fine because the service runs as a **single Fly instance**
(no horizontal scaling for the demo). If you ever scale it horizontally,
move the cache to a Postgres table — `fhir_demo_idempotency (key TEXT
PRIMARY KEY, response JSONB, expires_at TIMESTAMP)`.

### Wiring it into a route

```python
@app.post("/transform")
def transform(idem_key: str | None = Header(default=None, alias="Idempotency-Key")):
    if idem_key:
        cached = idem.get(idem_key)
        if cached:
            return cached
    result = pipeline.run_transform()
    if idem_key:
        idem.put(idem_key, result)
    return result
```

---

## 6. Connection helper + retry

`app/db.py` is the *only* place that opens a Supabase connection. Every
other module receives a connection or a cursor — no module imports
psycopg2 directly.

```python
# app/db.py
import os
import time
import logging
from contextlib import contextmanager
import psycopg2

logger = logging.getLogger(__name__)

DB_URL = os.environ["SUPADATABASE_URL"]        # explicit — fail loud on boot
CONNECT_TIMEOUT = int(os.getenv("DB_CONNECT_TIMEOUT", "2"))
MAX_ATTEMPTS    = int(os.getenv("DB_MAX_ATTEMPTS",    "3"))
BACKOFF_MS      = int(os.getenv("DB_BACKOFF_MS",      "200"))

@contextmanager
def get_connection():
    """Open a Supabase connection with retry on connect-time failures.

    Only retries `psycopg2.OperationalError` raised during the handshake —
    not query-time errors, which indicate real problems with the SQL.
    """
    last_err = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            conn = psycopg2.connect(DB_URL, connect_timeout=CONNECT_TIMEOUT)
            break
        except psycopg2.OperationalError as e:
            last_err = e
            if attempt == MAX_ATTEMPTS:
                raise
            sleep = (BACKOFF_MS / 1000.0) * (2 ** (attempt - 1))
            logger.warning(
                "DB connect failed (attempt %d/%d): %s — retrying in %.2fs",
                attempt, MAX_ATTEMPTS, e, sleep,
            )
            time.sleep(sleep)
    try:
        yield conn
    finally:
        conn.close()
```

Defaults: 2-second connect timeout, 3 attempts, exponential backoff
(200ms → 400ms → 800ms). Worst-case latency before raising = ~1.4s of
sleep + 3 × 2s of handshake attempts = ~7.4s. Tune via env vars.

Retries apply to the **handshake only** — once a connection is open, a
query failure surfaces immediately so we don't blindly retry partial
writes.

---

## 7. Single-transaction pipeline

`app/pipeline.py` mirrors the helpers in the existing
`projects/fhir_omop/pipeline/db.py`, but:

- Takes the open connection as a parameter instead of constructing one.
- Each public function = exactly one transaction.

Two main entry points:

### `bulk_ingest(conn, bundles, source_label) -> (run_id, raw_count)`
1. INSERT into `fhir_demo_ingestion_run`.
2. `execute_values` INSERT into `fhir_demo_raw_fhir_resource`.
3. UPDATE `fhir_demo_ingestion_run` with completion.
4. COMMIT.

### `run_transform(conn) -> counts`
1. SELECT raw resources, grouped by `resource_type`.
2. Per-row upsert INSERT into `fhir_demo_person` (RETURNING for id correlation).
3. `execute_values` INSERT into the four event tables (visits, conditions,
   measurements, drug_exposures).
4. `execute_values` INSERT into `fhir_demo_code_mapping_report`.
5. COMMIT.

Both are already implemented in the working tree of the parent repo —
copy and remove the `DatabaseManager` wrapper.

---

## 8. Configuration

| Env var | Required | Default | Purpose |
| --- | --- | --- | --- |
| `SUPADATABASE_URL` | yes | — | Postgres DSN. Same value the Streamlit app used. |
| `DB_CONNECT_TIMEOUT` | no | `2` | Seconds per psycopg2 handshake. |
| `DB_MAX_ATTEMPTS` | no | `3` | Total handshake attempts before raising. |
| `DB_BACKOFF_MS` | no | `200` | Initial retry backoff (doubles each attempt). |
| `LOG_LEVEL` | no | `INFO` | Standard logging level. |
| `API_SHARED_SECRET` | no | — | If set, all routes require `X-API-Key: <value>`. |

`.env.example`:
```bash
SUPADATABASE_URL=postgres://...
DB_CONNECT_TIMEOUT=2
DB_MAX_ATTEMPTS=3
LOG_LEVEL=INFO
# API_SHARED_SECRET=  # uncomment to enable shared-secret auth
```

---

## 9. Auth

Two acceptable shapes for a portfolio demo:

**Option A — Fly internal network only (simpler)**
Deploy the backend with no public ports, only `internal_port = 8080` on
the Fly 6PN network. The Streamlit app calls
`http://fhir-omop-api.flycast/dashboard`. No auth needed because nothing
outside the org can reach it.

**Option B — Shared-secret header (more demonstrable)**
Public service, all routes guarded by a FastAPI dependency that checks
`X-API-Key` against `API_SHARED_SECRET`. The Streamlit app holds the
same secret as a Fly secret. Slightly more for a recruiter to look at.

Default recommendation: **Option A** for simplicity. Switch to B if you
want to demo the page from `localhost` without a tunnel.

---

## 10. Deployment (Fly.io)

`fly.toml`:
```toml
app = "fhir-omop-api"
primary_region = "ord"

[build]
  dockerfile = "Dockerfile"

[http_service]
  internal_port = 8080
  force_https = true
  auto_stop_machines = "stop"
  auto_start_machines = true
  min_machines_running = 0

  [[http_service.checks]]
    interval = "30s"
    timeout = "5s"
    grace_period = "10s"
    method = "GET"
    path = "/healthz"
```

`Dockerfile` (lean — no PyTorch, no ML deps):
```dockerfile
FROM python:3.11-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev gcc && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app/ ./app/
COPY sql/ ./sql/
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
```

`requirements.txt`:
```
fastapi==0.115.0
uvicorn[standard]==0.30.6
psycopg2-binary==2.9.9
pydantic==2.9.2
```

Initial deploy:
```bash
fly launch --copy-config --no-deploy
fly secrets set SUPADATABASE_URL="postgres://..."
fly deploy
```

Schema bootstrap (one-time, against Supabase directly — same as the
existing demo):
```bash
psql "$SUPADATABASE_URL" -f sql/001_create_tables.sql
```

---

## 11. Local dev

```bash
python -m venv .venv && source .venv/bin/activate   # or .venv\Scripts\activate
pip install -r requirements.txt
export SUPADATABASE_URL="postgres://..."             # or use .env + python-dotenv
uvicorn app.main:app --reload --port 8080
```

Smoke tests:
```bash
curl -X POST http://localhost:8080/reset
curl -X POST http://localhost:8080/ingest/sample \
     -H "Idempotency-Key: $(uuidgen)"
curl -X POST http://localhost:8080/transform \
     -H "Idempotency-Key: $(uuidgen)"
curl http://localhost:8080/dashboard | jq .summary
```

OpenAPI docs auto-mounted at `http://localhost:8080/docs`.

---

## 12. Tests

Minimum useful set — keep it tight:

- `test_pipeline.py` — load the three bundled sample patients, run the
  transform, assert row counts match expected (3 persons, 5 visits, etc).
  Hits a real local Postgres (or Supabase test DB). No mocks.
- `test_idempotency.py` — same `Idempotency-Key` twice returns same
  response; second call does not insert duplicate rows.
- `test_api.py` — `httpx.AsyncClient` against the FastAPI app, smoke
  every endpoint, check shapes against pydantic models.

The existing CLAUDE.md guidance applies: **don't mock the database** in
the pipeline tests. Mocked DB tests passed while a real migration broke
production once already; we're not doing that again.

---

## 13. Migration order

1. Build the service. Deploy to Fly with `SUPADATABASE_URL` pointing at
   the **same** Supabase database the Streamlit app uses.
2. Verify `/healthz` and `/dashboard` work against the existing data.
3. Set `FHIR_OMOP_API_URL` as a Fly secret on the Streamlit app.
4. Streamlit page is already refactored (see §14) — it picks up the env
   var on next deploy and switches to the backend.
5. Once verified working in prod, delete the now-unused server-side
   pipeline imports from the Streamlit repo:
   - `projects/fhir_omop/pipeline/db.py`
   - `projects/fhir_omop/pipeline/transformers.py`
   - `projects/fhir_omop/pipeline/analytics.py`
   - `projects/fhir_omop/pipeline/fhir_loader.py`
   - `projects/fhir_omop/sample_data/` (now lives in the backend)
   - The `database/fhir_omop_sql/` directory (or keep as documentation)

   Keep `projects/fhir_omop/pipeline/terminology.py` — the Streamlit page
   still uses it for the Clinical Terminology Explorer (pure compute on
   data the backend returns).

---

## 14. Streamlit-side changes (already done in this repo)

The Streamlit page has been refactored to:

- Import `projects/fhir_omop/pipeline/api_client.py` (new) instead of
  `analytics`, `fhir_loader`, `transformers`, and `db`.
- Make **one** `GET /dashboard` call per rerun to render all tabs.
- Send `Idempotency-Key` headers on `/ingest/*` and `/transform` calls,
  reusing the same key across automatic retries.
- Retry once on `requests.ConnectionError` / 5xx with a 2-second pause;
  surface the result to the user via `st.status`.
- Show a clear configuration banner if `FHIR_OMOP_API_URL` is unset.

The client lives at `projects/fhir_omop/pipeline/api_client.py`. Its
public surface mirrors §4 exactly — when adding endpoints to the backend,
mirror the new methods into the client and the page will pick them up.

---

## 15. What this doesn't fix

Be honest about scope:

- **Sustained Supabase outages.** Retries help one-off blips, not 30s
  of network packet loss. The backend will surface those as 5xx; the
  page will show "backend unreachable, retrying…". That's the floor.
- **Fly.io cold starts.** With `min_machines_running = 0`, the first
  request after idle wakes the machine — adds ~1s. Set to `1` if you
  want zero cold starts (costs ~$2/mo).
- **Concurrent writes.** Single-instance, single-worker is fine for a
  demo. Two simultaneous transforms would race. Add a Postgres advisory
  lock around `run_transform` if that ever becomes real.

---

## 16. File checklist for the new repo

Day-one minimum to ship:

- [ ] `app/main.py` — routes per §4
- [ ] `app/db.py` — connect helper per §6
- [ ] `app/idempotency.py` — cache per §5
- [ ] `app/pipeline.py` — single-tx ingest + transform per §7
- [ ] `app/schemas.py` — pydantic request/response models
- [ ] `app/{transformers,analytics,fhir_loader,terminology}.py` — copies
- [ ] `app/sample_data/*.json` — copies
- [ ] `sql/001_create_tables.sql` — copy
- [ ] `Dockerfile`, `fly.toml`, `requirements.txt`, `.env.example`
- [ ] `tests/test_pipeline.py` — happy-path
- [ ] `tests/test_idempotency.py`
- [ ] `tests/test_api.py`
- [ ] `README.md` — quick start (mostly a pointer to this plan)
