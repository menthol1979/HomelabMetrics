"""
Static configuration for the Homelab Metrics Dashboard.

Polling is split across three independent tiers to avoid duplicating
the HomeLab-Pi5 project's own 3s Glances polling of Argos/Alcyone/Selene
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
  - Backup detection: piggybacks on the fast tier's tick (processlist
    check against Argos only) so a backup's start/end is caught at the
    same ~3s resolution as the temperature readings that get compared
    against it - nothing else in the fleet watches for this.
"""

HOSTS = {
    "alcyone": {
        "display_name": "Alcyone",
        "ip": "192.168.1.10",
        "port": 61208,
        "cpu_temp_label": "Package id 0",
        "is_backup_host": False,
    },
    "argos": {
        "display_name": "Argos",
        "ip": "192.168.1.17",
        "port": 61208,
        "cpu_temp_label": "cpu_thermal 0",
        "is_backup_host": True,
    },
    "selene": {
        "display_name": "Selene",
        "ip": "192.168.1.16",
        "port": 61208,
        "cpu_temp_label": "cpu_thermal 0",
        "is_backup_host": False,
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

# Process names/cmdline substrings that mark an in-progress raspiBackup
# run on the backup host (Argos), checked against Glances' processlist
# plugin - no SSH/new agent required.
BACKUP_PROCESS_MARKERS = ["raspiBackup", "pigz", "gzip"]

SLOW_POLL_INTERVAL = 60

HTTP_TIMEOUT = 6

# Retention.
METRICS_RETENTION_DAYS = 30
BACKUP_EVENT_RETENTION_DAYS = 365  # summaries only; ~52 rows/year regardless
RETENTION_SWEEP_INTERVAL = 3600  # run the prune job hourly

DB_PATH = "data/homelab_metrics.db"
