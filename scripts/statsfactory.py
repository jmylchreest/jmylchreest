"""
statsfactory Python SDK — anonymous event telemetry.

Zero external dependencies (stdlib only). Works with Python 3.8+.

Basic usage::

    from statsfactory import StatsFactory

    sf = StatsFactory(server_url="https://stats.example.com", app_key="sf_live_xxxx")

    # Count-only event
    sf.track("release_installed", {"version": "1.2.3", "os": "linux"})

    # Metric event (value for SUM/AVG aggregation)
    sf.track("release_downloads", {"repo": "myrepo", "version": "1.2.3"}, value=42)

    sf.flush()   # or sf.close()
"""

from __future__ import annotations

import json
import os
import secrets
import threading
import time
import urllib.request
import urllib.error
from typing import Any, Callable, Dict, List, Optional, Union

__version__ = "0.1.0"

# ── Types ─────────────────────────────────────────────────────────────────────

DimScalar = Union[str, int, float, bool]
Dims = Dict[str, Union[DimScalar, List[DimScalar]]]

# ── ULID generation ───────────────────────────────────────────────────────────

_ULID_CHARS = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _generate_ulid() -> str:
    """Generate a time-sortable 26-character ULID."""
    ms = int(time.time() * 1000)
    time_part = ""
    for _ in range(10):
        time_part = _ULID_CHARS[ms % 32] + time_part
        ms //= 32
    rand_bytes = secrets.token_bytes(10)
    rand_part = "".join(_ULID_CHARS[b % 32] for b in rand_bytes)
    return time_part + rand_part


def _generate_session_id() -> str:
    return secrets.token_hex(16)


# ── SDK ───────────────────────────────────────────────────────────────────────

class StatsFactory:
    """
    Statsfactory analytics client.

    Thread-safe. Events are queued locally and flushed in background batches.
    """

    MAX_BATCH_SIZE = 25
    DEFAULT_FLUSH_INTERVAL = 30.0  # seconds
    FLUSH_TIMEOUT = 10.0  # seconds

    def __init__(
        self,
        server_url: str,
        app_key: str,
        *,
        client_name: str = "",
        client_version: str = "",
        flush_interval: float = DEFAULT_FLUSH_INTERVAL,
        session_id: str = "",
        on_error: Optional[Callable[[Exception], None]] = None,
    ) -> None:
        if not server_url:
            raise ValueError("server_url is required")
        if not app_key:
            raise ValueError("app_key is required")

        self._server_url = server_url.rstrip("/")
        self._app_key = app_key
        self._client_name = client_name
        self._client_version = client_version
        self._on_error = on_error
        self._session_id = session_id or _generate_session_id()
        self._user_agent = self._build_user_agent()

        self._lock = threading.Lock()
        self._queue: List[Dict[str, Any]] = []
        self._closed = False

        self._flush_interval = flush_interval
        self._timer: Optional[threading.Timer] = None
        if flush_interval > 0:
            self._schedule_flush()

    # ── Public API ────────────────────────────────────────────────────────────

    def track(
        self,
        event_name: str,
        dims: Optional[Dims] = None,
        *,
        value: Optional[float] = None,
        timestamp: Optional[str] = None,
        session_id: Optional[str] = None,
        distinct_id: Optional[str] = None,
    ) -> None:
        """
        Enqueue an event. Does not block on I/O.

        :param event_name: Lowercase alphanumeric + underscores, max 64 chars.
        :param dims: Key-value dimensions.
        :param value: Optional numeric value for SUM/AVG/MIN/MAX aggregation.
                      Absence means count-only (default behaviour unchanged).
        :param timestamp: ISO 8601 timestamp string. Defaults to server time.
        :param session_id: Override the client-level session ID.
        :param distinct_id: User/install identity.
        """
        with self._lock:
            if self._closed:
                return

            ev: Dict[str, Any] = {
                "event": event_name,
                "event_key": _generate_ulid(),
                "session_id": session_id or self._session_id,
            }
            if timestamp:
                ev["timestamp"] = timestamp
            if distinct_id:
                ev["distinct_id"] = distinct_id
            if value is not None:
                ev["value"] = value
            if dims:
                ev["dimensions"] = dict(dims)

            self._queue.append(ev)

    def flush(self) -> None:
        """Flush all queued events synchronously. Raises on network/server errors."""
        batch = self._drain()
        if batch:
            self._send_batches(batch)

    def close(self) -> None:
        """Flush remaining events and stop the SDK. Subsequent track() calls are dropped."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
            batch = self._drain_locked()

        if batch:
            self._send_batches(batch)

    @property
    def session_id(self) -> str:
        return self._session_id

    def queue_length(self) -> int:
        with self._lock:
            return len(self._queue)

    # ── Internal ──────────────────────────────────────────────────────────────

    def _build_user_agent(self) -> str:
        parts = [f"statsfactory-sdk-py/{__version__}"]
        if self._client_name:
            client = self._client_name
            if self._client_version:
                client += "/" + self._client_version
            parts.append(f"({client})")
        return " ".join(parts)

    def _drain(self) -> List[Dict[str, Any]]:
        with self._lock:
            return self._drain_locked()

    def _drain_locked(self) -> List[Dict[str, Any]]:
        batch = self._queue
        self._queue = []
        return batch

    def _schedule_flush(self) -> None:
        self._timer = threading.Timer(self._flush_interval, self._background_flush)
        self._timer.daemon = True
        self._timer.start()

    def _background_flush(self) -> None:
        batch = self._drain()
        if batch:
            try:
                self._send_batches(batch)
            except Exception as exc:
                if self._on_error:
                    self._on_error(exc)
        with self._lock:
            if not self._closed:
                self._schedule_flush()

    def _send_batches(self, events: List[Dict[str, Any]]) -> None:
        while events:
            chunk = events[: self.MAX_BATCH_SIZE]
            events = events[self.MAX_BATCH_SIZE :]
            self._send_chunk(chunk)

    def _send_chunk(self, events: List[Dict[str, Any]]) -> None:
        url = self._server_url + "/v1/events"
        payload = json.dumps({"events": events}).encode()

        req = urllib.request.Request(
            url,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._app_key}",
                "User-Agent": self._user_agent,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.FLUSH_TIMEOUT) as resp:
                if resp.status >= 400:
                    body = resp.read().decode(errors="replace")
                    raise RuntimeError(f"StatsFactory: HTTP {resp.status}: {body}")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode(errors="replace") if exc.fp else ""
            raise RuntimeError(f"StatsFactory: HTTP {exc.code}: {body}") from exc

    # ── Context manager support ───────────────────────────────────────────────

    def __enter__(self) -> "StatsFactory":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()
