"""Connection helper for the FHIR-OMOP backend.

Sole entry point to psycopg2. Every other module receives a connection or a
cursor from `get_connection()` — no module elsewhere should import psycopg2
to open its own connection.
"""
from __future__ import annotations

import logging
import os
import time
from contextlib import contextmanager
from typing import Iterator

import psycopg2

logger = logging.getLogger(__name__)


def _required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Required environment variable {name} is not set")
    return value


CONNECT_TIMEOUT = int(os.getenv("DB_CONNECT_TIMEOUT", "2"))
MAX_ATTEMPTS = int(os.getenv("DB_MAX_ATTEMPTS", "3"))
BACKOFF_MS = int(os.getenv("DB_BACKOFF_MS", "200"))


@contextmanager
def get_connection() -> Iterator["psycopg2.extensions.connection"]:
    """Open a Supabase connection with retry on handshake failures only.

    Query-time errors after the handshake propagate immediately so we never
    blindly retry partial writes.
    """
    dsn = _required_env("SUPADATABASE_URL")
    conn = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            conn = psycopg2.connect(dsn, connect_timeout=CONNECT_TIMEOUT)
            break
        except psycopg2.OperationalError as e:
            if attempt == MAX_ATTEMPTS:
                raise
            sleep_s = (BACKOFF_MS / 1000.0) * (2 ** (attempt - 1))
            logger.warning(
                "DB connect failed (attempt %d/%d): %s — retrying in %.2fs",
                attempt, MAX_ATTEMPTS, e, sleep_s,
            )
            time.sleep(sleep_s)
    assert conn is not None
    try:
        yield conn
    finally:
        conn.close()
