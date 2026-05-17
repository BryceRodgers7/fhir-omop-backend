# FHIR-OMOP Backend

Small FastAPI service that owns all database I/O for the FHIR → OMOP demo.
The Streamlit portfolio page is a dumb HTTP client; this service holds the
Supabase connection, runs the transforms, and exposes a stable JSON surface.

See [`ARCHITECTURE.md`](ARCHITECTURE.md) for the full design.

## Quick start

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows
# source .venv/bin/activate       # macOS/Linux

pip install -r requirements.txt
$env:SUPADATABASE_URL = "postgres://..."   # PowerShell
# export SUPADATABASE_URL=...               # bash

uvicorn app.main:app --reload --port 8080
```

OpenAPI docs: http://localhost:8080/docs

### Smoke tests

```bash
curl -X POST http://localhost:8080/reset
curl -X POST http://localhost:8080/ingest/sample -H "Idempotency-Key: $(uuidgen)"
curl -X POST http://localhost:8080/transform     -H "Idempotency-Key: $(uuidgen)"
curl http://localhost:8080/dashboard | jq .summary
```

### Schema bootstrap (one-time)

```bash
psql "$SUPADATABASE_URL" -f sql/001_create_tables.sql
```

## Layout

```
app/        # FastAPI app, pipeline, transformers, analytics, sample data
sql/        # 001_create_tables.sql
tests/      # test_pipeline, test_idempotency, test_api
Dockerfile  fly.toml  requirements.txt  .env.example
```
