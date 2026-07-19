"""
control_bot/provisioning.py

Turns a completed onboarding conversation (see onboarding.py) into: real
Asana boards created from scratch (asana_client.py's bootstrap_* methods),
a saved team config row, a generated per-team .env file, and a running
systemd unit for that team's own isolated sync.py process.
"""

import logging
import os

import asana_client

_logger = logging.getLogger("control_bot.provisioning")

# One dispatch board to start - the data model (a variable-length
# comma-separated ASANA_PROJECT_IDS list) already supports more; a team can
# get a 2nd/3rd added later without any new plumbing, see the plan.
_DISPATCH_BOARD_SUFFIX = "Dispatch"
_DATABASE_BOARD_SUFFIX = "Database"
_ODOMETER_BOARD_SUFFIX = "Odometer Jump"


class Provisioner:
    def __init__(self, config_store, supervisor, teams_root_dir, shared_bot_token, logger=None):
        self.config_store = config_store
        self.supervisor = supervisor
        self.teams_root_dir = teams_root_dir
        self.shared_bot_token = shared_bot_token
        self.logger = logger or _logger

    def provision_team(self, team_id, data):
        client = asana_client.AsanaClient(data["asana_token"], [], self.logger)
        workspace_gid = data["workspace_gid"]
        team_gid = data.get("asana_team_gid")
        team_name = data["team_name"]

        dispatch_project_id = client.bootstrap_dispatch_project(
            workspace_gid, f"{team_name} {_DISPATCH_BOARD_SUFFIX}", team_gid,
        )
        database_project_id = client.bootstrap_database_project(
            workspace_gid, f"{team_name} {_DATABASE_BOARD_SUFFIX}", team_gid,
        )
        odometer_project_id = client.bootstrap_odometer_project(
            workspace_gid, f"{team_name} {_ODOMETER_BOARD_SUFFIX}", team_gid,
        )

        self._populate_staff_roster(client, dispatch_project_id, data.get("staff_roster") or {})

        self.config_store.create_team(
            team_id, team_name,
            status="active",
            workspace_gid=workspace_gid,
            asana_team_gid=team_gid,
            asana_token=data["asana_token"],
            asana_project_ids=dispatch_project_id,
            asana_database_project_id=database_project_id,
            asana_odometer_project_id=odometer_project_id,
            factor_session_token=data.get("factor_session_token"),
            factor_tenant_id=data.get("factor_tenant_id"),
            leader_session_token=data.get("leader_session_token"),
            leader_tenant_id=data.get("leader_tenant_id"),
            staff_roster=data.get("staff_roster") or {},
        )

        self.rewrite_env(team_id)
        self.supervisor.enable_and_start(team_id)

    def _populate_staff_roster(self, client, dispatch_project_id, staff_roster):
        """Add each roster entry as a Staff ID option (and its matching
        Staff ID History option). These two fields are never generic across
        teams (confirmed live on the existing boards - they're one team's
        own staff codes), unlike Violation, so bootstrap_dispatch_project()
        creates them empty and this fills them in from what onboarding
        collected. Reuses _get_project_config directly (rather than adding
        a public wrapper) since this is the same cache-building lookup
        every other method on AsanaClient already relies on internally."""
        if not staff_roster:
            return
        config = client._get_project_config(dispatch_project_id)
        for first_name, code in staff_roster.items():
            client.add_enum_option(config["staff_id_field_gid"], f"#{code}")
            client.add_enum_option(config["staff_history_field_gid"], f"{first_name.title()} {code}")

    def rewrite_env(self, team_id):
        """(Re)generate teams/<team_id>/.env from the current config_store
        row - called once at initial provisioning, and again any time
        router.py rotates a token, so the file sync.py actually reads from
        never goes stale relative to what's in the encrypted store."""
        team = self.config_store.get_team(team_id)
        team_dir = os.path.join(self.teams_root_dir, team_id)
        os.makedirs(team_dir, exist_ok=True)
        env_path = os.path.join(team_dir, ".env")
        lines = [
            f"TEAM_ID={team_id}",
            f"ASANA_TOKEN={team['asana_token']}",
            f"ASANA_PROJECT_IDS={team['asana_project_ids']}",
            f"ASANA_DATABASE_PROJECT_ID={team['asana_database_project_id']}",
            # Plural: one Odometer Jump project per dispatch board, comma-
            # separated in the same order as ASANA_PROJECT_IDS. A team
            # provisioned through the bot has exactly one dispatch board, so
            # this is a single id (no comma) - see sync.py's main() for how
            # a team like "original" with 3 dispatch boards uses 3 here.
            f"ASANA_ODOMETER_PROJECT_IDS={team['asana_odometer_project_id']}",
        ]
        if team.get("factor_session_token"):
            lines.append(f"FACTOR_SESSION_TOKEN={team['factor_session_token']}")
            lines.append(f"FACTOR_TENANT_ID={team['factor_tenant_id']}")
        if team.get("leader_session_token"):
            lines.append(f"LEADER_SESSION_TOKEN={team['leader_session_token']}")
            lines.append(f"LEADER_TENANT_ID={team['leader_tenant_id']}")
        lines.append("CONTROL_MODE=notifier")
        lines.append(f"TELEGRAM_BOT_TOKEN={self.shared_bot_token}")
        # Every chat_id registered for this team in team_admins (see
        # config_store.add_team_admin/chat_ids_for_team) - not tracked as
        # its own column on the teams row, so it can't go stale relative to
        # who's actually allowed to control this team.
        chat_ids = self.config_store.chat_ids_for_team(team_id)
        lines.append(f"TEAM_CHAT_IDS={','.join(str(c) for c in chat_ids)}")
        with open(env_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
