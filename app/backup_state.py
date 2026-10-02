"""
Tiny shared record of which hosts currently have an in-progress backup
event (host -> backup_events.id), set directly by the
POST /api/backup-events/{start,finish} handlers in main.py and read by
poller.run_fast_loop to flag in_backup_window on each metric row.

Deliberately process-memory only, not backed by the DB on the hot
path - main.py's two handlers are the only writers and poller.py's fast
loop the only reader, all on the same asyncio event loop, so there's no
polling and no race to guard against beyond what a single-threaded
event loop already gives for free. A stale entry left behind by a
crash is handled at startup instead: db.interrupt_stale_backup_events()
marks any DB row still 'in_progress' as 'interrupted', and this dict
starts empty every process start regardless, so the two can never
disagree for longer than one restart.
"""

active: dict[str, int] = {}
