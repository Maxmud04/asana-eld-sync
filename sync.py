"""
sync.py

This is the main program. Run it and it will:

  1. Ask Factor ELD (and, once it's set up, Leader ELD) for every driver's
     current duty status.
  2. Find that driver's task in Asana (by name) and update their "Duty
     Status" dropdown to match.
  3. Log exactly what changed, e.g. "John Doe: On Duty -> Driving".
  4. Repeat every few minutes forever (or just once, if you pass --once).

If one ELD platform fails (network error, expired login, etc.), the other
one still runs normally - a problem with one source never stops the whole
sync.

HOW TO RUN IT:
    python sync.py --once      # run a single sync and exit (good for testing)
    python sync.py             # run forever, syncing on a timer

All secrets and IDs (API keys, tokens, project IDs) come from a file named
.env sitting next to this script - see .env.example for the full list.
"""

import argparse
import base64
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone

from dotenv import load_dotenv

import asana_client
import eld_factor
import eld_leader
import telegram_control
import telegram_notifier
from asana_client import normalize_company_name, normalize_name, vehicle_field_value, word_sort_key
from eld_common import higher_priority_status
from eld_common import invisibility_reason as compute_invisibility_reason

load_dotenv(".env")  # loaded relative to the process's CWD (per-team when
# run from teams/<team_id>/ - see the plan) - NOT load_dotenv() with no
# args, which searches upward from THIS FILE's own location (sync.py always
# lives in the repo root) rather than from the process's actual working
# directory. Confirmed the hard way: running from teams/original/ silently
# still loaded the repo root's .env until this was fixed.

# Some Asana dropdown option names contain characters Windows' default
# console encoding (cp1252) can't display (confirmed: invisible left-to-
# right marks in Maxmud Test A/B's abbreviated Status options - see
# asana_client.STATUS_ABBREVIATION_ALIASES) - without this, logging a
# message that includes one crashes with UnicodeEncodeError instead of just
# printing it. errors="replace" swaps anything unprintable for "?" rather
# than losing the whole log line.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="replace")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("sync.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("sync")


def _lookup_matches(name, task_index, fallback_index):
    """Find a name's existing Asana task(s): exact match first, then the
    word-order-independent fallback (see asana_client.word_sort_key)."""
    matches = task_index.get(normalize_name(name), [])
    if not matches:
        matches = fallback_index.get(word_sort_key(name), [])
    return matches


def _group_co_drivers(all_drivers, logger):
    """Split all fetched drivers into solo drivers and co-driver pairs.

    Confirmed against live Factor ELD data: two drivers sharing one truck
    both report the same (non-blank) vehicle_number, and an inactive/"Off
    Platform" driver always reports a blank one - so grouping active
    drivers by (company, vehicle_number) is a reliable way to find real
    co-driver teams, without ever pairing someone who's actually solo.

    Returns (solo_drivers, combined_units)
      solo_drivers   : [Driver, ...] - handled exactly like before this
                       feature existed.
      combined_units : [ {name, status, vehicle_number, violation,
                           company_name, source, member_names: [name, name]},
                          ... ] - one shared Asana task per pair, named
                       "First & Second" (alphabetical, so the title never
                       reorders itself between runs) with whichever status
                       ranks higher in eld_common.STATUS_PRIORITY_ORDER (e.g.
                       "Driving" beats "Sleeping" when co-drivers are taking
                       turns), and whichever violation is non-None if only
                       one co-driver currently has one.
    """
    groups = {}
    solo_drivers = []
    for driver in all_drivers:
        vehicle_key = (driver.vehicle_number or "").strip()
        if driver.status == "Off Platform" or not vehicle_key:
            solo_drivers.append(driver)
            continue
        group_key = (normalize_company_name(driver.company_name or ""), vehicle_key)
        groups.setdefault(group_key, []).append(driver)

    combined_units = []
    for (company_key, vehicle_key), members in groups.items():
        if len(members) == 1:
            solo_drivers.append(members[0])
        elif len(members) == 2:
            ordered = sorted(members, key=lambda d: normalize_name(d.name))
            combined_violation = ordered[0].violation or ordered[1].violation
            if ordered[0].violation and ordered[1].violation and ordered[0].violation != ordered[1].violation:
                logger.warning(
                    "Co-driver pair '%s' & '%s' both have different active "
                    "violations (%s / %s) - showing '%s' on their shared task.",
                    ordered[0].name, ordered[1].name,
                    ordered[0].violation, ordered[1].violation, combined_violation,
                )
            combined_units.append({
                "name": f"{ordered[0].name} & {ordered[1].name}",
                "status": higher_priority_status(ordered[0].status, ordered[1].status),
                "vehicle_number": vehicle_key,
                "violation": combined_violation,
                "company_name": ordered[0].company_name,
                "source": ordered[0].source,
                "member_names": [ordered[0].name, ordered[1].name],
                "member_driver_ids": [ordered[0].driver_id, ordered[1].driver_id],
            })
        else:
            # 3+ drivers sharing one vehicle number shouldn't happen for a
            # normal co-driver team - rather than guess how to combine them,
            # log it and leave every one of them as a solo driver.
            logger.warning(
                "%s drivers share vehicle number '%s' at company '%s' - "
                "expected at most 2 for a co-driver pair. Leaving each as a "
                "solo driver instead of guessing how to combine them: %s",
                len(members), vehicle_key, company_key, [m.name for m in members],
            )
            solo_drivers.extend(members)

    return solo_drivers, combined_units


def _resolve_staff_for_driver(driver, staff_editors_by_id):
    """Look up (staff_id, staff_history) for one solo driver from
    eld_factor.fetch_staff_editors' results, keyed by driver_id. staff_id is
    always None - the "Staff ID" dropdown is manually managed in Asana now
    (confirmed directly) - only staff_history ("Staff ID History") gets
    written by the sync. Returns (None, None) if we don't have a driver_id,
    or no recognized editor was found for them this cycle."""
    if not driver.driver_id:
        return None, None
    result = staff_editors_by_id.get(driver.driver_id)
    if result is None:
        return None, None
    _staff_id, display_name, _created_at = result
    return None, display_name


def _resolve_staff_for_combined_unit(unit, staff_editors_by_id, logger):
    """Look up (staff_id, staff_history) for a co-driver pair. Each member
    has their own separate logbook/commit history, so if both were recently
    edited by a recognized staff member, show whichever edit is actually
    more recent rather than picking one arbitrarily. staff_id is always None
    - see _resolve_staff_for_driver for why."""
    candidates = [
        staff_editors_by_id[driver_id]
        for driver_id in unit["member_driver_ids"]
        if driver_id and staff_editors_by_id.get(driver_id) is not None
    ]
    if not candidates:
        return None, None

    candidates.sort(key=lambda c: c[2] or "", reverse=True)
    _staff_id, display_name, _created_at = candidates[0]
    if len(candidates) > 1 and candidates[0][0] != candidates[1][0]:
        logger.info(
            "Co-driver pair '%s': both logbooks were recently edited by "
            "different staff - showing the more recent one (%s).",
            unit["name"], display_name,
        )
    return None, display_name


class TokenAlertState:
    """Per-team dedup state for the two proactive Telegram alerts below (a
    dead-token failure, a token-expiring-soon warning) - see
    _handle_factor_fetch_failure/_check_token_expiry_warning. Kept on this
    small object, rather than module globals, so multi_sync.py's per-team
    loop can give each team its own instance (a shared global would let one
    team's dead Factor ELD token suppress another team's identical alert).
    The single-team main() below never constructs one explicitly - every
    function here defaults to _default_token_alert_state, preserving its
    exact original single-team behavior."""

    def __init__(self):
        self.factor_token_alert_sent = False
        self.last_warned_token_exp = {"FACTOR_SESSION_TOKEN": None, "LEADER_SESSION_TOKEN": None}


# Tracks whether we've already sent a Telegram alert for the CURRENT Factor
# ELD token outage, so a dead token doesn't spam a new message every single
# cycle - just once when it first breaks, and the state resets the moment a
# fetch succeeds again (see _handle_factor_fetch_failure / _mark_factor_
# fetch_ok). Shared across run_one_cycle and run_database_cycle since both
# can hit the exact same underlying dead-token problem.
_default_token_alert_state = TokenAlertState()


def _is_factor_token_error(exc):
    # eld_factor.py raises this exact wording for any 401 from either
    # Factor ELD or Leader ELD (same backend, same failure shape), whether
    # the real server-side reason is a plain expiry or an early
    # invalidation (e.g. "TOKEN_NOT_ACTIVE" from a new login elsewhere) -
    # either way, the fix is the same: send a fresh token.
    return "rejected the request as unauthorized" in str(exc)


def _handle_factor_fetch_failure(exc, control, state=None):
    """Proactively alert via Telegram the first time a cycle fails because
    a Factor ELD/Leader ELD token is dead - the one failure mode that
    actually needs the user to do something. Does nothing for other kinds
    of failures (network hiccups, etc.), if no Telegram control is
    running, or if we've already alerted for this same ongoing outage."""
    state = state or _default_token_alert_state
    if control is None or not _is_factor_token_error(exc) or state.factor_token_alert_sent:
        return
    control.notify_all(
        f"⚠️ Sync is failing - {exc}\n\nSend a fresh token here (or /settoken) to resume syncing."
    )
    state.factor_token_alert_sent = True


def _mark_factor_fetch_ok(state=None):
    (state or _default_token_alert_state).factor_token_alert_sent = False


# How many days ahead of its own expiry to warn about a session token, so
# there's time to renew it before a cycle actually fails (confirmed the
# token itself carries a real "exp" claim we can just read locally - no
# network call needed to check this). A token's actual lifetime (we've seen
# anywhere from under a day to ~30 days) is decided entirely by Factor/
# Leader ELD's own login flow, not by anything here - this constant just
# controls how early we nag about whatever expiry the token itself states.
TOKEN_EXPIRY_WARNING_DAYS = 3

def _decode_token_expiry(token):
    """Return a session JWT's own 'exp' claim as a UTC datetime, or None if
    it's not in the expected three-part JWT shape. Never verifies the
    signature - we don't have Factor/Leader ELD's signing key, and don't
    need one just to read this one timestamp back out."""
    try:
        payload_b64 = token.split(".")[1]
        padded = payload_b64 + "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded))
        exp = payload.get("exp")
        if exp is None:
            return None
        return datetime.fromtimestamp(exp, tz=timezone.utc)
    except Exception:
        return None


def _check_one_token_expiry(control, env_var_name, platform_label, token_value=None, state=None):
    state = state or _default_token_alert_state
    # Falls back to the process's own env var when no explicit token_value
    # is given - preserves main()'s original single-team behavior exactly.
    if token_value is None:
        token_value = os.environ.get(env_var_name, "")
    exp_dt = _decode_token_expiry(token_value)
    if exp_dt is None:
        return

    days_remaining = (exp_dt - datetime.now(timezone.utc)).total_seconds() / 86400
    if days_remaining > TOKEN_EXPIRY_WARNING_DAYS:
        state.last_warned_token_exp[env_var_name] = None  # fresh/renewed token - reset for next time
        return
    if state.last_warned_token_exp[env_var_name] == exp_dt:
        return  # already warned about this exact token

    control.notify_all(
        f"⏰ Heads up: the {platform_label} token expires in about "
        f"{days_remaining:.1f} day(s) ({exp_dt:%Y-%m-%d %H:%M} UTC). "
        f"Renew it soon (log in to {platform_label}, grab the fresh access_token) "
        f"so syncing doesn't stop."
    )
    state.last_warned_token_exp[env_var_name] = exp_dt


def _check_token_expiry_warning(control, factor_token=None, leader_token=None, state=None):
    """Proactively warn via Telegram once either Factor ELD's or Leader
    ELD's current token is within TOKEN_EXPIRY_WARNING_DAYS of its own
    stated expiry - lets you renew ahead of time instead of only finding
    out once a cycle actually fails (see _handle_factor_fetch_failure for
    that reactive case). Checks both platforms independently - a fresh
    Factor token doesn't reset Leader's dedup state or vice versa.
    factor_token/leader_token let a caller (multi_sync.py) pass a specific
    team's own token explicitly instead of relying on process env vars -
    the single-team main() below omits them, keeping its original
    env-var-based behavior."""
    if control is None:
        return
    _check_one_token_expiry(control, "FACTOR_SESSION_TOKEN", "Factor ELD", factor_token, state)
    _check_one_token_expiry(control, "LEADER_SESSION_TOKEN", "Leader ELD", leader_token, state)


def _sync_odometer_board(
    asana, odometer_project_ids_by_dispatch_id, logger, section_index,
    factor_session_token=None, factor_tenant_id=None,
    leader_session_token=None, leader_tenant_id=None,
):
    """Sync every dispatch board's own Odometer Jump project: one task per
    driver who currently has an active odometer problem ("Odometer jump"
    or "Odometer is missing"), from BOTH Factor ELD and Leader ELD
    (confirmed shared/common across both platforms). One separate Odometer
    Jump project per dispatch board (Texas A/B/C each have their own -
    odometer_project_ids_by_dispatch_id maps a dispatch board's project_id
    to its own Odometer Jump project_id, built in run_one_cycle), sectioned
    by COMPANY within each project, exactly like the dispatch boards
    themselves. Which project a company's issue belongs to is looked up
    via section_index (the same one run_one_cycle uses); a company not yet
    placed on any dispatch board is skipped with a warning rather than
    guessed at. Unlike the Database board, a task here only exists while
    Factor/Leader ELD is still reporting the problem - once it's no longer
    active, the task is deleted, the same way the dispatch boards delete a
    task once a driver is no longer visible. The four *_token/*_tenant_id
    params let a caller (multi_sync.py) pass one team's own credentials
    explicitly instead of relying on process env vars - main() below omits
    them, keeping its original env-var-based behavior."""
    issues_by_driver_id = {}
    try:
        issues_by_driver_id.update(eld_factor.fetch_odometer_issues(
            logger, session_token=factor_session_token, tenant_id=factor_tenant_id,
        ))
    except Exception:
        logger.exception("Factor ELD: odometer issue fetch failed this run.")

    try:
        issues_by_driver_id.update(eld_leader.fetch_odometer_issues(
            logger, session_token=leader_session_token, tenant_id=leader_tenant_id,
        ))
    except Exception:
        logger.exception("Leader ELD: odometer issue fetch failed this run.")

    issues_by_odometer_project = {}
    for issue in issues_by_driver_id.values():
        name, company_name, issue_type, occurred_at = (
            issue["name"], issue["company_name"], issue["issue_type"], issue["occurred_at"],
        )
        board_info = section_index.get(normalize_company_name(company_name))
        if board_info is None:
            logger.warning(
                "Odometer Jump board: '%s' (company '%s') has an active "
                "odometer issue, but that company isn't on any dispatch "
                "board yet - skipping until it's assigned a board.",
                name, company_name,
            )
            continue
        odometer_project_id = odometer_project_ids_by_dispatch_id.get(board_info["project_id"])
        if odometer_project_id is None:
            logger.warning(
                "Odometer Jump board: no Odometer Jump project configured "
                "for dispatch board '%s' - skipping '%s' (company '%s').",
                board_info["project_name"], name, company_name,
            )
            continue
        issues_by_odometer_project.setdefault(odometer_project_id, []).append(
            (name, company_name, issue_type, occurred_at),
        )

    for odometer_project_id, issues in issues_by_odometer_project.items():
        _sync_one_odometer_project(asana, odometer_project_id, issues, logger)


def _sync_one_odometer_project(asana, odometer_project_id, issues, logger):
    """Reconcile one dispatch board's own Odometer Jump project against its
    slice of currently-active issues - see _sync_odometer_board."""
    try:
        index = asana.build_odometer_task_index(odometer_project_id)
    except Exception:
        logger.exception("Odometer Jump board: could not read existing tasks from Asana - skipping this project this run.")
        return

    keys_still_active = set()
    for name, company_name, issue_type, occurred_at in issues:
        key = (normalize_company_name(company_name), normalize_name(name))
        keys_still_active.add(key)
        existing = index.get(key)

        if existing is None:
            try:
                new_task_gid = asana.create_odometer_task(odometer_project_id, company_name, name, issue_type, occurred_at)
                logger.info("Odometer Jump board: created task for '%s' (%s, %s).", name, issue_type, occurred_at)
                # Same fix as the Database board: a second driver_id sharing
                # this same (company, name) later in this loop must see the
                # task just created, not create a duplicate - see the
                # matching comment in run_database_cycle for why.
                index[key] = {
                    "task_gid": new_task_gid, "task_title": name,
                    "current_odometer": issue_type, "current_date": occurred_at,
                }
            except Exception:
                logger.exception("Odometer Jump board: failed to create task for '%s'.", name)
            continue

        if existing["current_odometer"] != issue_type or existing.get("current_date") != occurred_at:
            try:
                changed = asana.update_odometer_task(odometer_project_id, existing, issue_type, occurred_at)
                if changed:
                    logger.info("Odometer Jump board: '%s' updated to '%s' (%s).", name, issue_type, occurred_at)
            except Exception:
                logger.exception("Odometer Jump board: failed to update task for '%s'.", name)

    for key, match in index.items():
        if key in keys_still_active:
            continue
        try:
            asana.delete_task(match["task_gid"])
            logger.info(
                "Odometer Jump board: deleted task '%s' - no longer an "
                "active odometer issue.", match["task_title"],
            )
        except Exception:
            logger.exception(
                "Odometer Jump board: failed to delete resolved task '%s'.",
                match["task_title"],
            )

    try:
        asana.cleanup_empty_odometer_sections(odometer_project_id)
    except Exception:
        logger.exception("Odometer Jump board: failed to clean up empty company sections.")


def run_one_cycle(
    asana, control=None, odometer_project_ids_by_dispatch_id=None,
    token_state=None,
    factor_session_token=None, factor_tenant_id=None, factor_company_filter=None,
    leader_session_token=None, leader_tenant_id=None,
    staff_roster=None, algo_label=None,
):
    """Fetch drivers from every ELD platform, match them to Asana tasks,
    and update anything that changed. Returns nothing - everything
    interesting is written to the log.

    Every keyword-only-in-spirit param after odometer_project_ids_by_
    dispatch_id lets a caller (multi_sync.py's per-team loop) pass that
    team's own state/credentials explicitly instead of relying on process
    env vars or module-global dedup state - see TokenAlertState's
    docstring. main() below omits all of them, so its single-team behavior
    is completely unchanged.

    A driver whose company has no section on any dispatch board is only
    ever logged as a warning here - never alerted over Telegram. Adding a
    brand-new company to a board is a deliberate, admin-initiated action
    via the bot's "Company Assign" menu (see control_bot/router.py's
    _show_menu_create_section_boards/_prompt_create_section), not
    something this sync loop offers to do automatically."""

    all_drivers = []

    # Each platform is wrapped in its own try/except so that if one of them
    # is down (network error, expired login, bad response, etc.) the other
    # one still runs normally instead of the whole sync failing.
    try:
        all_drivers.extend(eld_factor.fetch_drivers(
            logger, session_token=factor_session_token, tenant_id=factor_tenant_id,
            company_filter=factor_company_filter,
        ))
        _mark_factor_fetch_ok(token_state)
    except Exception as exc:
        logger.exception("Factor ELD fetch failed this run - continuing without it.")
        _handle_factor_fetch_failure(exc, control, token_state)

    try:
        all_drivers.extend(eld_leader.fetch_drivers(
            logger, session_token=leader_session_token, tenant_id=leader_tenant_id,
        ))
    except Exception:
        logger.exception("Leader ELD fetch failed this run - continuing without it.")

    if not all_drivers:
        logger.warning("No drivers were fetched from any platform this run.")
        return

    try:
        task_index, fallback_index, combined_tasks = asana.build_task_index()
        section_index = asana.build_section_index()
    except Exception:
        logger.exception("Could not read tasks from Asana - skipping this run.")
        return

    if odometer_project_ids_by_dispatch_id:
        _sync_odometer_board(
            asana, odometer_project_ids_by_dispatch_id, logger, section_index,
            factor_session_token=factor_session_token, factor_tenant_id=factor_tenant_id,
            leader_session_token=leader_session_token, leader_tenant_id=leader_tenant_id,
        )

    changed_count = 0
    unchanged_count = 0
    not_found_count = 0
    created_count = 0
    deleted_count = 0

    # Tracks every driver name that definitely has a task by the end of this
    # run (either it already existed, or we just created one) - used below
    # to safely delete old combined co-driver tasks, only once we're sure
    # the split-off driver's own replacement task is really there.
    names_with_a_task = set()

    # Real, currently-active co-driver pairings confirmed this cycle (by
    # task gid) - protects a legitimate combined task from the stale-combined
    # cleanup pass below, which would otherwise see both names in
    # names_with_a_task and delete it.
    combined_gids_in_use = set()

    solo_drivers, combined_units = _group_co_drivers(all_drivers, logger)

    # Names of every solo driver who's currently invisible (Off Platform, or
    # no vehicle assigned) - used below to catch a stale combined task where
    # BOTH former co-drivers have gone invisible at once. The normal cleanup
    # only deletes a stale combined task once one member's own replacement
    # task is confirmed to exist - but if neither member gets a new task
    # (because both are now invisible), that signal never comes, and the
    # old combined task is orphaned forever. Confirmed happening for real:
    # a co-driver pair at A M R TRANSPORT CORP stayed combined in Asana
    # after both were deactivated in Factor ELD.
    invisible_solo_names = {
        normalize_name(d.name) for d in solo_drivers if compute_invisibility_reason(d) is not None
    }

    # Only ask who last edited a logbook for drivers who'll actually show up
    # in Asana - this endpoint is one HTTP call per driver with no bulk
    # equivalent, so doing this for the whole fleet would be far slower than
    # it needs to be (see eld_factor.fetch_staff_editors). Split by platform
    # (Driver.source) since each platform's commits have to be fetched with
    # that platform's own credentials.
    factor_driver_ids = [
        d.driver_id for d in solo_drivers
        if d.driver_id and compute_invisibility_reason(d) is None and d.source == "Factor ELD"
    ]
    leader_driver_ids = [
        d.driver_id for d in solo_drivers
        if d.driver_id and compute_invisibility_reason(d) is None and d.source == "Leader ELD"
    ]
    for unit in combined_units:
        ids = [did for did in unit["member_driver_ids"] if did]
        if unit["source"] == "Leader ELD":
            leader_driver_ids.extend(ids)
        else:
            factor_driver_ids.extend(ids)

    staff_editors_by_id = {}
    try:
        staff_editors_by_id.update(eld_factor.fetch_staff_editors(
            factor_driver_ids, logger, session_token=factor_session_token,
            tenant_id=factor_tenant_id, staff_roster=staff_roster, algo_label=algo_label,
        ))
    except Exception:
        logger.exception(
            "Factor ELD: failed to fetch logbook edit history - continuing "
            "without Staff ID updates this run."
        )

    try:
        staff_editors_by_id.update(eld_leader.fetch_staff_editors(
            leader_driver_ids, logger, session_token=leader_session_token,
            tenant_id=leader_tenant_id, staff_roster=staff_roster, algo_label=algo_label,
        ))
    except Exception:
        logger.exception(
            "Leader ELD: failed to fetch logbook edit history - continuing "
            "without Staff ID updates this run."
        )

    for driver in solo_drivers:
        driver_key = normalize_name(driver.name)
        matches = _lookup_matches(driver.name, task_index, fallback_index)

        invisibility_reason = compute_invisibility_reason(driver)

        if not matches:
            # No existing task for this driver. We only auto-create a task
            # for drivers who are both active AND have a vehicle assigned in
            # Factor ELD - there's no value in cluttering Asana with a task
            # for someone who isn't actually on a truck right now.
            section_info = section_index.get(normalize_company_name(driver.company_name))
            if invisibility_reason is not None:
                not_found_count += 1
                logger.info(
                    "%s: no existing task, and driver %s - not creating a "
                    "new task.",
                    driver.name, invisibility_reason,
                )
            elif section_info is not None:
                try:
                    staff_id, staff_history = _resolve_staff_for_driver(driver, staff_editors_by_id)
                    asana.create_task_for_driver(
                        driver.name, driver.status, driver.vehicle_number,
                        driver.violation, staff_id, staff_history, section_info,
                    )
                    created_count += 1
                    names_with_a_task.add(driver_key)
                    logger.info(
                        "%s: created new task with status %s (%s / %s)",
                        driver.name, driver.status,
                        section_info["project_name"], section_info["section_name"],
                    )
                except Exception:
                    logger.exception(
                        "Failed to create a new Asana task for driver '%s'.", driver.name
                    )
            else:
                not_found_count += 1
                logger.warning(
                    "%s: no matching Asana task found, and no existing section for "
                    "company '%s' to create one in (source: %s, status: %s) - use the "
                    "bot's 'Company Assign' menu to add it to a board.",
                    driver.name, driver.company_name, driver.source, driver.status,
                )
            continue

        if invisibility_reason is not None:
            # Driver has gone inactive, or no longer has a vehicle assigned,
            # and already has a task - delete it entirely rather than just
            # marking it some placeholder status. This does lose whatever
            # else was on the task (Comments, etc.) - a deliberate tradeoff
            # you've confirmed for the Off Platform case, and the same
            # logic applies here.
            for match in matches:
                try:
                    asana.delete_task(match["task_gid"])
                    deleted_count += 1
                    logger.info(
                        "%s: deleted task - driver %s (%s)",
                        driver.name, invisibility_reason, match["project_name"],
                    )
                except Exception:
                    logger.exception(
                        "Failed to delete task for driver '%s'.", driver.name
                    )
            continue

        names_with_a_task.add(driver_key)
        for match in matches:
            correct_section_info = section_index.get(normalize_company_name(driver.company_name))
            if (
                correct_section_info is not None
                and match.get("current_section_gid")
                and correct_section_info["section_gid"] != match["current_section_gid"]
            ):
                try:
                    asana.move_task_to_section(match["task_gid"], match["project_id"], correct_section_info)
                    changed_count += 1
                    logger.info(
                        "%s: moved from '%s' to '%s' (company changed in Factor ELD)",
                        driver.name, match.get("current_section_name"),
                        correct_section_info["section_name"],
                    )
                except Exception:
                    logger.exception(
                        "Failed to move task for driver '%s' to the correct company "
                        "section.", driver.name,
                    )

            staff_id, staff_history = _resolve_staff_for_driver(driver, staff_editors_by_id)

            old_status = match["current_status"] or "(blank)"
            vehicle_changed = bool(match.get("vehicle_field_gid")) and (
                vehicle_field_value(match.get("vehicle_field_type"), driver.vehicle_number)
                != match.get("current_vehicle_number")
            )
            violation_changed = bool(match.get("violation_field_gid")) and (
                driver.violation != match.get("current_violation")
            )
            staff_changed = (
                bool(match.get("staff_id_field_gid")) and staff_id is not None
                and staff_id != match.get("current_staff_id")
            ) or (
                bool(match.get("staff_history_field_gid")) and staff_history is not None
                and staff_history != match.get("current_staff_history")
            )
            if (
                old_status == driver.status and not vehicle_changed
                and not violation_changed and not staff_changed
            ):
                unchanged_count += 1
                continue

            try:
                success = asana.update_task_status(
                    match, driver.status, driver.vehicle_number, driver.violation,
                    staff_id, staff_history,
                )
            except Exception:
                logger.exception(
                    "Failed to update task for driver '%s' - skipping it this run.",
                    driver.name,
                )
                continue
            if old_status != driver.status and success:
                changed_count += 1
                logger.info(
                    "%s: %s -> %s (%s)",
                    driver.name, old_status, driver.status, match["project_name"],
                )
            if violation_changed:
                changed_count += 1
                if driver.violation:
                    logger.info(
                        "%s: violation set to %s (%s)",
                        driver.name, driver.violation, match["project_name"],
                    )
                else:
                    logger.info(
                        "%s: violation cleared (%s)", driver.name, match["project_name"],
                    )
            elif vehicle_changed:
                changed_count += 1
                logger.info(
                    "%s: vehicle number updated to %s (%s)",
                    driver.name, driver.vehicle_number, match["project_name"],
                )
            elif staff_changed:
                changed_count += 1
                logger.info(
                    "%s: last logbook edit attributed to %s (%s)",
                    driver.name, staff_history or staff_id, match["project_name"],
                )

    # --- co-driver pairs (two drivers confirmed sharing one vehicle) ---
    combined_task_index = {}
    for combined in combined_tasks:
        if len(combined["names"]) == 2:
            pair_key = tuple(sorted(normalize_name(n) for n in combined["names"]))
            combined_task_index[pair_key] = combined

    for unit in combined_units:
        pair_key = tuple(sorted(normalize_name(n) for n in unit["member_names"]))
        match = combined_task_index.get(pair_key)

        if match is not None:
            combined_gids_in_use.add(match["task_gid"])
            for name in unit["member_names"]:
                names_with_a_task.add(normalize_name(name))

            correct_section_info = section_index.get(normalize_company_name(unit["company_name"]))
            if (
                correct_section_info is not None
                and match.get("current_section_gid")
                and correct_section_info["section_gid"] != match["current_section_gid"]
            ):
                try:
                    asana.move_task_to_section(match["task_gid"], match["project_id"], correct_section_info)
                    changed_count += 1
                    logger.info(
                        "%s: moved from '%s' to '%s' (company changed in Factor ELD) "
                        "[co-driver task]",
                        unit["name"], match.get("current_section_name"),
                        correct_section_info["section_name"],
                    )
                except Exception:
                    logger.exception(
                        "Failed to move combined co-driver task '%s' to the correct "
                        "company section.", unit["name"],
                    )

            staff_id, staff_history = _resolve_staff_for_combined_unit(unit, staff_editors_by_id, logger)

            old_status = match["current_status"] or "(blank)"
            vehicle_changed = bool(match.get("vehicle_field_gid")) and (
                vehicle_field_value(match.get("vehicle_field_type"), unit["vehicle_number"])
                != match.get("current_vehicle_number")
            )
            violation_changed = bool(match.get("violation_field_gid")) and (
                unit["violation"] != match.get("current_violation")
            )
            staff_changed = (
                bool(match.get("staff_id_field_gid")) and staff_id is not None
                and staff_id != match.get("current_staff_id")
            ) or (
                bool(match.get("staff_history_field_gid")) and staff_history is not None
                and staff_history != match.get("current_staff_history")
            )
            if (
                old_status == unit["status"] and not vehicle_changed
                and not violation_changed and not staff_changed
            ):
                unchanged_count += 1
                continue

            try:
                success = asana.update_task_status(
                    match, unit["status"], unit["vehicle_number"], unit["violation"],
                    staff_id, staff_history,
                )
            except Exception:
                logger.exception(
                    "Failed to update combined co-driver task '%s'.", unit["name"]
                )
                continue
            if old_status != unit["status"] and success:
                changed_count += 1
                logger.info(
                    "%s: %s -> %s (%s) [co-driver task]",
                    unit["name"], old_status, unit["status"], match["project_name"],
                )
            if violation_changed:
                changed_count += 1
                if unit["violation"]:
                    logger.info(
                        "%s: violation set to %s (%s) [co-driver task]",
                        unit["name"], unit["violation"], match["project_name"],
                    )
                else:
                    logger.info(
                        "%s: violation cleared (%s) [co-driver task]",
                        unit["name"], match["project_name"],
                    )
            elif vehicle_changed:
                changed_count += 1
                logger.info(
                    "%s: vehicle number updated to %s (%s) [co-driver task]",
                    unit["name"], unit["vehicle_number"], match["project_name"],
                )
            elif staff_changed:
                changed_count += 1
                logger.info(
                    "%s: last logbook edit attributed to %s (%s) [co-driver task]",
                    unit["name"], staff_history or staff_id, match["project_name"],
                )
            continue

        # No existing combined task for this exact pair yet.
        section_info = section_index.get(normalize_company_name(unit["company_name"]))
        if section_info is None:
            not_found_count += 1
            logger.warning(
                "%s: co-driver pair has no matching combined task, and no "
                "existing section for company '%s' to create one in "
                "(source: %s, status: %s)",
                unit["name"], unit["company_name"], unit["source"], unit["status"],
            )
            continue

        staff_id, staff_history = _resolve_staff_for_combined_unit(unit, staff_editors_by_id, logger)
        try:
            new_gid = asana.create_task_for_driver(
                unit["name"], unit["status"], unit["vehicle_number"],
                unit["violation"], staff_id, staff_history, section_info,
            )
        except Exception:
            logger.exception(
                "Failed to create a new combined co-driver task for '%s'.", unit["name"]
            )
            continue

        combined_gids_in_use.add(new_gid)
        created_count += 1
        for name in unit["member_names"]:
            names_with_a_task.add(normalize_name(name))
        logger.info(
            "%s: created new combined co-driver task with status %s (%s / %s)",
            unit["name"], unit["status"],
            section_info["project_name"], section_info["section_name"],
        )

        # Either co-driver may still have their own individual task left
        # over from before they were paired together - that's now redundant
        # since they share the task we just created.
        for name in unit["member_names"]:
            for individual_match in _lookup_matches(name, task_index, fallback_index):
                try:
                    asana.delete_task(individual_match["task_gid"])
                    deleted_count += 1
                    logger.info(
                        "%s: deleted old individual task - now sharing a combined "
                        "co-driver task with '%s' (%s)",
                        name, unit["name"], individual_match["project_name"],
                    )
                except Exception:
                    logger.exception(
                        "Failed to delete old individual task for '%s' after "
                        "pairing with a co-driver.", name
                    )

    # Now that every driver has been handled (and any needed replacement
    # tasks created), it's safe to clean up old/stale combined co-driver
    # tasks. Delete one once EITHER:
    #   - at least one of its named drivers is confirmed to have their own
    #     separate task elsewhere (the normal "split up" case), or
    #   - every one of its named drivers is currently invisible (both co-
    #     drivers deactivated at once - see invisible_solo_names above;
    #     without this, the old combined task would never get cleaned up,
    #     since neither member ever gets a replacement task to signal it).
    # Skipped either way if it isn't a pairing we just confirmed/created as
    # still genuinely active above.
    for combined in combined_tasks:
        if combined["task_gid"] in combined_gids_in_use:
            continue
        split_name = next(
            (n for n in combined["names"] if normalize_name(n) in names_with_a_task),
            None,
        )
        all_invisible = all(normalize_name(n) in invisible_solo_names for n in combined["names"])
        if split_name is None and not all_invisible:
            continue
        reason = (
            f"'{split_name}' now has their own separate task"
            if split_name is not None
            else "both co-drivers are now inactive"
        )
        try:
            asana.delete_task(combined["task_gid"])
            deleted_count += 1
            logger.info(
                "Deleted old combined task '%s' (%s) - %s.",
                combined["task_title"], combined["project_name"], reason,
            )
        except Exception:
            logger.exception(
                "Failed to delete old combined task '%s'.", combined["task_title"]
            )

    logger.info(
        "Sync run complete: %s changed, %s unchanged, %s created, %s deleted, "
        "%s driver(s) with no matching task/section.",
        changed_count, unchanged_count, created_count, deleted_count, not_found_count,
    )


DATABASE_SYNC_INTERVAL_SECONDS = 1 * 60 * 60


def run_database_cycle(
    asana, database_project_id, control=None, token_state=None,
    factor_session_token=None, factor_tenant_id=None,
    leader_session_token=None, leader_tenant_id=None,
):
    """Sync the standalone 'Database' board: every driver (active AND
    inactive) from BOTH Factor ELD and Leader ELD (confirmed this board is
    shared/common across both platforms, not a separate one per platform),
    grouped into per-company sections (auto-created the first time a
    company is seen). Only ever creates or updates - never deletes, even
    once a driver goes inactive (a deliberate difference from the dispatch
    boards in run_one_cycle, which do delete - confirmed this board is
    meant to be a permanent record instead). token_state/*_token/*_tenant_id
    let a caller (multi_sync.py) pass one team's own state/credentials
    explicitly - main() below omits them, unchanged single-team behavior."""
    records = []
    try:
        records.extend(eld_factor.fetch_driver_database_records(
            logger, session_token=factor_session_token, tenant_id=factor_tenant_id,
        ))
        _mark_factor_fetch_ok(token_state)
    except Exception as exc:
        logger.exception("Factor ELD: driver database fetch failed this run.")
        _handle_factor_fetch_failure(exc, control, token_state)

    try:
        records.extend(eld_leader.fetch_driver_database_records(
            logger, session_token=leader_session_token, tenant_id=leader_tenant_id,
        ))
    except Exception:
        logger.exception("Leader ELD: driver database fetch failed this run.")

    if not records:
        logger.warning("Database board: no driver records fetched - skipping this run.")
        return

    try:
        index = asana.build_database_task_index(database_project_id)
    except Exception:
        logger.exception("Database board: could not read existing tasks from Asana - skipping this run.")
        return

    created_count = 0
    updated_count = 0
    unchanged_count = 0

    for record in records:
        key = (normalize_company_name(record.company_name or ""), normalize_name(record.name))
        existing = index.get(key)
        if existing is None:
            try:
                new_task_gid = asana.create_database_task(database_project_id, record)
                created_count += 1
                logger.info(
                    "Database board: created task for '%s' (%s).",
                    record.name, record.company_name or "Unknown Company",
                )
                # Record this key as now having a task immediately - Factor
                # ELD/Leader ELD can list the exact same (company, name)
                # more than once in a single fetch (e.g. the same physical
                # driver under two different driver_ids - confirmed
                # happening for real). Without this, a second record with
                # the same key later in this same loop would find nothing
                # in the index (built once, before this loop started) and
                # create a duplicate task instead of recognizing the one
                # just created.
                index[key] = {
                    "task_gid": new_task_gid, "task_title": record.name, "current": {},
                }
            except Exception:
                logger.exception(
                    "Database board: failed to create task for '%s' (%s).",
                    record.name, record.company_name,
                )
            continue

        try:
            changed = asana.update_database_task(database_project_id, existing, record)
        except Exception:
            logger.exception("Database board: failed to update task for '%s'.", record.name)
            continue
        if changed:
            updated_count += 1
        else:
            unchanged_count += 1

    logger.info(
        "Database board sync complete: %s created, %s updated, %s unchanged "
        "(never deletes).",
        created_count, updated_count, unchanged_count,
    )


def main():
    parser = argparse.ArgumentParser(description="Sync ELD driver duty status into Asana.")
    parser.add_argument(
        "--once", action="store_true",
        help="Run a single sync cycle and exit, instead of looping forever.",
    )
    args = parser.parse_args()

    asana_token = os.environ.get("ASANA_TOKEN", "")
    project_ids = [
        p.strip() for p in os.environ.get("ASANA_PROJECT_IDS", "").split(",") if p.strip()
    ]
    if not asana_token or not project_ids:
        logger.error("ASANA_TOKEN and ASANA_PROJECT_IDS must be set in .env - stopping.")
        return

    asana = asana_client.AsanaClient(asana_token, project_ids, logger)

    database_project_id = os.environ.get("ASANA_DATABASE_PROJECT_ID", "").strip()
    if not database_project_id:
        logger.info("ASANA_DATABASE_PROJECT_ID not set - Database board sync is disabled.")

    # Odometer Jump board config supports two shapes:
    #   - ASANA_ODOMETER_PROJECT_IDS (plural, comma list, positionally
    #     matched to ASANA_PROJECT_IDS) - one Odometer Jump project per
    #     dispatch board, for a team that wants them kept separate.
    #   - ASANA_ODOMETER_PROJECT_ID (singular) - one shared Odometer Jump
    #     project for every dispatch board (this team's current setup).
    #     Every dispatch board's project_id is mapped to this same single
    #     odometer project_id, so _sync_odometer_board's per-project
    #     grouping just naturally merges every company's issues into that
    #     one project - no separate single-project code path needed.
    odometer_project_ids = [
        p.strip() for p in os.environ.get("ASANA_ODOMETER_PROJECT_IDS", "").split(",") if p.strip()
    ]
    single_odometer_project_id = os.environ.get("ASANA_ODOMETER_PROJECT_ID", "").strip()

    if odometer_project_ids:
        if len(odometer_project_ids) != len(project_ids):
            logger.error(
                "ASANA_ODOMETER_PROJECT_IDS has %s entries but ASANA_PROJECT_IDS has %s - "
                "they must be the same length and in the same order. Odometer Jump board "
                "sync is disabled until this is fixed.",
                len(odometer_project_ids), len(project_ids),
            )
            odometer_project_ids_by_dispatch_id = None
        else:
            odometer_project_ids_by_dispatch_id = dict(zip(project_ids, odometer_project_ids))
    elif single_odometer_project_id:
        odometer_project_ids_by_dispatch_id = {pid: single_odometer_project_id for pid in project_ids}
    else:
        logger.info(
            "Neither ASANA_ODOMETER_PROJECT_IDS nor ASANA_ODOMETER_PROJECT_ID is set - "
            "Odometer Jump board sync is disabled."
        )
        odometer_project_ids_by_dispatch_id = None

    if args.once:
        logger.info("Running a single sync cycle (--once)...")
        # This standalone path only ever serves team "original" (Texas)
        # locally - its own commit label is passed explicitly here rather
        # than relying on a shared fallback inside eld_factor.py, which
        # would incorrectly apply Texas's label to every other team's
        # sync (see eld_factor.ALGO_SERVICE_ACCOUNT_LABEL's docstring).
        run_one_cycle(
            asana, odometer_project_ids_by_dispatch_id=odometer_project_ids_by_dispatch_id,
            algo_label=eld_factor.ALGO_SERVICE_ACCOUNT_LABEL,
        )
        if database_project_id:
            run_database_cycle(asana, database_project_id)
        return

    # Loop mode: set up Telegram alerting/control, then sync forever on a
    # timer. Two mutually exclusive modes:
    #   - CONTROL_MODE=notifier (the multi-tenant control panel setup - see
    #     the plan): this process is one team's isolated instance sharing a
    #     bot token with a separate, singleton control_bot process that owns
    #     all interactive commands (/pause, /resume, /settoken). This
    #     process only ever SENDS alerts and reads a paused.flag file - it
    #     never polls Telegram itself, since two processes long-polling the
    #     same bot token would race each other for updates.
    #   - anything else (the default, today's single-tenant/local setup):
    #     this process itself owns the bot token and handles interactive
    #     commands directly via telegram_control.TelegramControl, unchanged.
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    control = None
    if os.environ.get("CONTROL_MODE", "").strip().lower() == "notifier":
        chat_ids_raw = os.environ.get("TEAM_CHAT_IDS", "")
        chat_ids = [int(x.strip()) for x in chat_ids_raw.split(",") if x.strip()]
        control = telegram_notifier.TelegramNotifier(bot_token, chat_ids, logger)
        logger.info("Telegram notifier mode - alerts only, no interactive polling here.")
    elif bot_token:
        allowed_ids_raw = os.environ.get("ALLOWED_TELEGRAM_IDS", "")
        # An empty ALLOWED_TELEGRAM_IDS means "no allowlist" (open to every
        # Telegram account) - see telegram_control.py's SECURITY NOTE. That's
        # a deliberate choice, not a default: only leave it empty if you
        # mean for anyone to be able to control the sync.
        allowed_ids = [int(x.strip()) for x in allowed_ids_raw.split(",") if x.strip()]
        control = telegram_control.TelegramControl(bot_token, allowed_ids, logger)
        control.start()
    else:
        logger.info("Telegram control not configured - running without pause/resume control.")

    interval_minutes = float(os.environ.get("POLL_INTERVAL_MINUTES", "5"))
    logger.info("Starting sync loop - running every %s minute(s). Press Ctrl+C to stop.", interval_minutes)
    if database_project_id:
        logger.info(
            "Database board sync is enabled - running every %s hour(s), "
            "independent of the main dispatch sync above.",
            DATABASE_SYNC_INTERVAL_SECONDS / 3600,
        )

    # 0 forces the Database board to sync on the very first loop iteration,
    # rather than waiting a full 12 hours after startup.
    last_database_sync = 0

    while True:
        _check_token_expiry_warning(control)
        if control is not None and control.is_paused():
            logger.info("Sync is paused (via Telegram) - skipping this cycle.")
        else:
            # Same reasoning as the --once path above re: algo_label.
            run_one_cycle(
                asana, control, odometer_project_ids_by_dispatch_id,
                algo_label=eld_factor.ALGO_SERVICE_ACCOUNT_LABEL,
            )
            if database_project_id and (time.time() - last_database_sync) >= DATABASE_SYNC_INTERVAL_SECONDS:
                run_database_cycle(asana, database_project_id, control)
                last_database_sync = time.time()

        # Sleep in short chunks (rather than one long sleep) so a /pause or
        # /resume command can take effect without waiting for the whole
        # interval to finish - re-checking is_paused() every chunk (rather
        # than just once at the top of the outer loop) is what actually
        # makes that true, since a paused.flag file (see telegram_notifier.py,
        # the multi-tenant control panel's per-team pause mechanism) can
        # change between chunks with nothing else here to notice it.
        remaining_seconds = interval_minutes * 60
        while remaining_seconds > 0:
            if control is not None and control.is_paused():
                break
            chunk = min(30, remaining_seconds)
            time.sleep(chunk)
            remaining_seconds -= chunk


if __name__ == "__main__":
    main()
