"""
multi_sync.py

Single-process, multi-tenant sync engine: runs every active team's
dispatch-board, Database-board, and Odometer-Jump-board sync in one shared
loop, reading each team's own credentials fresh from control_bot's
ConfigStore every cycle. This replaces the old one-OS-process-per-team
design (control_bot/supervisor.py spawning a systemd unit per team) for a
deployment where there's only one running service and no ability to spawn
sibling processes (e.g. one Railway service) - a team's row simply gets
picked up by this same loop, no process to start or stop.

A rotated token (see control_bot/router.py's /rotatefactor etc.) takes
effect on this team's very next cycle automatically, since credentials are
read from config_store fresh every time - no restart needed, unlike the old
per-process design which had to bounce the process to pick up a rewritten
.env file.

Meant to be started as a background thread from control_bot/main.py,
alongside the Telegram bot itself. Reuses sync.py's run_one_cycle/
run_database_cycle/_check_token_expiry_warning exactly as they already
exist (see sync.py's own docstrings on the keyword params added to support
this) - nothing here duplicates that logic, it only supplies each team's
own credentials/state to it every cycle.
"""

import logging
import time
from concurrent.futures import ThreadPoolExecutor

import asana_client
import sync
import telegram_notifier

logger = logging.getLogger("multi_sync")

# Teams run CONCURRENTLY, not one after another - a large team (e.g. Texas,
# 1700+ drivers, 15-20+ minutes per cycle) would otherwise block every
# smaller team from getting its own updates for that whole time, every
# cycle - confirmed happening repeatedly for real once there was more than
# one team. Each team has its own AsanaClient/TeamRuntimeState/ELD session,
# and ConfigStore's own write lock already serializes concurrent writes -
# see config_store.py - so running these in parallel is safe.
MAX_PARALLEL_TEAMS = 8


class _TeamControl:
    """Duck-types the is_paused()/notify_all()/notify_with_buttons()
    interface sync.py's run_one_cycle/run_database_cycle expect (see
    telegram_notifier.TelegramNotifier, which this wraps for the actual
    sending). is_paused() checks this team's own config_store status
    ("paused" vs "active") instead of a flag file, since there's no
    separate per-team process/directory to keep one in - see router.py's
    /pause and /resume handlers, which set that same status column."""

    def __init__(self, bot_token, chat_ids, config_store, team_id, logger):
        self._notifier = telegram_notifier.TelegramNotifier(bot_token, chat_ids, logger)
        self._config_store = config_store
        self._team_id = team_id

    def is_paused(self):
        team = self._config_store.get_team(self._team_id)
        return bool(team) and team.get("status") == "paused"

    def notify_all(self, text):
        self._notifier.notify_all(text)

    def notify_with_buttons(self, text, buttons, columns=2):
        self._notifier.notify_with_buttons(text, buttons, columns)


class TeamRuntimeState:
    """Everything that needs to persist across cycles for one team, kept
    out of config_store since none of it is real configuration - it's
    this-process's own cache/dedup bookkeeping, lost (harmlessly) on
    restart. One instance per team_id, created lazily the first time that
    team is seen (see run_all_teams_once)."""

    def __init__(self):
        self.asana_client = None
        self.asana_token = None  # the token asana_client was built with
        self.token_state = sync.TokenAlertState()
        self.last_database_sync = 0.0


def _build_odometer_mapping(project_ids, odometer_ids_raw):
    """Parses config_store's single asana_odometer_project_id column into
    the {dispatch_project_id: odometer_project_id} mapping sync.py's
    _sync_odometer_board expects - same two shapes sync.py's own main()
    supports (see its docstring): a comma list the same length as
    project_ids (one Odometer Jump project per dispatch board), or a
    single id shared by every dispatch board. Returns None if unset, or if
    a multi-id list doesn't line up with project_ids (caller logs that as
    a misconfiguration rather than guessing)."""
    ids = [p.strip() for p in (odometer_ids_raw or "").split(",") if p.strip()]
    if not ids:
        return None
    if len(ids) == 1:
        return {pid: ids[0] for pid in project_ids}
    if len(ids) == len(project_ids):
        return dict(zip(project_ids, ids))
    return None


def run_team_cycle(config_store, bot_token, team_id, state, logger):
    """Run one dispatch-board sync cycle, and (on its own ~12h cadence via
    sync.DATABASE_SYNC_INTERVAL_SECONDS) one Database-board cycle, for a
    single team. Any exception here is caught - one team's failure must
    never stop the others from syncing this cycle (see run_all_teams_once)."""
    team = config_store.get_team(team_id)
    if team is None or team.get("status") != "active":
        return

    if not team.get("asana_token") or not team.get("asana_project_ids"):
        logger.warning("Team '%s': no Asana token/project configured yet - skipping.", team_id)
        return

    project_ids = [p.strip() for p in team["asana_project_ids"].split(",") if p.strip()]

    if state.asana_client is None or state.asana_token != team["asana_token"]:
        state.asana_client = asana_client.AsanaClient(team["asana_token"], project_ids, logger)
        state.asana_token = team["asana_token"]

    odometer_raw = team.get("asana_odometer_project_id")
    odometer_mapping = _build_odometer_mapping(project_ids, odometer_raw)
    if odometer_raw and odometer_mapping is None:
        logger.warning(
            "Team '%s': asana_odometer_project_id ('%s') doesn't line up with "
            "its %s dispatch board(s) - Odometer Jump sync disabled until fixed.",
            team_id, odometer_raw, len(project_ids),
        )

    chat_ids = config_store.chat_ids_for_team(team_id)
    control = _TeamControl(bot_token, chat_ids, config_store, team_id, logger)

    try:
        sync._check_token_expiry_warning(
            control,
            factor_token=team.get("factor_session_token"),
            leader_token=team.get("leader_session_token"),
            state=state.token_state,
        )
        if control.is_paused():
            logger.info("Team '%s': sync is paused - skipping this cycle.", team_id)
            return

        sync.run_one_cycle(
            state.asana_client, control, odometer_mapping,
            token_state=state.token_state,
            factor_session_token=team.get("factor_session_token"),
            factor_tenant_id=team.get("factor_tenant_id"),
            factor_company_filter=team.get("factor_company_filter"),
            leader_session_token=team.get("leader_session_token"),
            leader_tenant_id=team.get("leader_tenant_id"),
            staff_roster=team.get("staff_roster"),
            algo_label=team.get("algo_service_account_label") or None,
        )

        database_project_id = team.get("asana_database_project_id")
        if database_project_id and (
            time.time() - state.last_database_sync >= sync.DATABASE_SYNC_INTERVAL_SECONDS
        ):
            sync.run_database_cycle(
                state.asana_client, database_project_id, control, token_state=state.token_state,
                factor_session_token=team.get("factor_session_token"),
                factor_tenant_id=team.get("factor_tenant_id"),
                leader_session_token=team.get("leader_session_token"),
                leader_tenant_id=team.get("leader_tenant_id"),
            )
            state.last_database_sync = time.time()
    except Exception:
        logger.exception(
            "Team '%s': sync cycle raised an unhandled exception - skipping until next cycle.",
            team_id,
        )


def run_all_teams_once(config_store, bot_token, states_by_team_id, logger):
    """One pass over every currently-active team, run CONCURRENTLY (see
    MAX_PARALLEL_TEAMS above) rather than one after another.
    states_by_team_id is mutated in place (new teams get a fresh
    TeamRuntimeState the first time they're seen) - the caller owns that
    dict across calls so state persists between cycles."""
    teams = config_store.list_teams(status="active")
    logger.info("Multi-team sync cycle: %s active team(s).", len(teams))
    if not teams:
        return

    with ThreadPoolExecutor(max_workers=min(MAX_PARALLEL_TEAMS, len(teams))) as pool:
        team_id_by_future = {}
        for team in teams:
            team_id = team["team_id"]
            state = states_by_team_id.setdefault(team_id, TeamRuntimeState())
            future = pool.submit(run_team_cycle, config_store, bot_token, team_id, state, logger)
            team_id_by_future[future] = team_id

        for future, team_id in team_id_by_future.items():
            try:
                future.result()
            except Exception:
                # run_team_cycle already catches its own exceptions
                # internally - this only ever fires for something that
                # escaped before/around that (e.g. a config_store read
                # failing outright) - still must not stop the other
                # teams' futures from being collected.
                logger.exception(
                    "Team '%s': sync cycle raised an unhandled exception outside its own try/except.",
                    team_id,
                )


def sync_loop_forever(config_store, bot_token, logger, poll_interval_minutes=5):
    """Runs forever, one pass over every active team every
    poll_interval_minutes. Meant to be run on its own background thread -
    see control_bot/main.py."""
    states_by_team_id = {}
    interval_seconds = poll_interval_minutes * 60
    logger.info("Multi-team sync loop starting - running every %s minute(s).", poll_interval_minutes)
    while True:
        try:
            run_all_teams_once(config_store, bot_token, states_by_team_id, logger)
        except Exception:
            logger.exception("Multi-team sync loop: unexpected top-level error this cycle.")
        time.sleep(interval_seconds)


def _check_one_team_fmcsa(config_store, bot_token, team_id, logger):
    """One team's own HOS Audit Transfer check - no AsanaClient/dispatch-
    board dependency at all (see sync.check_fmcsa_transfers), which is why
    this runs on its own much faster loop below rather than piggybacking on
    the 5-minute dispatch cycle."""
    team = config_store.get_team(team_id)
    if team is None or team.get("status") != "active":
        return
    chat_ids = config_store.chat_ids_for_team(team_id)
    control = _TeamControl(bot_token, chat_ids, config_store, team_id, logger)
    if control.is_paused():
        return
    sync.check_fmcsa_transfers(
        control,
        seen_checker=lambda log_id: config_store.has_seen_fmcsa_transfer(team_id, log_id),
        mark_seen=lambda log_id: config_store.mark_fmcsa_transfer_seen(team_id, log_id),
        factor_session_token=team.get("factor_session_token"),
        factor_tenant_id=team.get("factor_tenant_id"),
        factor_company_filter=team.get("factor_company_filter"),
        leader_session_token=team.get("leader_session_token"),
        leader_tenant_id=team.get("leader_tenant_id"),
    )


def run_fmcsa_check_once(config_store, bot_token, logger):
    """One pass over every active team's HOS Audit Transfer check, run
    concurrently (same reasoning as run_all_teams_once)."""
    teams = config_store.list_teams(status="active")
    if not teams:
        return
    with ThreadPoolExecutor(max_workers=min(MAX_PARALLEL_TEAMS, len(teams))) as pool:
        team_id_by_future = {
            pool.submit(_check_one_team_fmcsa, config_store, bot_token, team["team_id"], logger): team["team_id"]
            for team in teams
        }
        for future, team_id in team_id_by_future.items():
            try:
                future.result()
            except Exception:
                logger.exception(
                    "Team '%s': HOS Audit Transfer check raised an unhandled exception outside "
                    "its own try/except.", team_id,
                )


def fmcsa_check_loop_forever(config_store, bot_token, logger, poll_interval_seconds=30):
    """Runs forever, checking every active team's HOS Audit Transfer history
    every poll_interval_seconds - deliberately its own separate, much faster
    loop from sync_loop_forever's 5-minute dispatch cycle, since a
    roadside-inspector transfer alert is time-sensitive in a way status
    updates aren't. Kept well above ~10s on purpose: a full pass already
    means one request per company (~130+ for Texas alone, Factor + Leader
    combined) at MAX_PARALLEL_COMPANY_FETCHES concurrency, which itself
    takes on the order of 10-15 seconds - polling faster than that would
    mean back-to-back passes with no rest, which is exactly what caused the
    403 rate-limit storms the regular dispatch sync had to add backoff for
    (see eld_factor.py's _request_with_retries). 30s leaves real breathing
    room while still alerting far faster than the 5-minute dispatch cycle."""
    logger.info("HOS Audit Transfer check loop starting - running every %s second(s).", poll_interval_seconds)
    while True:
        try:
            run_fmcsa_check_once(config_store, bot_token, logger)
        except Exception:
            logger.exception("HOS Audit Transfer check loop: unexpected top-level error this cycle.")
        time.sleep(poll_interval_seconds)
