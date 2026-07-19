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

import asana_client
import eld_factor
import eld_leader

_logger = logging.getLogger("control_bot.validators")


def check_factor(session_token, tenant_id):
    """Returns (True, message) or (False, message)."""
    try:
        drivers = eld_factor.fetch_drivers(
            _logger, session_token=session_token, tenant_id=tenant_id, apply_company_filter=False,
        )
    except Exception as exc:
        return False, str(exc)
    return True, f"{len(drivers)} driver(s) visible"


def check_leader(session_token, tenant_id):
    try:
        drivers = eld_leader.fetch_drivers(_logger, session_token=session_token, tenant_id=tenant_id)
    except Exception as exc:
        return False, str(exc)
    return True, f"{len(drivers)} driver(s) visible"


def check_asana(token):
    """Returns (True, [{"gid", "name"}, ...]) - the token's own workspaces -
    or (False, message) on failure."""
    client = asana_client.AsanaClient(token, [], _logger)
    try:
        me = client.get_current_user()
    except Exception as exc:
        return False, str(exc)
    return True, me.get("workspaces", [])


def workspace_info(token, workspace_gid):
    return asana_client.AsanaClient(token, [], _logger).get_workspace_info(workspace_gid)


def organization_teams(token, workspace_gid):
    return asana_client.AsanaClient(token, [], _logger).get_organization_teams(workspace_gid)
