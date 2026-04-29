from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from typing import Any


LOGGER = logging.getLogger("isambard.vpn")


class MullvadGuard:
    def __init__(self, ttl_seconds: float = 5.0) -> None:
        self._ttl_seconds = max(0.0, ttl_seconds)
        self._lock = threading.RLock()
        self._cached_status: dict[str, Any] | None = None
        self._cached_at = 0.0

    def status(self, force: bool = False) -> dict[str, Any]:
        now = time.monotonic()
        with self._lock:
            if not force and self._cached_status is not None and (now - self._cached_at) < self._ttl_seconds:
                return dict(self._cached_status)
        status = self._probe_status()
        with self._lock:
            self._cached_status = dict(status)
            self._cached_at = now
        return status

    def is_connected(self, force: bool = False) -> bool:
        return bool(self.status(force=force).get("connected"))

    def assert_connected(self, context: str) -> None:
        status = self.status(force=True)
        if status.get("connected"):
            return
        summary = status.get("summary") or "Mullvad connection is required"
        raise RuntimeError(f"{context} blocked until Mullvad is connected. {summary}")

    def wait_until_connected(self, context: str, poll_seconds: float = 5.0) -> None:
        interval = max(1.0, poll_seconds)
        while True:
            status = self.status(force=True)
            if status.get("connected"):
                return
            LOGGER.warning("%s waiting for Mullvad connection: %s", context, status.get("summary") or "unavailable")
            time.sleep(interval)

    def _probe_status(self) -> dict[str, Any]:
        endpoint = os.environ.get("MULLVAD_STATUS_URL", "https://am.i.mullvad.net/json")
        timeout = max(1.0, float(os.environ.get("MULLVAD_STATUS_TIMEOUT_SECONDS", "5")))
        try:
            with urllib.request.urlopen(endpoint, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            LOGGER.warning("failed to read mullvad endpoint: %s", exc)
            return self._fallback_cli_status()

        connected = bool(payload.get("mullvad_exit_ip"))
        ip = payload.get("ip") or "Unknown IP"
        location = ", ".join(
            part for part in [payload.get("city"), payload.get("country")] if isinstance(part, str) and part
        )
        organization = payload.get("organization") or ""
        summary_parts = [f"IP: {ip}"]
        if location:
            summary_parts.append(f"Location: {location}")
        if organization:
            summary_parts.append(f"Org: {organization}")
        return {
            "available": True,
            "connected": connected,
            "summary": "\n".join(summary_parts),
        }

    def _fallback_cli_status(self) -> dict[str, Any]:
        if shutil.which("mullvad") is None:
            return {
                "available": False,
                "connected": False,
                "summary": "Unable to verify Mullvad connection",
            }
        try:
            result = subprocess.run(
                ["mullvad", "status"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except Exception as exc:
            LOGGER.warning("failed to read mullvad status: %s", exc)
            return {
                "available": False,
                "connected": False,
                "summary": "Unable to read Mullvad status",
            }
        output = (result.stdout or result.stderr or "").strip()
        lowered = output.lower()
        connected = "connected" in lowered and "disconnected" not in lowered
        return {
            "available": True,
            "connected": connected,
            "summary": output or "Mullvad status unavailable",
        }
