"""
eld_leader.py

Leader ELD integration. Confirmed directly (DevTools capture of its own
driver-profile and duty-status endpoints): Leader ELD is a different
TENANT on the exact same backend Factor ELD uses (same host
api.drivehos.app, same endpoints, same field names, same status/error
codes) - only the session token and Tenant_id differ. Because of that,
the Database board and Odometer Jump board fetches below don't duplicate
eld_factor.py's logic - they just call its same functions with Leader
ELD's own credentials.

fetch_drivers() feeds the SAME per-company dispatch boards Factor ELD uses
(Maxmud Test A/B/C, confirmed you want Leader ELD's companies/drivers
mixed directly into those rather than a separate dedicated project) - it's
just eld_factor.fetch_drivers() called with Leader ELD's own credentials
and no FACTOR_COMPANY_FILTER applied (Leader ELD always syncs every
company it has).
"""

import os

import eld_factor


def fetch_drivers(logger, session_token=None, tenant_id=None):
    """Return a list of eld_common.Driver records from Leader ELD, for the
    same per-company dispatch boards Factor ELD uses (Maxmud Test A/B/C).

    session_token/tenant_id default to this process's own .env credentials
    (LEADER_SESSION_TOKEN/LEADER_TENANT_ID), but can be overridden - this is
    how a caller with its own per-team config (e.g. control_bot validating a
    team's pasted token before any .env exists for them) supplies Leader ELD
    credentials directly instead of relying on os.environ."""
    session_token, tenant_id = _leader_credentials(logger, session_token, tenant_id)
    if session_token is None:
        return []
    return eld_factor.fetch_drivers(
        logger, session_token, tenant_id, platform_label="Leader ELD", apply_company_filter=False,
    )


def _leader_credentials(logger, session_token=None, tenant_id=None):
    session_token = session_token or os.environ.get("LEADER_SESSION_TOKEN", "")
    tenant_id = tenant_id or os.environ.get("LEADER_TENANT_ID", "")
    if not session_token or not tenant_id:
        logger.error(
            "LEADER_SESSION_TOKEN or LEADER_TENANT_ID is missing from .env - "
            "skipping this Leader ELD fetch."
        )
        return None, None
    return session_token, tenant_id


def fetch_driver_database_records(logger, session_token=None, tenant_id=None):
    """Return every Leader ELD driver (active AND inactive) as
    DriverDatabaseRecord objects, for the shared 'Database' Asana board -
    the same board Factor ELD feeds, confirmed you want this common rather
    than a separate Leader-only board.

    session_token/tenant_id default to this process's own .env credentials,
    but can be overridden - see fetch_drivers() above."""
    session_token, tenant_id = _leader_credentials(logger, session_token, tenant_id)
    if session_token is None:
        return []
    return eld_factor.fetch_driver_database_records(logger, session_token, tenant_id, platform_label="Leader ELD")


def fetch_odometer_issues(logger, session_token=None, tenant_id=None):
    """Return {driver_id: {...}} for every Leader ELD driver with an
    active odometer problem, for the shared 'Odometer Jump' Asana board -
    same board Factor ELD feeds, same reasoning as fetch_driver_database_
    records above.

    session_token/tenant_id default to this process's own .env credentials,
    but can be overridden - see fetch_drivers() above."""
    session_token, tenant_id = _leader_credentials(logger, session_token, tenant_id)
    if session_token is None:
        return {}
    return eld_factor.fetch_odometer_issues(logger, session_token, tenant_id, platform_label="Leader ELD")


def fetch_staff_editors(driver_ids, logger, session_token=None, tenant_id=None, staff_roster=None, algo_label=None):
    """Return {driver_id: (staff_id, display_name, created_at) or None} for
    who most recently edited each Leader ELD driver's logbook - same
    Staff ID History feature Factor ELD has, same commits endpoint (same
    backend, confirmed).

    session_token/tenant_id default to this process's own .env credentials,
    but can be overridden - see fetch_drivers() above. staff_roster/
    algo_label default to eld_factor.py's own STAFF_ID_BY_FIRST_NAME/
    ALGO_SERVICE_ACCOUNT_LABEL, but can be overridden per-team - a shared
    roster would otherwise misattribute editors across different teams'
    overlapping first names."""
    session_token, tenant_id = _leader_credentials(logger, session_token, tenant_id)
    if session_token is None:
        return {}
    return eld_factor.fetch_staff_editors(
        driver_ids, logger, session_token, tenant_id, platform_label="Leader ELD",
        staff_roster=staff_roster, algo_label=algo_label,
    )
