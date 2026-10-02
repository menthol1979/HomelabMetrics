"""
Polling, split into three independent pieces (see config.py's
module docstring for why):

  - run_slow_poller: one per host, every config.SLOW_POLL_INTERVAL,
    fills SlowFieldsCache with NVMe Sensor 1/2, fan RPM, thresholds,
    critical_warning, media_errors, percentage_used.
  - run_fast_loop: a single loop (not per-host - one mirror call covers
    all three hosts), every config.MIRROR_POLL_INTERVAL. Fetches
    cpu_temp/nvme_composite_temp for all hosts from HomeLab-Pi5's web
    mirror in one call and merges in each host's latest cached slow
    fields. Backup windows are no longer detected here at all - see
    backup_state.py and main.py's POST /api/backup-events/{start,finish}
    - this loop only reads backup_state.active to stamp
    in_backup_window on whichever host currently has one open.
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

from . import backup_state, config, db, mirror_client
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
        # When each host's slow tier last actually refreshed - distinct
        # from the fast tier's per-tick timestamp, so the UI can show
        # both cadences instead of implying everything is 3s-fresh.
        self._updated_at: dict[str, str | None] = {host_key: None for host_key in config.HOSTS}

    def update(self, host_key: str, fields: dict) -> None:
        self._cache[host_key].update({k: fields.get(k) for k in SLOW_FIELDS})
        self._updated_at[host_key] = _now_iso()

    def updated_at(self, host_key: str) -> str | None:
        return self._updated_at[host_key]

    def get(self, host_key: str) -> dict:
        return dict(self._cache[host_key])


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
    async with httpx.AsyncClient() as http_client:
        while not stop_event.is_set():
            fast_by_host = await mirror_client.fetch_mirror_temps(http_client)

            for host_key in config.HOSTS:
                metric = {
                    "host": host_key,
                    "ts": _now_iso(),
                    **fast_by_host[host_key],
                    **cache.get(host_key),
                }
                metric["in_backup_window"] = 1 if host_key in backup_state.active else 0
                # Not a DB column - db.insert_metric() ignores extra dict
                # keys - just carried over the WebSocket so the frontend
                # can show slow-tier freshness separately from fast-tier.
                metric["slow_updated_at"] = cache.updated_at(host_key)

                db.insert_metric(metric)
                await broadcast_fn(metric)

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
