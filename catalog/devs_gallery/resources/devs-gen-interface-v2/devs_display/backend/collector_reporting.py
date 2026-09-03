"""Optional, asynchronous reporting to a durable shared-service collector.

The Interface remains fully functional when no collector is configured.  A
configured reporter coalesces frequent UI events per session and performs all
network I/O on one daemon thread, so collector latency can never delay a
student action or a model-generation worker.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from threading import Condition, Thread
from typing import Any, Callable, Optional


BundleFactory = Callable[[str, bool], dict[str, Any]]
BundleSender = Callable[[dict[str, Any]], None]


class CollectorReporter:
    def __init__(
        self,
        *,
        endpoint: str,
        token: str,
        source: str,
        bundle_factory: BundleFactory,
        debounce_seconds: float = 1.5,
        sender: Optional[BundleSender] = None,
    ) -> None:
        self.endpoint = endpoint.rstrip("/") + "/api/v1/ingest/session"
        self.token = token
        self.source = source
        self.bundle_factory = bundle_factory
        self.debounce_seconds = max(0.0, float(debounce_seconds))
        self.sender = sender or self._send_http
        self._condition = Condition()
        self._pending: dict[str, bool] = {}
        self._active = False
        self._thread = Thread(
            target=self._worker,
            name="devs-collector-reporter",
            daemon=True,
        )
        self._thread.start()

    @classmethod
    def from_environment(
        cls,
        bundle_factory: BundleFactory,
    ) -> Optional["CollectorReporter"]:
        endpoint = os.getenv("DEVS_COLLECTOR_URL", "").strip()
        token = os.getenv("DEVS_COLLECTOR_INGEST_TOKEN", "").strip()
        if not endpoint and not token:
            return None
        if not endpoint or not token:
            print(
                "[Collector] Reporting disabled: configure both "
                "DEVS_COLLECTOR_URL and DEVS_COLLECTOR_INGEST_TOKEN."
            )
            return None
        source = os.getenv("DEVS_COLLECTOR_SOURCE", "devs-gen").strip()
        if not source:
            source = "devs-gen"
        return cls(
            endpoint=endpoint,
            token=token,
            source=source,
            bundle_factory=bundle_factory,
        )

    def schedule(self, session_id: str, *, include_snapshots: bool = False) -> None:
        with self._condition:
            self._pending[session_id] = bool(
                self._pending.get(session_id) or include_snapshots
            )
            self._condition.notify()

    def wait_until_idle(self, timeout_seconds: float = 10.0) -> bool:
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        with self._condition:
            while self._pending or self._active:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(timeout=remaining)
            return True

    def _worker(self) -> None:
        while True:
            with self._condition:
                while not self._pending:
                    self._condition.wait()
                self._condition.wait(timeout=self.debounce_seconds)
                session_id, include_snapshots = self._pending.popitem()
                self._active = True
            try:
                bundle = self.bundle_factory(session_id, include_snapshots)
                bundle["source"] = self.source
                self.sender(bundle)
            except Exception as exc:
                # Local session files remain authoritative. Reporting failures
                # are deliberately non-fatal and become visible in backend logs.
                print(
                    "[Collector] Session sync failed "
                    f"({type(exc).__name__}): {exc}"
                )
            finally:
                with self._condition:
                    self._active = False
                    self._condition.notify_all()

    def _send_http(self, bundle: dict[str, Any]) -> None:
        body = json.dumps(bundle, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
        request = urllib.request.Request(
            self.endpoint,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-DEVS-Collector-Token": self.token,
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                if not 200 <= response.status < 300:
                    raise RuntimeError(f"collector returned HTTP {response.status}")
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"collector returned HTTP {exc.code}") from exc
