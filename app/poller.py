"""
Per-host polling loops.

Normal cadence is config.NORMAL_POLL_INTERVAL. On the designated backup
host (Argos), each tick also checks Glances' processlist for a running
raspiBackup/gzip/pigz process; while one is detected the loop switches to
config.BACKUP_POLL_INTERVAL and tracks a backup_events row (start/end/
duration/peak temps), closed out the tick after the process disappears.
"""

import asyncio
import datetime
import logging

import httpx

from . import config, db
from .glances_client import GlancesClient

logger = logging.getLogger("homelab_metrics.poller")


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


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


async def run_host_poller(host_key: str, broadcast_fn, stop_event: asyncio.Event) -> None:
    host_cfg = config.HOSTS[host_key]
    client = GlancesClient(host_key, host_cfg)
    tracker = BackupTracker() if host_cfg["is_backup_host"] else None

    async with httpx.AsyncClient() as http_client:
        while not stop_event.is_set():
            interval = config.NORMAL_POLL_INTERVAL
            metric = await client.poll(http_client)

            if metric is not None:
                metric["ts"] = _now_iso()

                if tracker is not None:
                    backup_running = await client.is_backup_running(http_client)
                    if backup_running and not tracker.active:
                        tracker.start(host_key)

                    # Observe this tick's reading while the tracker is
                    # active, INCLUDING the tick where the backup process
                    # just stopped being detected - otherwise the sample
                    # most likely to be the true peak (the last one taken
                    # while still in/just after the backup) would be
                    # silently dropped from the peak calculation.
                    if tracker.active:
                        tracker.observe(metric)
                        interval = config.BACKUP_POLL_INTERVAL

                    if not backup_running and tracker.active:
                        tracker.finish(host_key)

                metric["in_backup_window"] = 1 if (tracker is not None and tracker.active) else 0
                db.insert_metric(metric)
                await broadcast_fn(metric)
            # else: host unreachable this tick - just skip it and retry
            # next interval; an in-progress backup on a host that drops
            # off mid-run gets closed out (as 'interrupted') the next
            # time the app restarts, via db.interrupt_stale_backup_events.

            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
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
