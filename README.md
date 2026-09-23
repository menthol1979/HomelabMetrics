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

No new agents run on any host, and this deliberately does **not** poll
Glances a second time for data the HomeLab-Pi5 project (the physical
Argos/Alcyone/Selene dashboard) is already polling every 3 seconds for
itself. Polling is split into three pieces instead:

- **Fast tier (every 3s, `MIRROR_POLL_INTERVAL`):** one HTTP call to
  HomeLab-Pi5's own live-state web mirror at
  `http://192.168.1.17:8081/api/state`, which already carries
  `cpu_temp`/`ssd_temp` (NVMe composite) for Argos, Alcyone, and Selene
  in a single response — that project's own 3s Glances poll, reused
  rather than duplicated. This tier drives the chart resolution and the
  backup-event peak-temperature tracking.
- **Slow tier (every 60s per host, `SLOW_POLL_INTERVAL`):** a direct,
  low-frequency call to each host's own `/api/4/sensors` and
  `/api/4/smart` for the handful of fields the mirror doesn't carry:
  NVMe Sensor 1/2, fan RPM, each host's own warn/crit thresholds, and
  the NVMe `critical_warning`/`media_errors`/`percentage_used` fields.
  These change slowly, so 60s is plenty, and this tier's load is
  negligible next to HomeLab-Pi5's continuous 3s poll.
- **Backup detection:** piggybacks on the fast tier's tick —
  `/api/4/processlist` on Argos only, matched against
  `raspiBackup`/`pigz`/`gzip` — so a backup's start/end is caught at
  the same ~3s resolution as the temperatures being compared against
  it. Nothing else in the fleet watches for this, so it isn't a
  duplicate of anything.
- **Live push:** every fast-tier tick is broadcast over `/ws` to any
  open browser tab, so the dashboard updates without polling itself.

Each stored metric row combines the latest fast-tier reading with
whatever the slow tier most recently cached for that host — so a row
is always "complete" even though its fields update at different rates
under the hood.

**Trade-off worth knowing:** `cpu_temp`/`nvme_composite_temp` now
depend on HomeLab-Pi5's `home-dash.service` (and its web-mirror thread)
being up on Argos. If that service is down, those two fields go blank
in this dashboard until it's back — the slow-tier fields (wear,
critical_warning, media_errors, fan, thresholds) and backup detection
are unaffected, since they're polled independently.

### Data sources, mapped

| Metric | Source |
| --- | --- |
| CPU temperature | HomeLab-Pi5 web mirror, `cpu_temp_c` (guarded by `has_cpu_temp`) |
| NVMe composite temp | HomeLab-Pi5 web mirror, `ssd_temp_c` (guarded by `has_ssd_temp`) |
| NVMe Sensor 1 / Sensor 2 temps | `/api/4/sensors`, labels `Sensor 1` / `Sensor 2` (same on all three hosts) — slow tier |
| NVMe critical-warning flag, percentage-used (wear), media errors | `/api/4/smart`, looked up by the attribute's `key` field (`criticalWarning`, `percentageUsed`, `integrityErrors` — Glances' name for nvme-cli's `media_errors`) — slow tier |
| Fan RPM | `/api/4/sensors`, label `pwmfan 0` — present on Argos/Selene's Active Coolers, absent on Alcyone — slow tier |
| Backup detection | `/api/4/processlist` on Argos, matched against `raspiBackup`/`pigz`/`gzip` — fast tier |

Per-sensor `warning`/`critical` thresholds are pulled from Glances too
(each host's own lm-sensors config, slow tier) and used for
color-coding on the frontend, rather than one hardcoded threshold for
every host.

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

### Updating the stack after a code change

Portainer's "Pull and redeploy" for a git-sourced, `build:`-based stack
has two failure modes that have actually been hit here — don't assume
a green "success" toast in Portainer means the running container
matches the latest commit:

- **Never check "Re-pull image".** This stack has no registry image —
  Docker builds it locally from the Dockerfile. Checking that box makes
  Portainer try to `docker pull docker.io/library/homelab-metrics-homelab-metrics:latest`,
  which doesn't exist on Docker Hub, and it fails with "pull access
  denied". Leave it unchecked.
- **Even unchecked, "Pull and redeploy" can silently recreate the
  container from a stale checkout** — Portainer reports success but
  the app's still running old code. If that happens, fix it directly
  on Argos:

  ```
  # 1. Find the stack's compose folder (path is inside Portainer's own
  #    container, not Argos's real filesystem):
  docker inspect homelab-metrics --format '{{ index .Config.Labels "com.docker.compose.project.working_dir" }}'
  # -> /data/compose/<N>

  # 2. Translate that to Argos's real path via Portainer's own bind mount:
  docker inspect portainer --format '{{ range .Mounts }}{{ .Destination }} -> {{ .Source }}{{ "\n" }}{{ end }}'
  # -> /data -> /home/nickkal/docker/portainer/data
  #    so the real folder is /home/nickkal/docker/portainer/data/compose/<N>

  # 3. That folder has no .git of its own - get a fresh checkout and sync it in:
  git clone https://github.com/menthol1979/HomelabMetrics.git /tmp/homelabmetrics-fresh
  sudo rsync -a --exclude '.git' /tmp/homelabmetrics-fresh/ /home/nickkal/docker/portainer/data/compose/<N>/
  rm -rf /tmp/homelabmetrics-fresh

  # 4. Rebuild under the SAME project name Portainer itself uses
  #    ("homelab-metrics"), so it reattaches to the existing data volume
  #    instead of creating a stray new one:
  sudo docker rm -f homelab-metrics
  sudo bash -c 'cd /home/nickkal/docker/portainer/data/compose/<N> && docker compose -p homelab-metrics up -d --build'
  ```

  A bare `docker compose up -d --build` from that folder *without*
  `-p homelab-metrics` derives the project name from the folder itself
  (e.g. `7`), which creates a parallel image/network/volume and looks
  exactly like the metric history got wiped — it didn't, it's just
  sitting unused under the wrong project name. `docker volume ls | grep
  homelab` shows both if this happens; the real one is
  `homelab-metrics_homelab_metrics_data`.

- **After any rebuild, hard-refresh the browser tab (Cmd+Shift+R).** A
  stale cached `app.js`/`index.html` pair can throw a JS error before
  the page ever opens its WebSocket, which looks exactly like being
  stuck on "connecting…" forever even though the backend is healthy.

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
