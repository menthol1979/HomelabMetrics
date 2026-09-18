# Homelab Metrics Dashboard

Always-on health logging for Alcyone, Argos, and Selene: continuous CPU/NVMe
temperature, wear, and fan-speed tracking, with automatic detection of
Argos's Friday 04:00 `raspiBackup` window so every backup leaves a
retrievable trace of how hot it actually got.

Built on the same stack as [OfficeAQI](https://github.com/menthol1979/OfficeAQI):
FastAPI + Uvicorn backend, SQLite storage, a WebSocket live feed, and a
single-page vanilla JS + Apache ECharts frontend (bundled locally, no
CDN dependency), packaged as one Docker container.

## How it works

No new agents run on any host. All three machines already expose Glances
(v4) on port 61208 with the `sensors` and `smart` plugins enabled. The
backend polls each host's `/api/4/sensors`, `/api/4/smart`, and — on
Argos only — `/api/4/processlist` (to detect a running
`raspiBackup`/`pigz`/`gzip` process), and writes normalized rows to
SQLite.

- **Normal polling:** every 45s per host.
- **During a detected backup on Argos:** drops to every 12s so the
  temperature curve is actually resolved, and a `backup_events` row
  tracks start/end/duration/peak NVMe & CPU temp for that run.
- **Live push:** every poll is also broadcast over `/ws` to any open
  browser tab, so the dashboard updates without polling itself.

### Data sources, mapped

| Metric | Source |
| --- | --- |
| CPU temperature | `/api/4/sensors`, host-specific label (`Package id 0` on Alcyone, `cpu_thermal 0` on Argos/Selene — see `app/config.py`) |
| NVMe composite + 2 sensor temps | `/api/4/sensors`, labels `Composite` / `Sensor 1` / `Sensor 2` (same on all three hosts) |
| NVMe critical-warning flag, percentage-used (wear), media errors | `/api/4/smart`, looked up by the attribute's `key` field (`criticalWarning`, `percentageUsed`, `integrityErrors` — Glances' name for nvme-cli's `media_errors`) |
| Fan RPM | `/api/4/sensors`, label `pwmfan 0` — present on Argos/Selene's Active Coolers, absent on Alcyone |
| Backup detection | `/api/4/processlist` on Argos, matched against `raspiBackup`/`pigz`/`gzip` |

Per-sensor `warning`/`critical` thresholds are pulled from Glances too
(each host's own lm-sensors config) and used for color-coding on the
frontend, rather than one hardcoded threshold for every host.

### Known limitation: no `throttled` (undervoltage/thermal) flag

The original spec asked for the Pi's undervoltage/throttling flag
(`vcgencmd get_throttled`). Glances' API does not expose this on any of
the three hosts — it isn't part of the `sensors`, `smart`, or any other
plugin surfaced at `/api/4/pluginslist`. This field is *not* currently
collected. If it matters later, the simplest fix without adding a new
agent would be a small custom Glances plugin, or a tiny sidecar script
on Argos/Selene that shells out to `vcgencmd` and exposes it on a local
port this app could poll too.

## Retention

- Routine metrics: 30 days, rolling (`METRICS_RETENTION_DAYS` in `app/config.py`).
- Backup-event summaries (start/end/duration/peak temps): 365 days,
  rolling (`BACKUP_EVENT_RETENTION_DAYS`) — kept longer than routine
  metrics since it's only ~52 rows/year and lets you spot backups
  getting hotter over months, which a 30-day window would hide.
- A retention sweep runs hourly in the background.

## Running locally (dev)

```
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Then open http://localhost:8000/. It will start polling the real
Alcyone/Argos/Selene Glances APIs immediately (LAN access required).

**Don't point `data/` at a NAS/SMB-mounted path when running locally.**
SQLite over this homelab's NAS mount throws `disk I/O error` on
`executescript` (same class of issue as the git-object-corruption gotcha
hit on the OfficeAQI NAS folder) — run it from local disk. This isn't a
concern for the Docker deployment below, since that uses a local Docker
named volume, not a NAS bind mount.

## Deployment (Portainer, on Argos)

Matches the OfficeAQI pattern: a Portainer-managed stack rather than a
manual `docker compose up`, so it can be updated from a git pull instead
of a manual redeploy.

1. Push this repo to GitHub (see below).
2. In Portainer on Argos → Stacks → Add stack → Repository, point it at
   this repo, and deploy `docker-compose.yml` as-is.
3. It exposes the dashboard on **port 8098** (`http://192.168.1.17:8098/`).
   This was free when checked (8080 Gatus, 8081 the HomeLab-Pi5 web
   mirror, 8090 in use, 3000 Homepage, 9443 Portainer) — worth a quick
   re-check before first deploy in case that's changed.
4. Metric data persists in the `homelab_metrics_data` Docker volume, so
   redeploys/updates don't lose history.

## Repo setup

Two things push this to the Mac rather than doing it here, same as
OfficeAQI:

- This session's device bridge has no GitHub credentials configured.
- Running `git init`/`git commit` directly against this NAS-mounted
  folder throws `fatal: ... is not a valid object` - the same class of
  corruption already hit and root-caused on the OfficeAQI NAS folder
  (the network mount's write/caching behavior, not something retrying
  fixes). No `.git` directory has been created here for that reason.

So: copy this `HomelabMetrics/` folder to local disk on your Mac first
(e.g. `~/Projects/HomelabMetrics`), then from there:

```
cd ~/Projects/HomelabMetrics
git init
git add -A
git commit -m "Initial HomelabMetrics dashboard"
git remote add origin git@github.com:menthol1979/HomelabMetrics.git
git push -u origin main
```

(No commit exists yet anywhere - this NAS copy is the working tree, not a repo.)

## Open items not decided yet

- **Alerting:** intentionally left as a passive dashboard for now (no
  ntfy/Gatus threshold notice) per your call - revisit once you've seen
  real data and know what threshold actually matters.
- **Additional hosts:** Pyravlos wasn't included in this first pass;
  adding it later is just one more entry in `app/config.py`'s `HOSTS`
  dict plus confirming its Glances `sensors`/`smart` plugins are enabled.
