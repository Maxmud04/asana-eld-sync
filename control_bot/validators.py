"""
control_bot/validators.py

Live-checks a team's pasted credentials against the real Factor ELD /
Leader ELD / Asana backends before onboarding creates anything, turning a
bad token into a friendly chat reply instead of a stack trace. Calls
directly into eld_factor.py/eld_leader.py/asana_client.py's own
already-parameterized functions (see the plan's Phase 1) - no duplicated
HTTP logic here.

A bad/expired token surfaces as a RuntimeError from eld_factor.py's own
_request_with_retries on a 401 (see its "rejected the request as
unauthorized" message) - this propagates up through fetch_drivers()
because a brand-new team has no company filter yet, so fetch_drivers takes
the _discover_companies() path, which is NOT wrapped in a try/except the
way the per-company and violation fetches are. That's what check_factor/
check_leader below rely on to turn a bad token into an exception instead of
a silent empty result.
"""

import logging
import time

import asana_client
import eld_factor
import eld_leader

_logger = logging.getLogger("control_bot.validators")

# How many times to retry a validation check that fails with a transient
# error (403/429/5xx - see _is_retryable_validation_error) before actually
# telling the user their token/tenant_id is bad. Confirmed live (2026-08-03):
# a brand-new token/tenant_id pair has no cached company list to fall back
# on (unlike eld_factor.py's own _discover_companies cache, which only
# helps once a tenant has been seen before) - so a validation check run
# during a transient rate-limit storm on the ELD backend would otherwise
# reject a perfectly valid token as "invalid", right when a team most needs
# rotation to work (their old token just died). A real bad/expired token
# still fails immediately below - see _is_retryable_validation_error.
_VALIDATION_RETRIES = 3
_VALIDATION_RETRY_DELAY_SECONDS = 5


def _is_retryable_validation_error(exc):
    """True for a transient backend hiccup (403/429/5xx after
    eld_factor.py's own 3 internal retries already failed) worth retrying
    here too. False for a genuinely bad/expired token - eld_factor.py
    raises that as a plain RuntimeError with its own distinct message (see
    this module's docstring), never retried internally, and shouldn't be
    retried here either - retrying a truly bad token just wastes time
    before giving the same correct "invalid" answer."""
    return not isinstance(exc, RuntimeError)


def _check_with_retries(fetch_fn):
    last_exc = None
    for attempt in range(1, _VALIDATION_RETRIES + 1):
        try:
            return True, fetch_fn()
        except Exception as exc:
            last_exc = exc
            if not _is_retryable_validation_error(exc) or attempt == _VALIDATION_RETRIES:
                return False, str(exc)
            _logger.warning(
                "Validation check hit a transient error (attempt %s/%s) - "
                "retrying in %ss before concluding the token is actually "
                "bad: %s",
                attempt, _VALIDATION_RETRIES, _VALIDATION_RETRY_DELAY_SECONDS, exc,
            )
            time.sleep(_VALIDATION_RETRY_DELAY_SECONDS)
    return False, str(last_exc)  # unreachable, satisfies linters


def check_factor(session_token, tenant_id):
    """Returns (True, message) or (False, message). Uses check_credentials'
    single-request check (2026-09-22) rather than a real fetch_drivers()
    call - a full fetch means discovering every company (up to ~22 pages
    for a large tenant) plus one more request per company, all serialized
    behind live production traffic on the same process-wide rate-limit
    lock (see eld_scheduler.default_scheduler) - confirmed this made
    onboarding a brand-new team feel "stuck" for minutes at a time."""
    ok, result = _check_with_retries(lambda: eld_factor.check_credentials(
        _logger, session_token, tenant_id,
    ))
    return (True, f"{result} driver(s) visible") if ok else (False, result)


def check_leader(session_token, tenant_id):
    ok, result = _check_with_retries(
        lambda: eld_leader.check_credentials(_logger, session_token, tenant_id)
    )
    return (True, f"{result} driver(s) visible") if ok else (False, result)


def check_asana(token):
    """Returns (True, [{"gid", "name"}, ...]) - the token's own workspaces -
    or (False, message) on failure."""
    client = asana_client.AsanaClient(token, [], _logger)
    try:
        me = client.get_current_user()
    except Exception as exc:
        return False, str(exc)
    return True, me.get("workspaces", [])


def check_asana_project(token, project_id):
    """Returns (True, project_name) or (False, message) - confirms a pasted
    existing-Database-board link/id (see onboarding.py's
    _handle_database_board) actually resolves, before saving it as the
    team's permanent driver-history board."""
    try:
        name = asana_client.AsanaClient(token, [], _logger).get_project_name(project_id)
    except Exception as exc:
        return False, str(exc)
    return True, name


def ensure_database_project_ready(token, workspace_gid, project_id):
    """A pasted existing Database board link resolving (check_asana_project)
    only proves it's a real, accessible project - not that it has any field
    run_database_cycle can actually write into. Confirmed live (2026-09-26,
    onboarding LEADER D): a genuinely blank placeholder project (zero
    custom fields at all) passed onboarding fine, then every single sync
    cycle afterward silently failed to read it forever (caught by
    run_database_cycle's own try/except, logged and skipped - no one would
    ever notice short of manually checking). If _get_database_project_config
    finds NONE of the 8 standard fields, auto-attach them (exactly what
    bootstrap_database_project puts on a brand-new board) rather than
    rejecting the board outright - a team's already-real, differently-
    shaped Database board (e.g. Central B's combined User/Pass column)
    still resolves normally and is left untouched here. Returns True if any
    fields were just attached (worth telling the admin about), False if the
    board already had usable fields."""
    client = asana_client.AsanaClient(token, [], _logger)
    try:
        client._get_database_project_config(project_id)
        return False  # already has at least one usable field - untouched
    except RuntimeError:
        pass
    for field_name in ["Co-driver", "Vehicle Id", "Email", "Phone Number", "CDL", "State", "Login", "Password"]:
        field_gid = client.create_text_custom_field(workspace_gid, field_name)
        client.attach_custom_field(project_id, field_gid)
    return True


def workspace_info(token, workspace_gid):
    return asana_client.AsanaClient(token, [], _logger).get_workspace_info(workspace_gid)


def organization_teams(token, workspace_gid):
    return asana_client.AsanaClient(token, [], _logger).get_organization_teams(workspace_gid)
