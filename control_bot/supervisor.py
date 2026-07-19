"""
control_bot/supervisor.py

Runs `systemctl` for a team_id's isolated sync.py process (see the plan's
systemd unit template, eld-sync@<team_id>.service) and manages that team's
paused.flag (see telegram_notifier.py - the file per-team sync.py checks
each sleep chunk, no restart needed for pause/resume).

Needs a narrowly-scoped sudoers rule permitting only `systemctl
{start,stop,restart,enable,disable} eld-sync@*` for the control-bot's own
service account - never run this process as root.
"""

import logging
import os
import subprocess

_logger = logging.getLogger("control_bot.supervisor")

_UNIT_TEMPLATE = "eld-sync@{team_id}.service"
PAUSED_FLAG_FILENAME = "paused.flag"


class Supervisor:
    def __init__(self, teams_root_dir, logger=None):
        self.teams_root_dir = teams_root_dir
        self.logger = logger or _logger

    def _systemctl(self, *args):
        try:
            result = subprocess.run(
                ["systemctl", *args], capture_output=True, text=True, timeout=30,
            )
        except OSError as exc:
            # No systemd on this machine at all (e.g. local Windows testing
            # before real VPS deployment - see the plan's Phase 2/6) -
            # degrade gracefully rather than crash whichever bot command
            # triggered this (confirmed happening for real: /status silently
            # sent nothing back because this raised before the reply).
            self.logger.warning(
                "systemctl unavailable (%s) - process supervision isn't "
                "available in this environment.", exc,
            )
            return subprocess.CompletedProcess(args, returncode=1, stdout="", stderr=str(exc))
        if result.returncode != 0:
            self.logger.warning("systemctl %s failed: %s", args, result.stderr.strip())
        return result

    def enable_and_start(self, team_id):
        self._systemctl("enable", "--now", _UNIT_TEMPLATE.format(team_id=team_id))

    def rotate_and_restart(self, team_id):
        """Called after config_store's token column was already updated and
        the team's .env regenerated - this just bounces the process so it
        picks up the new token on its next start (this codebase treats
        credentials as load-once-per-process, see the plan)."""
        self._systemctl("restart", _UNIT_TEMPLATE.format(team_id=team_id))

    def disable_and_stop(self, team_id):
        """Stops syncing without deleting anything - the team's config row,
        .env, and logs are all left in place."""
        self._systemctl("disable", "--now", _UNIT_TEMPLATE.format(team_id=team_id))

    def get_status(self, team_id):
        result = self._systemctl(
            "show", _UNIT_TEMPLATE.format(team_id=team_id), "--property=ActiveState,SubState,NRestarts",
        )
        return result.stdout.strip().replace("\n", ", ") or "unknown (no process supervisor available here)"

    def pause_team(self, team_id):
        open(self._paused_flag_path(team_id), "w").close()

    def resume_team(self, team_id):
        path = self._paused_flag_path(team_id)
        if os.path.exists(path):
            os.remove(path)

    def _paused_flag_path(self, team_id):
        return os.path.join(self.teams_root_dir, team_id, PAUSED_FLAG_FILENAME)
