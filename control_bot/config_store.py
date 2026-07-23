"""
control_bot/config_store.py

The multi-tenant control panel's single source of truth: one encrypted
SQLite database holding every team's credentials/config, which chat_id
belongs to which team, and any onboarding conversation still in progress.
Only the control_bot process ever touches this file - per-team sync.py
processes never gain this dependency (see the plan's config store
section).

Token columns are encrypted at rest with Fernet (symmetric, authenticated
encryption) using one master key file kept outside the repo. Losing that
key file makes every stored token unrecoverable - back it up somewhere
durable, this file does not do that for you.
"""

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

from cryptography.fernet import Fernet

_SCHEMA = """
CREATE TABLE IF NOT EXISTS teams (
    team_id TEXT PRIMARY KEY,
    team_name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'onboarding',
    workspace_gid TEXT,
    asana_team_gid TEXT,
    asana_token_enc BLOB,
    asana_project_ids TEXT,
    asana_database_project_id TEXT,
    asana_odometer_project_id TEXT,
    factor_session_token_enc BLOB,
    factor_tenant_id TEXT,
    factor_company_filter TEXT,
    factor_company_name TEXT,
    leader_session_token_enc BLOB,
    leader_tenant_id TEXT,
    staff_roster_json TEXT,
    algo_service_account_label TEXT,
    excluded_company_names_json TEXT,
    poll_interval_minutes REAL NOT NULL DEFAULT 5,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS onboarding_sessions (
    chat_id INTEGER PRIMARY KEY,
    state TEXT NOT NULL,
    data_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

# team_admins now allows one chat_id to administer several teams at once
# (see the plan's Phase 8 - "Add Another Team") - a composite primary key
# instead of chat_id alone. Created/migrated separately from _SCHEMA above
# (see _migrate_team_admins) since an existing on-disk table with the OLD
# single-column-PK shape needs real migration, not just "IF NOT EXISTS".
_SCHEMA_TEAM_ADMINS = """
CREATE TABLE IF NOT EXISTS team_admins (
    chat_id INTEGER NOT NULL,
    team_id TEXT NOT NULL,
    telegram_user_id INTEGER,
    added_at TEXT NOT NULL,
    PRIMARY KEY (chat_id, team_id)
);
"""

# Which of a chat's (possibly several) teams a bare command with no
# embedded team_id - /status, /trucks, the main menu itself - currently
# applies to. Irrelevant (and kept trivially in sync) for the common
# single-team-per-chat case; only matters once a chat administers >1 team.
_SCHEMA_CHAT_ACTIVE_TEAM = """
CREATE TABLE IF NOT EXISTS chat_active_team (
    chat_id INTEGER PRIMARY KEY,
    team_id TEXT NOT NULL
);
"""

# DB-backed replacement for pending_companies.py's per-team JSON file, for
# the single-service deployment mode (see multi_sync.py) where there is no
# per-team working directory to keep such a file in - one process handles
# every team. add_if_new/pop/get below mirror that module's exact
# semantics (see its own docstring) so router.py's callback handlers work
# identically regardless of which mode produced the alert.
_SCHEMA_PENDING_COMPANIES = """
CREATE TABLE IF NOT EXISTS pending_companies (
    team_id TEXT NOT NULL,
    pending_id TEXT NOT NULL,
    company_name TEXT NOT NULL,
    source TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    PRIMARY KEY (team_id, pending_id)
);
"""

# Which HOS Audit Transfer log entries (DOT inspector logbook transfers -
# see sync.py's check_fmcsa_transfers) a team has already been alerted about
# over Telegram, so a redeploy/restart doesn't re-alert on the same old
# inspection every time.
_SCHEMA_SEEN_FMCSA_TRANSFERS = """
CREATE TABLE IF NOT EXISTS seen_fmcsa_transfers (
    team_id TEXT NOT NULL,
    log_id TEXT NOT NULL,
    seen_at TEXT NOT NULL,
    PRIMARY KEY (team_id, log_id)
);
"""

# Fields whose value is stored encrypted (as "<field>_enc") rather than
# plaintext - see _prepare_row/_decode_team_row.
_ENCRYPTED_FIELDS = {"asana_token", "factor_session_token", "leader_session_token"}

# Fields stored as a JSON blob under a differently-named column.
_JSON_FIELDS = {
    "staff_roster": "staff_roster_json",
    "excluded_company_names": "excluded_company_names_json",
}


def _now():
    return datetime.now(timezone.utc).isoformat()


def _load_or_create_key(master_key_path):
    if os.path.exists(master_key_path):
        with open(master_key_path, "rb") as f:
            return f.read().strip()
    key = Fernet.generate_key()
    with open(master_key_path, "wb") as f:
        f.write(key)
    try:
        os.chmod(master_key_path, 0o600)
    except OSError:
        pass  # best-effort on platforms without POSIX permission bits
    return key


class ConfigStore:
    def __init__(self, db_path, master_key_path):
        self._db_path = db_path
        self._fernet = Fernet(_load_or_create_key(master_key_path))
        self._lock = threading.Lock()
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
            self._migrate_team_admins(conn)
            conn.executescript(_SCHEMA_CHAT_ACTIVE_TEAM)
            conn.executescript(_SCHEMA_PENDING_COMPANIES)
            conn.executescript(_SCHEMA_SEEN_FMCSA_TRANSFERS)

    def _migrate_team_admins(self, conn):
        """Create team_admins fresh (new composite-PK shape) if it doesn't
        exist yet, or migrate it in place if it's still the old
        single-chat_id-PK shape - preserving every existing row rather than
        requiring a manual reset (there was already one live team's data in
        here when this need was discovered)."""
        existing = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='team_admins'",
        ).fetchone()
        if existing is None:
            conn.executescript(_SCHEMA_TEAM_ADMINS)
            return
        if "PRIMARY KEY (chat_id, team_id)" in existing["sql"]:
            return  # already migrated
        conn.execute("ALTER TABLE team_admins RENAME TO team_admins_old")
        conn.executescript(_SCHEMA_TEAM_ADMINS)
        conn.execute(
            "INSERT INTO team_admins (chat_id, team_id, telegram_user_id, added_at) "
            "SELECT chat_id, team_id, telegram_user_id, added_at FROM team_admins_old",
        )
        conn.execute("DROP TABLE team_admins_old")

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _encrypt(self, value):
        return None if value is None else self._fernet.encrypt(value.encode("utf-8"))

    def _decrypt(self, value):
        return None if value is None else self._fernet.decrypt(bytes(value)).decode("utf-8")

    def _prepare_row(self, fields):
        row = {}
        for key, value in fields.items():
            if key in _ENCRYPTED_FIELDS:
                row[f"{key}_enc"] = self._encrypt(value)
            elif key in _JSON_FIELDS:
                row[_JSON_FIELDS[key]] = json.dumps(value)
            else:
                row[key] = value
        return row

    # ---------- teams ----------

    def create_team(self, team_id, team_name, **fields):
        """Insert a brand-new team row. fields may include any plaintext
        column plus asana_token/factor_session_token/leader_session_token
        (encrypted automatically) and staff_roster/excluded_company_names
        (JSON-encoded automatically)."""
        fields = dict(fields)
        status = fields.pop("status", "onboarding")
        row = self._prepare_row(fields)
        now = _now()
        columns = ["team_id", "team_name", "status", "created_at", "updated_at"] + list(row.keys())
        values = [team_id, team_name, status, now, now] + list(row.values())
        placeholders = ", ".join("?" for _ in values)
        with self._lock, self._connect() as conn:
            conn.execute(
                f"INSERT INTO teams ({', '.join(columns)}) VALUES ({placeholders})", values,
            )

    def update_team(self, team_id, **fields):
        """Update only the given columns (encrypting/JSON-encoding the same
        fields create_team does) and bump updated_at."""
        if not fields:
            return
        row = self._prepare_row(fields)
        set_clause = ", ".join(f"{col} = ?" for col in row) + ", updated_at = ?"
        with self._lock, self._connect() as conn:
            conn.execute(
                f"UPDATE teams SET {set_clause} WHERE team_id = ?",
                list(row.values()) + [_now(), team_id],
            )

    def get_team(self, team_id):
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM teams WHERE team_id = ?", (team_id,)).fetchone()
        return self._decode_team_row(row) if row else None

    def list_teams(self, status=None):
        with self._connect() as conn:
            if status:
                rows = conn.execute("SELECT * FROM teams WHERE status = ?", (status,)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM teams").fetchall()
        return [self._decode_team_row(row) for row in rows]

    def _decode_team_row(self, row):
        data = dict(row)
        data["asana_token"] = self._decrypt(data.pop("asana_token_enc"))
        data["factor_session_token"] = self._decrypt(data.pop("factor_session_token_enc"))
        data["leader_session_token"] = self._decrypt(data.pop("leader_session_token_enc"))
        data["staff_roster"] = json.loads(data.pop("staff_roster_json") or "null") or {}
        data["excluded_company_names"] = json.loads(data.pop("excluded_company_names_json") or "null") or []
        return data

    # ---------- chat_id -> team routing ----------

    def team_ids_for_chat(self, chat_id):
        """Every team this chat administers (a chat can now own several -
        see the plan's Phase 8, "Add Another Team"). Empty list if none."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT team_id FROM team_admins WHERE chat_id = ?", (chat_id,),
            ).fetchall()
        return [row["team_id"] for row in rows]

    def add_team_admin(self, chat_id, team_id, telegram_user_id):
        """Link chat_id to team_id (INSERT OR REPLACE is safe now that the
        primary key is the (chat_id, team_id) pair - it can no longer
        silently drop this chat's OTHER teams the way a chat_id-only key
        did). Auto-activates team_id for this chat the first time it gets
        any team at all, so the common single-team case never needs an
        explicit "Switch Team" step."""
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO team_admins (chat_id, team_id, telegram_user_id, added_at) "
                "VALUES (?, ?, ?, ?)",
                (chat_id, team_id, telegram_user_id, _now()),
            )
            has_active = conn.execute(
                "SELECT 1 FROM chat_active_team WHERE chat_id = ?", (chat_id,),
            ).fetchone()
            if has_active is None:
                conn.execute(
                    "INSERT INTO chat_active_team (chat_id, team_id) VALUES (?, ?)", (chat_id, team_id),
                )

    def get_active_team(self, chat_id):
        with self._connect() as conn:
            row = conn.execute(
                "SELECT team_id FROM chat_active_team WHERE chat_id = ?", (chat_id,),
            ).fetchone()
        return row["team_id"] if row else None

    def set_active_team(self, chat_id, team_id):
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO chat_active_team (chat_id, team_id) VALUES (?, ?) "
                "ON CONFLICT(chat_id) DO UPDATE SET team_id=excluded.team_id",
                (chat_id, team_id),
            )

    def chat_ids_for_team(self, team_id):
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT chat_id FROM team_admins WHERE team_id = ?", (team_id,),
            ).fetchall()
        return [row["chat_id"] for row in rows]

    # ---------- onboarding conversation state ----------

    def get_onboarding_session(self, chat_id):
        """Returns (state, data) or None if this chat has no conversation
        in progress."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT state, data_json FROM onboarding_sessions WHERE chat_id = ?", (chat_id,),
            ).fetchone()
        return (row["state"], json.loads(row["data_json"])) if row else None

    def save_onboarding_session(self, chat_id, state, data):
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO onboarding_sessions (chat_id, state, data_json, updated_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(chat_id) DO UPDATE SET state=excluded.state, "
                "data_json=excluded.data_json, updated_at=excluded.updated_at",
                (chat_id, state, json.dumps(data), _now()),
            )

    def clear_onboarding_session(self, chat_id):
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM onboarding_sessions WHERE chat_id = ?", (chat_id,))

    # ---------- pending companies ----------
    # sync.py no longer auto-alerts on a newly-detected, unassigned company
    # (that's now a deliberate admin action only - see router.py's "Company
    # Assign" menu) - get/pop below just resolve any button taps still
    # coming in against pending_companies rows created before that change.

    def get_pending_company(self, team_id, pending_id):
        with self._connect() as conn:
            row = conn.execute(
                "SELECT company_name, source, first_seen FROM pending_companies "
                "WHERE team_id = ? AND pending_id = ?",
                (team_id, pending_id),
            ).fetchone()
        return dict(row) if row else None

    def pop_pending_company(self, team_id, pending_id):
        """Remove and return one entry (the caller has just assigned it to
        a board), or None if it wasn't there (already resolved, or a stale
        button tap)."""
        entry = self.get_pending_company(team_id, pending_id)
        if entry is None:
            return None
        with self._lock, self._connect() as conn:
            conn.execute(
                "DELETE FROM pending_companies WHERE team_id = ? AND pending_id = ?",
                (team_id, pending_id),
            )
        return entry

    # ---------- HOS Audit Transfer alerts ----------

    def has_seen_fmcsa_transfer(self, team_id, log_id):
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM seen_fmcsa_transfers WHERE team_id = ? AND log_id = ?",
                (team_id, log_id),
            ).fetchone()
        return row is not None

    def mark_fmcsa_transfer_seen(self, team_id, log_id):
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO seen_fmcsa_transfers (team_id, log_id, seen_at) VALUES (?, ?, ?)",
                (team_id, log_id, _now()),
            )
