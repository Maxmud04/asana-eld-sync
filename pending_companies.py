"""
pending_companies.py

Shared file-based hand-off between a team's own isolated sync.py process
and the singleton control-bot process, for "this company has no dispatch
board section yet - which board should it join?" (see the plan's Phase 7,
item 3). Deliberately a plain JSON file living in the team's own directory
(teams/<team_id>/pending_companies.json) rather than the control-bot's
encrypted config_store, since a per-team sync.py process must never gain a
dependency on that store - see the plan's per-team-process isolation
design.

sync.py only ever ADDS entries (see run_one_cycle); control_bot only ever
REMOVES them (once an admin picks a board via an inline button - see
router.py). Both sides read-modify-write the whole file each time; safe
without file locking since a new company appearing is rare (nowhere near
frequent enough for the two processes to race on this file in practice).
"""

import hashlib
import json
import os

FILENAME = "pending_companies.json"


def pending_id_for(company_name, source):
    """A short, deterministic id for one (company_name, source) pair, so
    the same company is never added twice across cycles regardless of
    dict ordering or which process is asking."""
    return hashlib.sha1(f"{company_name}|{source}".encode("utf-8")).hexdigest()[:10]


def _path(team_dir):
    return os.path.join(team_dir, FILENAME)


def load(team_dir):
    path = _path(team_dir)
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save(team_dir, data):
    with open(_path(team_dir), "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def add_if_new(team_dir, company_name, source, first_seen_iso):
    """Returns the new pending_id if this was a genuinely new company (the
    caller should alert), or None if it was already pending (no
    double-alert on a company still awaiting a decision)."""
    data = load(team_dir)
    pending_id = pending_id_for(company_name, source)
    if pending_id in data:
        return None
    data[pending_id] = {"company_name": company_name, "source": source, "first_seen": first_seen_iso}
    save(team_dir, data)
    return pending_id


def pop(team_dir, pending_id):
    """Remove and return one entry (the caller has just assigned it to a
    board), or None if it wasn't there (already resolved, or a stale
    button tap)."""
    data = load(team_dir)
    entry = data.pop(pending_id, None)
    if entry is not None:
        save(team_dir, data)
    return entry


def get(team_dir, pending_id):
    """Read-only lookup - used by "Skip" (see router.py), which leaves the
    entry in place (so add_if_new's dedup check keeps treating this
    company as already-seen and never re-alerts on it) rather than
    removing it the way an actual board assignment does."""
    return load(team_dir).get(pending_id)
