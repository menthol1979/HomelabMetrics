"""
Client for HomeLab-Pi5's own live-state web mirror
(http://192.168.1.17:8081/api/state) - one HTTP call returns
cpu_temp/ssd_temp for Argos, Alcyone, and Selene at once, since that
app is already polling all three via Glances every 3s for its physical
dashboard. Used instead of polling Glances a second time for these two
fields - see config.py's module docstring.
"""

import logging
from typing import Any

import httpx

from . import config

logger = logging.getLogger("homelab_metrics.mirror_client")


async def fetch_mirror_temps(client: httpx.AsyncClient) -> dict[str, dict[str, Any]]:
    """Returns {host_key: {"cpu_temp": float|None, "nvme_composite_temp": float|None}}
    for every host in config.HOSTS. On any failure, every host maps to
    (None, None) rather than raising, so a mirror outage degrades
    gracefully instead of stalling the fast tier."""
    empty = {"cpu_temp": None, "nvme_composite_temp": None}
    result = {host_key: dict(empty) for host_key in config.HOSTS}

    try:
        resp = await client.get(config.MIRROR_URL, timeout=config.HTTP_TIMEOUT)
        resp.raise_for_status()
        payload = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("mirror fetch failed (%s): %s", config.MIRROR_URL, exc)
        return result

    for host_key in config.HOSTS:
        host_json = payload.get(host_key)
        if not isinstance(host_json, dict):
            continue
        if host_json.get("has_cpu_temp"):
            result[host_key]["cpu_temp"] = host_json.get("cpu_temp_c")
        if host_json.get("has_ssd_temp"):
            result[host_key]["nvme_composite_temp"] = host_json.get("ssd_temp_c")

    return result
