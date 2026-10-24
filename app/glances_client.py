"""
Slow-tier async client for a single host's Glances (v4) REST API: NVMe
Sensor 1/2, fan RPM, per-host warn/crit thresholds (all from /sensors),
plus critical_warning/media_errors/percentage_used (from /smart).

cpu_temp and the NVMe composite temperature are deliberately NOT
fetched here any more - those come from HomeLab-Pi5's web mirror at the
fast tier instead (see mirror_client.py and config.py's docstring), so
this client only needs to run at config.SLOW_POLL_INTERVAL.

This client no longer does any backup detection - see config.py's
module docstring for why that moved to a push-based webhook instead of
a Glances processlist check.
"""

import logging
import re
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

    async def poll_slow(self, client: httpx.AsyncClient) -> dict[str, Any] | None:
        """Fetch /sensors + /smart for this host and normalize the
        slow-tier fields into one dict. Returns None (and logs) if the
        host is unreachable, rather than raising, so one down host
        never stops the others from polling."""
        try:
            sensors, smart = await self._get(client, "sensors"), await self._get(client, "smart")
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("slow poll failed for host=%s: %s", self.host_key, exc)
            return None

        return {
            "host": self.host_key,
            **self._parse_sensors(sensors),
            **self._parse_smart(smart),
        }

    def _parse_sensors(self, sensors: list[dict[str, Any]]) -> dict[str, Any]:
        by_label = {entry.get("label"): entry for entry in sensors if isinstance(entry, dict)}

        out: dict[str, Any] = {}

        cpu_entry = by_label.get(self.cfg["cpu_temp_label"])
        out["cpu_temp_warn"], out["cpu_temp_crit"] = self._sane_thresholds(cpu_entry)
        # Not a DB column, not broadcast - poller.py's run_fast_loop
        # consumes this as a fallback for hosts (e.g. Penelope) that
        # HomeLab-Pi5's mirror doesn't cover, or if the mirror is down.
        out["cpu_temp_slow"] = self._coerce_temp(cpu_entry)

        # Sensor 1/2 only - "Composite" is supplied by the mirror at the
        # fast tier instead (see module docstring).
        for field in ("nvme_sensor1_temp", "nvme_sensor2_temp"):
            entry = by_label.get(config.NVME_SENSOR_LABELS[field])
            out[field] = self._coerce_temp(entry)

        composite_entry = by_label.get(config.NVME_SENSOR_LABELS["nvme_composite_temp"])
        out["nvme_composite_warn"], out["nvme_composite_crit"] = self._sane_thresholds(composite_entry)
        out["nvme_composite_temp_slow"] = self._coerce_temp(composite_entry)

        fan_entry = by_label.get(config.FAN_LABEL)
        out["fan_rpm"] = fan_entry.get("value") if fan_entry else None

        return out

    @staticmethod
    def _coerce_temp(entry: dict | None) -> float | None:
        """Defensive guard against a non-temperature sensor (most
        plausibly a fan) slipping into a temperature field through a
        label mix-up - this exact failure mode once showed a fan
        reading as 1357 degrees on the HomeLab-Pi5 dashboard. Only
        trust a value that looks like a Celsius reading per Glances'
        own unit field (when it bothers to send one) and that falls in
        a range physically possible for a CPU or NVMe drive - anything
        else is treated as missing rather than trusted."""
        if not entry:
            return None
        value = entry.get("value")
        if not isinstance(value, (int, float)):
            return None
        unit = entry.get("unit")
        if unit not in (None, "C", "°C"):
            return None
        if not (0 <= value <= 150):
            return None
        return float(value)

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
        """Most hosts in this fleet have exactly one drive (NVMe), whose
        /api/4/smart entry uses NVMe SMART-log keys - criticalWarning,
        percentageUsed, integrityErrors, etc, matching config.SMART_KEYS.
        Penelope has a second, differently-shaped drive too (the Crucial
        BX500 SATA SSD backing DRBD's replicated volume), which reports
        classic ATA SMART attributes (Reallocated_Event_Count,
        Temperature_Celsius, ...) under a completely different key
        vocabulary - so rather than assume smart[0] is always "the"
        drive, find whichever entry actually matches config.SMART_KEYS
        and treat that as primary; anything else found is surfaced
        separately as an "extra" drive instead of being forced into the
        NVMe-shaped fields it doesn't have."""
        out: dict[str, Any] = {field: None for field in config.SMART_KEYS}
        out["extra_drive_label"] = None
        out["extra_drive_temp"] = None
        out["extra_drive_reallocated_sectors"] = None
        if not smart:
            return out

        primary_attrs: dict[str, Any] | None = None
        extra_drive: dict[str, Any] | None = None

        for drive in smart:
            # Numbered SMART attributes live as dict values keyed
            # "1".."23"; look each one up by its "key" field rather than
            # assuming a fixed numeric index (not guaranteed stable
            # across versions or drive types).
            by_attr_key = {
                attr.get("key"): attr
                for attr in drive.values()
                if isinstance(attr, dict) and "key" in attr
            }
            if primary_attrs is None and any(k in by_attr_key for k in config.SMART_KEYS.values()):
                primary_attrs = {k: v.get("value") for k, v in by_attr_key.items()}
                continue
            if extra_drive is None:
                extra_drive = {"device": drive.get("DeviceName"), "attrs": by_attr_key}

        if primary_attrs:
            for field, attr_key in config.SMART_KEYS.items():
                out[field] = primary_attrs.get(attr_key)

        if extra_drive:
            out["extra_drive_label"] = extra_drive["device"]
            out["extra_drive_temp"] = self._coerce_ata_temp(extra_drive["attrs"].get("Temperature_Celsius"))
            out["extra_drive_reallocated_sectors"] = self._coerce_ata_count(
                extra_drive["attrs"].get("Reallocated_Event_Count")
            )
            # Deliberately NOT surfacing attribute 202 "Percent_Lifetime_Remain"
            # as a wear percentage - its raw-value polarity (percent used
            # vs. percent remaining) is genuinely ambiguous and model/
            # firmware-dependent even in vendor documentation for this
            # exact drive family, and showing it backwards would be worse
            # than not showing it. Reallocated-sector count and
            # temperature are unambiguous across all ATA drives instead.

        return out

    @staticmethod
    def _coerce_ata_temp(attr: dict | None) -> float | None:
        """ATA SMART temperature attributes (e.g. Temperature_Celsius)
        report their raw value as a string like "45 (Min/Max 21/57)" -
        take the leading number as the current reading, same 0-150C
        plausibility guard as _coerce_temp."""
        if not attr:
            return None
        raw = attr.get("raw")
        if isinstance(raw, (int, float)):
            value = raw
        elif isinstance(raw, str):
            match = re.match(r"-?\d+", raw.strip())
            if not match:
                return None
            value = int(match.group())
        else:
            return None
        if not (0 <= value <= 150):
            return None
        return float(value)

    @staticmethod
    def _coerce_ata_count(attr: dict | None) -> int | None:
        """ATA SMART attributes like Reallocated_Event_Count report their
        raw value as a numeric string - 0 is healthy, any nonzero is
        worth seeing regardless of the drive vendor."""
        if not attr:
            return None
        raw = attr.get("raw")
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None
