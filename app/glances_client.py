"""
Thin async client for a single host's Glances (v4) REST API, plus
normalization of the /sensors and /smart responses into the flat metric
shape the rest of the app works with.
"""

import logging
from typing import Any

import httpx

from . import config

logger = logging.getLogger("homelab_metrics.glances_client")


class GlancesClient:
    def __init__(self, host_key: str, host_cfg: dict[str, Any]):
        self.host_key = host_key
        self.cfg = host_cfg
        self.base_url = f"http://{host_cfg['ip']}:{host_cfg['port']}/api/4"

    async def _get(self, client: httpx.AsyncClient, path: str) -> Any:
        resp = await client.get(f"{self.base_url}/{path}", timeout=config.HTTP_TIMEOUT)
        resp.raise_for_status()
        return resp.json()

    async def poll(self, client: httpx.AsyncClient) -> dict[str, Any] | None:
        """Fetch sensors + smart for this host and normalize into one dict.

        Returns None (and logs) if the host is unreachable, rather than
        raising, so one down host never stops the others from polling.
        """
        try:
            sensors, smart = await self._get(client, "sensors"), await self._get(client, "smart")
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("poll failed for host=%s: %s", self.host_key, exc)
            return None

        return {
            "host": self.host_key,
            **self._parse_sensors(sensors),
            **self._parse_smart(smart),
        }

    async def is_backup_running(self, client: httpx.AsyncClient) -> bool:
        """Only meaningful for hosts with is_backup_host=True."""
        try:
            procs = await self._get(client, "processlist")
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("processlist poll failed for host=%s: %s", self.host_key, exc)
            return False

        for proc in procs:
            name = (proc.get("name") or "").lower()
            cmdline = " ".join(proc.get("cmdline") or []).lower()
            haystack = name + " " + cmdline
            if any(marker.lower() in haystack for marker in config.BACKUP_PROCESS_MARKERS):
                return True
        return False

    def _parse_sensors(self, sensors: list[dict[str, Any]]) -> dict[str, Any]:
        by_label = {entry.get("label"): entry for entry in sensors if isinstance(entry, dict)}

        out: dict[str, Any] = {}

        cpu_entry = by_label.get(self.cfg["cpu_temp_label"])
        out["cpu_temp"] = cpu_entry.get("value") if cpu_entry else None
        out["cpu_temp_warn"], out["cpu_temp_crit"] = self._sane_thresholds(cpu_entry)

        for field, label in config.NVME_SENSOR_LABELS.items():
            entry = by_label.get(label)
            out[field] = entry.get("value") if entry else None

        # Only "Composite" carries meaningful lm-sensors thresholds on
        # this fleet - "Sensor 1"/"Sensor 2" report a 65261 sentinel
        # (no threshold configured), so thresholds are only captured
        # here rather than per individual NVMe sensor.
        composite_entry = by_label.get(config.NVME_SENSOR_LABELS["nvme_composite_temp"])
        out["nvme_composite_warn"], out["nvme_composite_crit"] = self._sane_thresholds(composite_entry)

        fan_entry = by_label.get(config.FAN_LABEL)
        out["fan_rpm"] = fan_entry.get("value") if fan_entry else None

        return out

    @staticmethod
    def _sane_thresholds(entry: dict | None) -> tuple[float | None, float | None]:
        """Glances passes lm-sensors thresholds through verbatim,
        including a 65261 sentinel some drivers use for "unset". Treat
        anything absurdly high as not-configured so the frontend falls
        back to its own default band instead of drawing a useless line
        near 65000 degrees."""
        if not entry:
            return None, None
        warn, crit = entry.get("warning"), entry.get("critical")
        warn = warn if isinstance(warn, (int, float)) and warn < 200 else None
        crit = crit if isinstance(crit, (int, float)) and crit < 200 else None
        return warn, crit

    def _parse_smart(self, smart: list[dict[str, Any]]) -> dict[str, Any]:
        out: dict[str, Any] = {field: None for field in config.SMART_KEYS}
        if not smart:
            return out

        # One NVMe drive per host in this fleet - take the first entry.
        drive = smart[0]
        # Numbered SMART attributes live as dict values keyed "1".."23";
        # look each one up by its "key" field rather than assuming a
        # fixed numeric index (not guaranteed stable across versions).
        by_attr_key = {
            attr.get("key"): attr.get("value")
            for attr in drive.values()
            if isinstance(attr, dict) and "key" in attr
        }

        for field, attr_key in config.SMART_KEYS.items():
            out[field] = by_attr_key.get(attr_key)

        return out
