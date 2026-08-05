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
    """Returns (True, message) or (False, message)."""
    ok, result = _check_with_retries(lambda: eld_factor.fetch_drivers(
        _logger, session_token=session_token, tenant_id=tenant_id, apply_company_filter=False,
    ))
    return (True, f"{len(result)} driver(s) visible") if ok else (False, result)


def check_leader(session_token, tenant_id):
    ok, result = _check_with_retries(
        lambda: eld_leader.fetch_drivers(_logger, session_token=session_token, tenant_id=tenant_id)
    )
    return (True, f"{len(result)} driver(s) visible") if ok else (False, result)


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


def workspace_info(token, workspace_gid):
    return asana_client.AsanaClient(token, [], _logger).get_workspace_info(workspace_gid)


def organization_teams(token, workspace_gid):
    return asana_client.AsanaClient(token, [], _logger).get_organization_teams(workspace_gid)
