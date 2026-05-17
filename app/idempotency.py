"""In-process idempotency cache for write endpoints.

`/ingest` and `/transform` can corrupt data on naive retry. Clients send an
`Idempotency-Key` (UUIDv4) header; we cache the first successful response
keyed by that header and return the cached value on retry.

In-process LRU is fine because the service runs as a single Fly instance
(no horizontal scaling for this demo). If we ever scale out, swap this for
a Postgres-backed cache.
"""
from __future__ import annotations

from collections import OrderedDict
from threading import Lock
from time import monotonic
from typing import Any, Dict, Optional, Tuple

_TTL_SECONDS = 600
_MAX_ENTRIES = 256


class IdempotencyCache:
    def __init__(self, ttl_seconds: int = _TTL_SECONDS, max_entries: int = _MAX_ENTRIES) -> None:
        self._store: "OrderedDict[str, Tuple[float, Dict[str, Any]]]" = OrderedDict()
        self._lock = Lock()
        self._ttl = ttl_seconds
        self._max = max_entries

    def get(self, key: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            ts, value = entry
            if monotonic() - ts > self._ttl:
                self._store.pop(key, None)
                return None
            self._store.move_to_end(key)
            return value

    def put(self, key: str, value: Dict[str, Any]) -> None:
        with self._lock:
            self._store[key] = (monotonic(), value)
            self._store.move_to_end(key)
            while len(self._store) > self._max:
                self._store.popitem(last=False)


cache = IdempotencyCache()
