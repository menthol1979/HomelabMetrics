"""
Static configuration for the Homelab Metrics Dashboard.

Polling is split across two independent tiers to avoid duplicating the
HomeLab-Pi5 project's own 3s Glances polling of Argos/Alcyone/Selene
(see that repo's config.example.h - CFG_*_GLANCES_POLL_INTERVAL_SEC):

  - FAST tier (MIRROR_POLL_INTERVAL): one HTTP call to HomeLab-Pi5's own
    "web mirror" at MIRROR_URL, which already dumps its live-polled
    cpu_temp/ssd_temp for all three hosts in one response. No duplicate
    Glances traffic at all for these two fields.
  - SLOW tier (SLOW_POLL_INTERVAL): a direct, low-frequency call per
    host to Glances' own /sensors and /smart, for the handful of fields
    the web mirror doesn't carry (NVMe Sensor 1/2, fan RPM, per-host
    warn/crit thresholds, critical_warning, media_errors,
    percentage_used). These change slowly, so 60s is plenty and this
    tier's load is negligible next to HomeLab-Pi5's continuous 3s poll.

Backup detection is push-based, not polled: raspibackup.service itself
calls POST /api/backup-events/{start,finish} (see main.py) via
ExecStartPre/ExecStopPost, so a backup window is ground truth from
systemd rather than inferred. An earlier version of this guessed by
grepping Glances' processlist for "raspiBackup"/"pigz"/"gzip" in any
process's cmdline - that both false-positived on unrelated commands
that merely mention the unit name (e.g. `systemctl status
raspibackup.service`) and could silently miss the real thing, since a
timed-out Glances call under real backup I/O load just returned False.
Peak temps for a finished event are computed after the fact from the
metrics rows already recorded for that host in [start_ts, end_ts] -
see db.get_peak_temps_in_window() - rather than tracked live tick by
tick, so there's nothing to miss even if a tick is slow or dropped.
"""

import os

HOSTS = {
    "alcyone": {
        "display_name": "Alcyone",
        "ip": "192.168.1.10",
        "port": 61208,
        "cpu_temp_label": "Package id 0",
        "is_backup_host": False,
        # Dell OptiPlex 3060 Micro - no PWM fan sensor exposed via
        # Glances/lm-sensors on Linux (see FAN_LABEL below).
        "has_fan": False,
    },
    "argos": {
        "display_name": "Argos",
        "ip": "192.168.1.17",
        "port": 61208,
        "cpu_temp_label": "cpu_thermal 0",
        "is_backup_host": True,
        "has_fan": True,
    },
    "selene": {
        "display_name": "Selene",
        "ip": "192.168.1.16",
        "port": 61208,
        "cpu_temp_label": "cpu_thermal 0",
        "is_backup_host": False,
        "has_fan": True,
    },
}

# HomeLab-Pi5's own live-state web mirror (src/web_state_json.cpp),
# already polling Argos/Alcyone/Selene's Glances every 3s for its
# physical dashboard. Its per-host JSON carries: cpu_temp_c,
# has_cpu_temp, ssd_temp_c (NVMe "Composite" reading only),
# has_ssd_temp, ssd_health_pct, has_ssd_health - see that repo's
# src/web_state_json.cpp::glances_json() for the authoritative shape.
MIRROR_URL = "http://192.168.1.17:8081/api/state"
MIRROR_POLL_INTERVAL = 3

# NVMe temperature sensor labels shared across all hosts (only used by
# the slow tier now - the fast tier's composite reading comes from the
# mirror instead).
NVME_SENSOR_LABELS = {
    "nvme_composite_temp": "Composite",
    "nvme_sensor1_temp": "Sensor 1",
    "nvme_sensor2_temp": "Sensor 2",
}

FAN_LABEL = "pwmfan 0"

# SMART attribute keys (Glances' `smart` plugin, pySMART-derived), looked
# up by the per-attribute "key" field rather than by numeric index, since
# the numeric keys aren't guaranteed stable across smartctl/pySMART
# versions.
SMART_KEYS = {
    "nvme_critical_warning": "criticalWarning",
    "nvme_percentage_used": "percentageUsed",
    # Glances/pySMART surfaces nvme-cli's "media_errors" (from
    # `nvme smart-log`) under the friendly name "Integrity errors".
    "nvme_media_errors": "integrityErrors",
}

# Shared secret the POST /api/backup-events/{start,finish} webhooks
# require as `Authorization: Bearer <token>` - set via the container's
# environment (docker-compose.yml), matching value goes in
# raspibackup.service's ExecStartPre/ExecStopPost curl calls. Unset
# means those endpoints are refused outright rather than left open.
BACKUP_EVENT_TOKEN = os.environ.get("BACKUP_EVENT_TOKEN")

SLOW_POLL_INTERVAL = 60

HTTP_TIMEOUT = 6

# Retention.
METRICS_RETENTION_DAYS = 30
BACKUP_EVENT_RETENTION_DAYS = 365  # summaries only; ~52 rows/year regardless
RETENTION_SWEEP_INTERVAL = 3600  # run the prune job hourly

DB_PATH = "data/homelab_metrics.db"
