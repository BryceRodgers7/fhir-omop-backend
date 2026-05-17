"""Test fixtures.

All tests in this directory hit a real Postgres (Supabase) database — no
mocks. The `clean_db` fixture truncates every fhir_demo_* table before each
test so cases stay independent.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# Allow `from app import ...` when running pytest from the repo root.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

if not os.environ.get("SUPADATABASE_URL"):
    pytest.skip(
        "SUPADATABASE_URL is not set; pipeline/API tests require a real database",
        allow_module_level=True,
    )

from app.db import get_connection
from app.pipeline import reset_demo_data


@pytest.fixture
def clean_db():
    """TRUNCATE every fhir_demo_* table before and after each test."""
    with get_connection() as conn:
        reset_demo_data(conn)
    yield
    with get_connection() as conn:
        reset_demo_data(conn)
