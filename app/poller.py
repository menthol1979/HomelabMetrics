"""
Polling, split into three independent pieces (see config.py's
module docstring for why):

  - run_slow_poller: one per host, every config.SLOW_POLL_INTERVAL,
    fills SlowFieldsCache with NVMe Sensor 1/2, fan RPM, thresholds,
    critical_warning, media_errors, percentage_used.
  - run_fast_loop: a single loop (not per-host - one mirror call covers
    all three hosts), every config.MIRROR_POLL_INTERVAL. Fetches
    cpu_temp/nvme_composite_temp for all hosts from HomeLab-Pi5's web
    mirror in one call, merges in each host's latest cached slow
    fields, and - for Argos only - checks Glances' processlist for a
    running backup and tracks a backup_events row across the window.
  - run_retention_sweeper: unchanged, hourly DB prune.

A metric row is only ever written by the fast loop, once per host per
tick, combining fresh fast fields with whatever the slow cache last
had - so the DB/frontend still see one complete row per poll, same as
before this split.
"""

import asyncio
import datetime
import logging

import httpx

from . import config, db, mirror_client
from .glances_client import GlancesClient

logger = logging.getLogger("homelab_metrics.poller")


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


SLOW_FIELDS = [
    "nvme_sensor1_temp", "nvme_sensor2_temp", "fan_rpm",
    "cpu_temp_warn", "cpu_temp_crit", "nvme_composite_warn", "nvme_composite_crit",
    "nvme_critical_warning", "nvme_media_errors", "nvme_percentage_used",
]


class SlowFieldsCache:
    """Last-known-good slow-tier fields per host, carried forward
    between the infrequent slow-tier polls so every fast-tier row is
    still a complete metric, not a sparse one."""

    def __init__(self):
        self._cache: dict[str, dict] = {
            host_key: {field: None for field in SLOW_FIELDS} for host_key in config.HOSTS
        }

    def update(self, host_key: str, fields: dict) -> None:
        self._cache[host_key].update({k: fields.get(k) for k in SLOW_FIELDS})

    def get(self, host_key: str) -> dict:
        return dict(self._cache[host_key])


class BackupTracker:
    """Tracks the currently in-progress backup event for one host, if any."""

    def __init__(self):
        self.event_id: int | None = None
        self.start_ts: str | None = None
        self.peak_nvme_temp: float | None = None
        self.peak_cpu_temp: float | None = None

    @property
    def active(self) -> bool:
        return self.event_id is not None

    def start(self, host: str) -> None:
        self.start_ts = _now_iso()
        self.event_id = db.start_backup_event(host, self.start_ts)
        self.peak_nvme_temp = None
        self.peak_cpu_temp = None
        logger.info("backup window started on %s (event_id=%s)", host, self.event_id)

    def observe(self, metric: dict) -> None:
        nvme_temp = metric.get("nvme_composite_temp")
        cpu_temp = metric.get("cpu_temp")
        if nvme_temp is not None:
            self.peak_nvme_temp = nvme_temp if self.peak_nvme_temp is None else max(self.peak_nvme_temp, nvme_temp)
        if cpu_temp is not None:
            self.peak_cpu_temp = cpu_temp if self.peak_cpu_temp is None else max(self.peak_cpu_temp, cpu_temp)

    def finish(self, host: str, status: str = "completed") -> None:
        end_ts = _now_iso()
        duration = (
            datetime.datetime.fromisoformat(end_ts) - datetime.datetime.fromisoformat(self.start_ts)
        ).total_seconds()
        db.finish_backup_event(self.event_id, end_ts, duration, self.peak_nvme_temp, self.peak_cpu_temp, status)
        logger.info(
            "backup window ended on %s (event_id=%s, duration=%.0fs, peak_nvme=%s, peak_cpu=%s)",
            host, self.event_id, duration, self.peak_nvme_temp, self.peak_cpu_temp,
        )
        self.event_id = self.start_ts = self.peak_nvme_temp = self.peak_cpu_temp = None


async def run_slow_poller(host_key: str, cache: SlowFieldsCache, stop_event: asyncio.Event) -> None:
    host_cfg = config.HOSTS[host_key]
    client = GlancesClient(host_key, host_cfg)

    async with httpx.AsyncClient() as http_client:
        while not stop_event.is_set():
            fields = await client.poll_slow(http_client)
            if fields is not None:
                cache.update(host_key, fields)
            # else: host unreachable this tick - keep serving the last
            # known-good slow fields rather than blanking them out.

            try:
                await asyncio.wait_for(stop_event.wait(), timeout=config.SLOW_POLL_INTERVAL)
            except asyncio.TimeoutError:
                pass


async def run_fast_loop(cache: SlowFieldsCache, broadcast_fn, stop_event: asyncio.Event) -> None:
    backup_host_key = next(k for k, c in config.HOSTS.items() if c["is_backup_host"])
    backup_client = GlancesClient(backup_host_key, config.HOSTS[backup_host_key])
    tracker = BackupTracker()

    async with httpx.AsyncClient() as http_client:
        while not stop_event.is_set():
            fast_by_host = await mirror_client.fetch_mirror_temps(http_client)
            backup_running = await backup_client.is_backup_running(http_client)

            if backup_running and not tracker.active:
                tracker.start(backup_host_key)

            for host_key in config.HOSTS:
                metric = {
                    "host": host_key,
                    "ts": _now_iso(),
                    **fast_by_host[host_key],
                    **cache.get(host_key),
                }
                metric["in_backup_window"] = 1 if (host_key == backup_host_key and tracker.active) else 0

                if host_key == backup_host_key and tracker.active:
                    tracker.observe(metric)

                db.insert_metric(metric)
                await broadcast_fn(metric)

            if not backup_running and tracker.active:
                tracker.finish(backup_host_key)

            try:
                await asyncio.wait_for(stop_event.wait(), timeout=config.MIRROR_POLL_INTERVAL)
            except asyncio.TimeoutError:
                pass


async def run_retention_sweeper(stop_event: asyncio.Event) -> None:
    while not stop_event.is_set():
        try:
            result = db.prune_old_data()
            logger.info("retention sweep: %s", result)
        except Exception:
            logger.exception("retention sweep failed")

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=config.RETENTION_SWEEP_INTERVAL)
        except asyncio.TimeoutError:
            pass
