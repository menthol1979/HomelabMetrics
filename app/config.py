"""
Static configuration for the Homelab Metrics Dashboard.

Host list and per-host quirks were derived by querying each host's live
Glances API (v4) directly:

  - Alcyone (Intel OptiPlex, lm-sensors coretemp): CPU temp is best read
    from the "Package id 0" label; no fan_speed sensor is exposed.
  - Argos / Selene (Raspberry Pi 5): CPU temp is "cpu_thermal 0"; both
    expose a "pwmfan 0" fan_speed reading (official Active Cooler).
  - All three report the NVMe drive's temperature three ways under
    /api/4/sensors: "Composite", "Sensor 1", "Sensor 2" - these are the
    "both sensors, where the drive reports two" the spec asks for.

Glances' own per-sensor "warning"/"critical" thresholds (also returned by
/api/4/sensors) are used by the frontend for color-coding instead of a
hardcoded threshold, so each host's own lm-sensors config stays the
source of truth.
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

# NVMe temperature sensor labels shared across all hosts (see docstring).
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

# Polling intervals (seconds).
NORMAL_POLL_INTERVAL = 45
BACKUP_POLL_INTERVAL = 12

HTTP_TIMEOUT = 6

# Retention.
METRICS_RETENTION_DAYS = 30
BACKUP_EVENT_RETENTION_DAYS = 365  # summaries only; ~52 rows/year regardless
RETENTION_SWEEP_INTERVAL = 3600  # run the prune job hourly

DB_PATH = "data/homelab_metrics.db"
