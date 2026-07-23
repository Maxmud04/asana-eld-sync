"""
eld_factor.py

Fetches the current duty status of every driver from Factor ELD and turns
each one into a standard eld_common.Driver record.

Factor ELD doesn't publish official public API documentation, so this file
talks to the same internal endpoint their own web dashboard uses (found by
watching the dashboard's network traffic in a browser). It's a real, working
endpoint, but Factor ELD could change its shape without warning - if the
sync suddenly starts failing on Factor ELD, this is the file to check first.

AUTHENTICATION NOTE (important, temporary limitation):
Factor ELD's dashboard logs in with an email/password and gets back a
temporary access token. We have NOT yet captured the login request itself,
so this file cannot log itself back in automatically yet - it simply uses
the session token you paste into FACTOR_SESSION_TOKEN in your .env file.
That token's actual lifetime is decided entirely by Factor ELD's own login
flow at the moment you obtained it - confirmed anywhere from under a day to
about 30 days depending on how it was issued (decode the token's own "exp"
claim, see sync.py's _decode_token_expiry, rather than assuming a fixed
number) - and needs to be manually replaced once it expires (from a browser
DevTools capture, the same way we found it) until we add real auto-login
here.
"""

import os
import random
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests

from eld_common import Driver, DriverDatabaseRecord, map_status

# How many companies to fetch from Factor ELD at the same time. Each
# company's data is independent, so fetching several in parallel cuts the
# total wait a lot (117 companies one-at-a-time took about 2 minutes;
# fetching 10 at a time takes more like 15-20 seconds).
MAX_PARALLEL_COMPANY_FETCHES = 10

# The "system-list" endpoint returns every company at once, but with 2000+
# drivers spread across ~22 pages of live, constantly-changing data, drivers
# can shift between pages while we're still fetching earlier ones - causing
# us to occasionally miss someone (this is exactly what happened with
# Aleyna Cansiz). When we're only watching ONE company, the "list" endpoint
# (scoped with a Company_id header) returns that company's much smaller
# driver set in a single page, avoiding the problem entirely.
SYSTEM_LIST_API_BASE = "https://api.drivehos.app/api/v1/hos/system-list"
SINGLE_COMPANY_API_BASE = "https://api.drivehos.app/api/v1/hos/list"
# Returns every company's currently unresolved HOS violations in one call
# (found by watching Factor ELD's own "Violations" dashboard page in
# DevTools, the same way we found the other endpoints here).
SYSTEM_VIOLATIONS_API_BASE = "https://api.drivehos.app/api/v1/hos/system-violations"
# Returns every company's currently logged odometer/engine errors in one
# call when "company_ids" is omitted (confirmed directly - unlike the
# per-company /hos/list endpoint). Mixes odometer errors together with
# unrelated ones (e.g. "ENGINE_POWER_UP_SEQUENCE") - see
# FACTOR_ODOMETER_ERROR_MAP for which error_type values we actually care
# about and translate; everything else is ignored, never guessed at.
SYSTEM_ERRORS_WARNINGS_API_BASE = "https://api.drivehos.app/api/v1/hos/system-errors-warnings"
# Returns one driver's logbook edit history (who changed what, and when).
# Unlike the endpoints above, this is scoped to a single driver_id - there's
# no bulk "every driver at once" version, so we only call this for drivers
# who actually have an Asana task (see sync.py), not the whole fleet.
COMMITS_API_BASE = "https://api.drivehos.app/api/v1/commits"
# Returns full driver profile/contact info (name, email, phone, CDL, license
# state, login username, assigned vehicle number, co-driver, etc.) - used
# only for the standalone "Database" Asana board (see fetch_driver_database_
# records), not the dispatch boards above. Confirmed via DevTools: unlike
# the endpoints above, there's no single "all" status value - "active" and
# "inactive" drivers have to be fetched as two separate calls.
DRIVERS_LIST_API_BASE = "https://api.drivehos.app/api/v1/drivers"
DRIVERS_LIST_STATUSES = ("active", "inactive")
# Returns roadside-inspector logbook transfer history ("Compliance > HOS
# Audit Transfer" in the dashboard - a DOT inspector "transferring"/auditing
# a driver's e-logs). Confirmed via DevTools (2026-07-23): scoped by the
# same Company_id header the per-company driver list uses, paginated with
# page/limit, returning {"data": {"logs": [{"id", "driver_name",
# "company_name", "start_date", "end_date", "file_status", "comment", ...
# "created_at"}, ...]}}. Only fetched one page at a time (see
# fetch_fmcsa_transfers) since we only care about detecting brand-new
# transfers, not the full historical log.
FMCSA_API_BASE = "https://api.drivehos.app/api/v1/fmcsa/company"
FMCSA_PAGE_SIZE = 20
PAGE_SIZE = 100  # how many drivers to ask for per page
MAX_RETRIES = 3
RETRY_DELAY_SECONDS = 2
MAX_PARALLEL_COMMIT_FETCHES = 10

# Company names permanently excluded from every board (dispatch, Database,
# Odometer Jump), on both Factor ELD and Leader ELD - confirmed these are
# test/training accounts, not real companies, so they should never be
# synced anywhere again, even if either platform keeps reporting drivers
# under them.
EXCLUDED_COMPANY_NAMES = {"training company"}


def _is_excluded_company(company_name):
    return (company_name or "").strip().lower() in EXCLUDED_COMPANY_NAMES


# Factor ELD's own status codes, translated into our standard statuses.
# Anything not listed here automatically becomes "Unknown" (see
# eld_common.map_status) instead of guessing - add more entries here as we
# see more real codes come through.
FACTOR_STATUS_MAP = {
    "DS_D": "Driving",
    "DS_SB": "Sleeping",
    "DS_OFF": "Off Duty",
    "DS_ON": "On Duty",
}

# Factor ELD's own violation_type codes, translated into the four options
# Asana's "Woring Vilation" dropdown actually has. Only codes confirmed
# against live data are listed here - see _resolve_violation below for what
# happens with anything else (never guessed, always logged). Note "NO_PTI"
# doesn't follow the "..._TIME_EXCEEDED" pattern the other three do - good
# thing this was confirmed against a real example instead of guessed.
FACTOR_VIOLATION_MAP = {
    "SHIFT_TIME_EXCEEDED": "Shift Violation",
    "BREAK_TIME_EXCEEDED": "Break Violation",
    "CYCLE_TIME_EXCEEDED": "Cycle Violation",
    "NO_PTI": "PTI Violation",
}

# Confirmed against live data: a driver can have more than one active
# violation at once (e.g. over both their 14-hour shift AND their 70-hour
# cycle limit at the same time). Asana's dropdown can only show one, so this
# is the tie-breaker order - not a regulatory ranking, just the order Asana's
# own dropdown options were created in, kept deterministic and logged
# whenever it actually matters (see _resolve_violation).
VIOLATION_PRIORITY_ORDER = [
    "Shift Violation",
    "Break Violation",
    "Cycle Violation",
    "PTI Violation",
]

# All four violation types are shown for this many days after they happen,
# regardless of whether Factor ELD has already marked them resolved (its own
# end_at field closes out surprisingly fast - e.g. a Shift violation cleared
# itself out in under a day) - confirmed directly that a violation should
# stay visible on the driver's task for a while so it actually gets seen,
# not disappear the moment the driver takes a break.
VIOLATION_LOOKBACK_DAYS = 7


def _is_violation_relevant(v, today_utc):
    """Whether one raw violation record happened recently enough (within
    VIOLATION_LOOKBACK_DAYS days) to still show on the driver's task."""
    work_date_str = v.get("work_date")
    if not work_date_str:
        return False
    try:
        work_date = datetime.strptime(work_date_str, "%Y-%m-%d").date()
    except ValueError:
        return False
    return 0 <= (today_utc - work_date).days <= VIOLATION_LOOKBACK_DAYS


# Factor ELD's own error_type codes for odometer problems, translated into
# the two options the "Odometer Jump" Asana board's dropdown actually has.
# Confirmed against live data: this same endpoint also returns unrelated
# error types (e.g. "ENGINE_POWER_UP_SEQUENCE") mixed in with these - only
# the two listed here are ever recognized; everything else is ignored
# rather than guessed at.
FACTOR_ODOMETER_ERROR_MAP = {
    "ODOMETER_WRONG": "Odometer jump",
    "ODOMETER_NOT_SET": "Odometer is missing",
}

# Confirmed directly: a driver can (rarely) have both odometer problems
# logged within the same lookback window - this is the tie-break order,
# same idea as VIOLATION_PRIORITY_ORDER above.
ODOMETER_PRIORITY_ORDER = ["Odometer jump", "Odometer is missing"]

# Unlike violations (which deliberately stay visible for a lookback window
# even after Factor ELD marks them resolved), odometer issues should
# disappear from Asana the moment they're fixed in Factor ELD - confirmed
# directly. So every ODOMETER_WRONG/ODOMETER_NOT_SET entry Factor ELD
# currently returns is treated as active; once fixing it makes Factor ELD
# stop returning that entry, the next cycle's "no longer in this cycle's
# active set" check (see sync.py's _sync_odometer_board) deletes the task.
# A generous cap still guards against showing something absurdly old if
# Factor ELD's own data ever contains a stale/lingering entry.
ODOMETER_MAX_AGE_DAYS = 30


def _is_odometer_error_relevant(error, today_utc):
    """Whether one raw odometer error record is recent enough to still
    count (see ODOMETER_MAX_AGE_DAYS above) - a generous sanity cap, not a
    "stay visible after being fixed" window like violations have."""
    work_date_str = error.get("work_date")
    if not work_date_str:
        return False
    try:
        work_date = datetime.strptime(work_date_str, "%Y-%m-%d").date()
    except ValueError:
        return False
    return 0 <= (today_utc - work_date).days <= ODOMETER_MAX_AGE_DAYS


def _format_odometer_timestamp(created_at, company_time_zone, logger):
    """Turn Factor ELD's raw "2026-07-16T15:21:42Z" (UTC) timestamp into a
    plain "YYYY-MM-DD HH:MM" local time for the Odometer Jump board's Date
    column, converted into the driver's own company_time_zone (an IANA zone
    name like "America/New_York", confirmed present in the same response
    this error came from) - no "UTC"/zone-abbreviation label, just the
    converted date and time. Falls back to UTC (still unlabeled) if the
    timezone is missing/unrecognized, and to the raw string if the
    timestamp itself isn't in the expected shape."""
    if not created_at:
        return ""
    try:
        parsed_utc = datetime.strptime(created_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return created_at

    if company_time_zone:
        try:
            local = parsed_utc.astimezone(ZoneInfo(company_time_zone))
            return local.strftime("%Y-%m-%d %I:%M %p")
        except ZoneInfoNotFoundError:
            logger.warning(
                "Factor ELD: unrecognized company_time_zone '%s' - falling "
                "back to UTC for this odometer timestamp.", company_time_zone,
            )

    return parsed_utc.strftime("%Y-%m-%d %I:%M %p")


def fetch_odometer_issues(logger, session_token=None, tenant_id=None, platform_label="Factor ELD"):
    """Return {driver_id: {"name", "company_name", "issue_type"}} for every
    driver Factor ELD currently reports an active odometer problem for
    ("Odometer jump" or "Odometer is missing"), fetched globally across
    every company in one paginated call (confirmed: omitting "company_ids"
    returns every company at once, unlike the per-company /hos/list
    endpoint). Disappears the moment Factor ELD stops returning it (i.e.
    once it's fixed) - see ODOMETER_MAX_AGE_DAYS above for why this isn't
    the same "stay visible after being fixed" lookback violations use.

    Confirmed response shape: a list of per-driver wrapper objects (each
    with its own company_name/driver_name) containing a nested "errors"
    array - name/company come from the wrapper, not the individual error
    entries (whose own "driver_name" is always blank).

    session_token/tenant_id default to Factor ELD's own .env credentials,
    but can be overridden - eld_leader.py calls this same function with its
    own credentials, since Leader ELD is confirmed to be a different tenant
    on this exact same backend (same host, same endpoints, same field
    names - only the credentials differ)."""
    session_token = session_token or os.environ.get("FACTOR_SESSION_TOKEN", "")
    tenant_id = tenant_id or os.environ.get("FACTOR_TENANT_ID", "")
    if not session_token or not tenant_id:
        logger.error(
            "FACTOR_SESSION_TOKEN or FACTOR_TENANT_ID is missing from .env - "
            "skipping odometer issue fetch this run."
        )
        return {}

    session = requests.Session()
    session.headers.update({
        "Authorization": f"Bearer {session_token}",
        "Tenant_id": tenant_id,
    })

    today_utc = datetime.now(timezone.utc).date()
    occurrences_by_driver = {}  # driver_id -> [(issue_type, created_at), ...]
    driver_info = {}  # driver_id -> (name, company_name, company_time_zone)
    page = 1
    total_pages = 1
    while page <= total_pages:
        body = _request_with_retries(
            session, SYSTEM_ERRORS_WARNINGS_API_BASE,
            {"page": page, "limit": PAGE_SIZE}, logger,
        )
        for wrapper in body["data"]["data"]:
            driver_id = wrapper.get("driver_id")
            if not driver_id:
                continue
            if _is_excluded_company(wrapper.get("company_name")):
                continue
            for entry in wrapper.get("errors", []):
                issue_type = FACTOR_ODOMETER_ERROR_MAP.get((entry.get("error_type") or "").strip().upper())
                if issue_type is None:
                    continue  # not an odometer error (e.g. ENGINE_POWER_UP_SEQUENCE) - ignore
                if not _is_odometer_error_relevant(entry, today_utc):
                    continue
                occurrences_by_driver.setdefault(driver_id, []).append(
                    (issue_type, entry.get("created_at") or "")
                )
                driver_info[driver_id] = (
                    wrapper.get("driver_name") or "",
                    wrapper.get("company_name") or "",
                    wrapper.get("company_time_zone") or "",
                )
        paging = body["data"].get("paging", {})
        total_pages = paging.get("totalPages", page)
        page += 1

    resolved = {}
    for driver_id, occurrences in occurrences_by_driver.items():
        name, company_name, company_time_zone = driver_info[driver_id]
        if not name.strip():
            continue
        unique_types = sorted({t for t, _ in occurrences}, key=ODOMETER_PRIORITY_ORDER.index)
        chosen = unique_types[0]
        if len(unique_types) > 1:
            logger.warning(
                "%s: driver '%s' currently has multiple odometer "
                "issues at once (%s) - showing '%s'.",
                platform_label, name, ", ".join(unique_types), chosen,
            )
        # The most recent occurrence of the chosen type - a driver can have
        # more than one of the same type within the lookback window (e.g.
        # a jump logged on two different days).
        chosen_created_at = max(
            created_at for issue_type, created_at in occurrences if issue_type == chosen
        )
        resolved[driver_id] = {
            "name": name, "company_name": company_name,
            "issue_type": chosen,
            "occurred_at": _format_odometer_timestamp(chosen_created_at, company_time_zone, logger),
        }

    logger.info(
        "%s: found %s driver(s) with an active odometer issue.",
        platform_label, len(resolved),
    )
    return resolved


# Your team's Factor ELD staff, keyed by first name (lower-cased) since
# that's how Factor ELD's own commit history labels each editor (e.g.
# "Joel J475") - confirmed against live commit data. Anyone whose name
# doesn't match this list (e.g. a shared/admin account like "ALGO CENTRAL
# Texas") isn't a recognized staff member - see _resolve_staff_editor.
STAFF_ID_BY_FIRST_NAME = {
    "david": "D195",
    "maria": "M545",
    "daniel": "D580",
    "alex": "A480",
    "joel": "J475",
    "ryan": "R535",
    "brandon": "B260",
    "kevin": "K790",
    "sam": "S505",
    "simon": "S830",
    "max": "M345",
    "tyler": "T840",
    "logan": "L825",
}

# Any commit editor whose name starts with "ALGO" (e.g. "ALGO SERVICE C TX",
# "ALGO CENTRAL Texas", "ALGO SERVICE BB") is a shared/admin account, not an
# individual staff member with their own code - all of these collapse to
# one label, confirmed directly for team "original" (Texas). This is NOT a
# generic default for every team - it's specifically Texas's own label, and
# is only ever used as an explicit default by sync.py's legacy single-team
# main() (which only ever serves Texas). Every other team gets its own
# algo_label passed in explicitly (see control_bot's "Staff Roster" ->
# "Set Commit Label", stored as config_store's algo_service_account_label) -
# a team with none configured gets NO label at all (see below), not this
# one - silently reusing Texas's label for every team was a real bug,
# confirmed live on team "missouri".
ALGO_SERVICE_ACCOUNT_LABEL = "Texas C"


def _resolve_staff_editor(edited_by_name, logger, staff_roster=None, algo_label=None):
    """Match a commit's edited_by_name against a known staff list. Returns
    (staff_id, display_name) - e.g. ("J475", "Joel J475") for a roster
    match, or (None, cleaned) using Factor/Leader ELD's own raw editor name
    as-is for anyone NOT in the roster - Factor/Leader ELD already tells us
    exactly who made the edit, so there's nothing to guess at; requiring a
    name to be hand-registered via the bot's "Staff Roster" menu before it
    can ever show up in Staff ID History would just be busywork (asana_
    client.py's _stage_enum_option auto-creates the dropdown option the
    first time a given name is seen). Only a genuinely empty edited_by_name,
    or an "ALGO ..." shared account this team has no algo_label configured
    for, returns bare None (nothing to show at all).

    staff_roster defaults to this team's own STAFF_ID_BY_FIRST_NAME, but a
    caller (e.g. a different team's own roster) can pass its own instead -
    a roster match still wins when there is one, purely so a known staff
    member's code (e.g. "J475") is included even on a platform whose own
    edited_by_name doesn't already embed it. algo_label has no shared
    default - see module docstring above."""
    staff_roster = STAFF_ID_BY_FIRST_NAME if staff_roster is None else staff_roster
    if not edited_by_name:
        return None
    cleaned = edited_by_name.strip()
    first_word = cleaned.split()[0].lower() if cleaned else ""

    if first_word == "algo":
        if not algo_label:
            return None  # this team has no commit label configured - leave it blank, same as any other unrecognized editor
        return None, algo_label

    staff_id = staff_roster.get(first_word)
    if staff_id is None:
        # Not in this team's curated roster - still show Factor/Leader
        # ELD's own editor name directly rather than leaving it blank.
        return None, cleaned
    # Factor ELD's own name already includes the ID for real staff (e.g.
    # "Joel J475") - use that as-is. Only append it ourselves if somehow
    # missing, so "Staff ID History" always shows the full "Name ID" form.
    display_name = cleaned if staff_id.lower() in cleaned.lower() else f"{cleaned} {staff_id}"
    return staff_id, display_name


def _fetch_latest_commit_editor(session, driver_id, logger, staff_roster=None, algo_label=None):
    """Return (staff_id, display_name, created_at) for whoever most recently
    edited one driver's logbook, or None if there's no commit history yet or
    the editor isn't a recognized staff member. created_at is included so a
    co-driver pair (two separate logbooks) can be compared to show whichever
    one was actually edited more recently - see sync.py."""
    body = _request_with_retries(
        session, COMMITS_API_BASE,
        {"driver_id": driver_id, "page": 1, "limit": 1}, logger,
    )
    commits = body.get("data", {}).get("commits", [])
    if not commits:
        return None
    resolved = _resolve_staff_editor(
        commits[0].get("edited_by_name"), logger, staff_roster, algo_label,
    )
    if resolved is None:
        return None
    staff_id, display_name = resolved
    return staff_id, display_name, commits[0].get("created_at")


def fetch_staff_editors(driver_ids, logger, session_token=None, tenant_id=None,
                          platform_label="Factor ELD", staff_roster=None, algo_label=None):
    """Return {driver_id: (staff_id, display_name, created_at) or None} for
    the given driver_ids. Only ever call this with driver_ids that actually
    have an Asana task (see sync.py) - this endpoint takes one driver_id per
    call with no bulk equivalent, so doing this for the whole fleet (2000+
    drivers) would be far slower than it needs to be.

    session_token/tenant_id default to Factor ELD's own .env credentials,
    but can be overridden - eld_leader.py calls this same function with its
    own credentials (same commits endpoint, same backend, confirmed).
    staff_roster/algo_label default to this team's own
    STAFF_ID_BY_FIRST_NAME/ALGO_SERVICE_ACCOUNT_LABEL, but can be overridden
    per-team (see control_bot's per-team config) since a shared roster would
    otherwise misattribute editors across different teams' overlapping
    first names."""
    session_token = session_token or os.environ.get("FACTOR_SESSION_TOKEN", "")
    tenant_id = tenant_id or os.environ.get("FACTOR_TENANT_ID", "")
    if not session_token or not tenant_id or not driver_ids:
        return {}

    session = requests.Session()
    session.headers.update({
        "Authorization": f"Bearer {session_token}",
        "Tenant_id": tenant_id,
    })

    results = {}
    with ThreadPoolExecutor(max_workers=MAX_PARALLEL_COMMIT_FETCHES) as pool:
        future_to_id = {
            pool.submit(
                _fetch_latest_commit_editor, session, driver_id, logger, staff_roster, algo_label,
            ): driver_id
            for driver_id in driver_ids
        }
        for future in as_completed(future_to_id):
            driver_id = future_to_id[future]
            try:
                results[driver_id] = future.result()
            except Exception:
                logger.exception(
                    "%s: failed to fetch commit history for "
                    "driver_id '%s' - leaving Staff ID unset this run.",
                    platform_label, driver_id,
                )
                results[driver_id] = None
    return results


def _fetch_fmcsa_logs_for_company(session_headers, company_id, logger, platform_label="Factor ELD"):
    """Return every HOS Audit Transfer log entry Factor/Leader ELD has for
    one company, most-recent first (confirmed by the dashboard's own
    behavior - the first page always showed today's newest transfer at the
    top). Only page 1 - see fetch_fmcsa_transfers for why the full history
    isn't needed."""
    scoped_session = requests.Session()
    scoped_session.headers.update(session_headers)
    scoped_session.headers["Company_id"] = company_id
    body = _request_with_retries(
        scoped_session, FMCSA_API_BASE,
        {"page": 1, "limit": FMCSA_PAGE_SIZE}, logger, platform_label,
    )
    return body.get("data", {}).get("logs", [])


def fetch_fmcsa_transfers(logger, session_token=None, tenant_id=None, platform_label="Factor ELD",
                            apply_company_filter=True, company_filter=None):
    """Return every company's recent HOS Audit Transfer log entries (raw
    dicts straight from the API, untouched - see _fetch_fmcsa_logs_for_company
    for the confirmed shape) across every company this token can see.
    Callers (see sync.py's check_fmcsa_transfers) match on each entry's own
    "id" field to find ones they haven't alerted about yet - this function
    itself has no notion of "new", just "currently visible".

    session_token/tenant_id/apply_company_filter/company_filter behave
    exactly like fetch_drivers' own params (same per-team override pattern),
    since this reuses the same company discovery and per-company fetch
    machinery."""
    session_token = session_token or os.environ.get("FACTOR_SESSION_TOKEN", "")
    tenant_id = tenant_id or os.environ.get("FACTOR_TENANT_ID", "")
    if not session_token or not tenant_id:
        return []

    company_filter = _get_company_filter(company_filter) if apply_company_filter else None

    session = requests.Session()
    session.headers.update({
        "Authorization": f"Bearer {session_token}",
        "Tenant_id": tenant_id,
    })

    if company_filter is not None and len(company_filter) == 1:
        companies_to_fetch = [{"company_id": company_filter[0], "company_name": None}]
    else:
        companies_to_fetch = _discover_companies(session, logger, tenant_id, platform_label)
        if company_filter is not None:
            normalized_filter = {_normalize_id(c) for c in company_filter}
            companies_to_fetch = [
                c for c in companies_to_fetch
                if _normalize_id(c["company_id"]) in normalized_filter
            ]

    all_logs = []
    with ThreadPoolExecutor(max_workers=MAX_PARALLEL_COMPANY_FETCHES) as pool:
        future_to_company = {
            pool.submit(
                _fetch_fmcsa_logs_for_company, session.headers, company["company_id"], logger, platform_label,
            ): company
            for company in companies_to_fetch
        }
        for future in as_completed(future_to_company):
            company = future_to_company[future]
            try:
                all_logs.extend(future.result())
            except Exception:
                logger.exception(
                    "%s: failed to check HOS Audit Transfer history for company "
                    "'%s' - skipping it this run.",
                    platform_label, company.get("company_name") or company["company_id"],
                )
    return all_logs


def _normalize_id(value):
    """Make company-id comparisons ignore dashes/case, since we've seen the
    same id written both with and without dashes."""
    return (value or "").replace("-", "").lower()


def _get_company_filter(explicit_filter=None):
    """A comma-separated list of Factor ELD company IDs to restrict the sync
    to. Empty/unset means "no filter, use every company" once we're ready to
    go live everywhere.

    explicit_filter, if given (a list of company ID strings, or an already
    comma-separated string), takes priority - this is how a caller with its
    own per-team config (e.g. control_bot, or a different team's .env)
    supplies its own filter without relying on this process's own
    FACTOR_COMPANY_FILTER env var. Falls back to reading FACTOR_COMPANY_FILTER
    from .env when omitted, unchanged from before.

    Returns the IDs exactly as written (dashes and case preserved) - callers
    that need to compare IDs should normalize both sides themselves with
    _normalize_id(). The raw form is what must be sent as the Company_id
    header - a normalized (dash-stripped) ID is not a real company ID and
    Factor ELD's API rejects it."""
    if explicit_filter is not None:
        if isinstance(explicit_filter, str):
            return [part.strip() for part in explicit_filter.split(",") if part.strip()] or None
        return list(explicit_filter) or None
    raw = os.environ.get("FACTOR_COMPANY_FILTER", "").strip()
    if not raw:
        return None
    return [part.strip() for part in raw.split(",") if part.strip()]


def _request_with_retries(session, url, params, logger, platform_label="Factor ELD"):
    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = session.get(url, params=params, timeout=30)
            if resp.status_code == 401:
                raise RuntimeError(
                    f"{platform_label} rejected the request as unauthorized - "
                    f"the session token has likely expired (lifetime varies - "
                    f"confirmed anywhere from under a day to about 30 days "
                    f"depending on how it was issued) and needs to be manually "
                    f"refreshed in .env until automatic login is added."
                )
            if resp.status_code >= 500:
                raise requests.HTTPError(f"{platform_label} server error {resp.status_code}")
            resp.raise_for_status()
            return resp.json()
        except RuntimeError:
            # Not worth retrying an auth failure - it won't fix itself.
            raise
        except requests.RequestException as exc:
            last_error = exc
            logger.warning(
                "%s request failed (attempt %s/%s): %s",
                platform_label, attempt, MAX_RETRIES, exc,
            )
            if attempt < MAX_RETRIES:
                # Confirmed live (2026-07-22 Railway logs): when a burst of
                # ~10 concurrent per-company requests (MAX_PARALLEL_COMPANY_
                # FETCHES) trips the backend's rate limit, every one of those
                # threads fails at once and, with a flat delay, retries at
                # once too - hitting the exact same limit again on attempt
                # 2/3 and 3/3. Exponential backoff + random jitter spreads
                # the retries out so they stop re-triggering each other.
                delay = RETRY_DELAY_SECONDS * (2 ** (attempt - 1))
                time.sleep(delay + random.uniform(0, delay))
    raise last_error


def _resolve_violation(raw_violation_types, driver_name, logger, platform_label="Factor ELD"):
    """Translate a driver's currently-active violation_type code(s) into a
    single one of Asana's four dropdown options. Returns None when there's
    no active violation at all. Unrecognized codes are skipped with a
    warning rather than guessed. If more than one recognized violation is
    active at the same time (confirmed this really happens), the
    highest-priority one wins (see VIOLATION_PRIORITY_ORDER) and a warning
    names the others so nothing is silently dropped."""
    if not raw_violation_types:
        return None

    mapped = []
    for raw in raw_violation_types:
        if not raw:
            continue
        resolved = FACTOR_VIOLATION_MAP.get(raw.strip().upper())
        if resolved is None:
            logger.warning(
                "%s: driver '%s' has an unrecognized violation_type "
                "'%s' - ignoring it. Add it to FACTOR_VIOLATION_MAP in "
                "eld_factor.py once you know which of Shift/Break/Cycle/PTI "
                "Violation it maps to.",
                platform_label, driver_name, raw,
            )
        else:
            mapped.append(resolved)

    if not mapped:
        return None

    unique_mapped = sorted(set(mapped), key=VIOLATION_PRIORITY_ORDER.index)
    chosen = unique_mapped[0]
    if len(unique_mapped) > 1:
        logger.warning(
            "%s: driver '%s' currently has multiple active "
            "violations at once (%s) - showing '%s' on their task.",
            platform_label, driver_name, ", ".join(unique_mapped), chosen,
        )
    return chosen


def _fetch_active_violations(session, logger, platform_label="Factor ELD"):
    """Return {(company_name, driver_name): [raw_violation_type, ...]} for
    every driver with one or more HOS violations within the last
    VIOLATION_LOOKBACK_DAYS days (see _is_violation_relevant), across every
    company, in a single paginated call. Read-only, like everything else in
    this file. Keys are lower-cased/stripped for case-insensitive matching
    against the same platform's own driver data - this never needs to match
    Asana's spelling, so it doesn't need the fancier normalizing helpers in
    asana_client.py."""
    today_utc = datetime.now(timezone.utc).date()
    violations = {}
    page = 1
    total_pages = 1
    while page <= total_pages:
        body = _request_with_retries(
            session, SYSTEM_VIOLATIONS_API_BASE,
            {"page": page, "limit": PAGE_SIZE}, logger, platform_label,
        )
        for entry in body["data"]["data"]:
            key = (
                (entry.get("company_name") or "").strip().lower(),
                (entry.get("driver_name") or "").strip().lower(),
            )
            for v in entry.get("violations", []):
                if not _is_violation_relevant(v, today_utc):
                    continue
                violations.setdefault(key, []).append(v.get("violation_type"))
        paging = body["data"].get("paging", {})
        total_pages = paging.get("totalPages", page)
        page += 1
    return violations


def _raw_driver_to_record(raw, logger, company_name_override=None, violations_by_key=None, platform_label="Factor ELD"):
    """Turn one raw driver dict (from Factor ELD or Leader ELD - same
    backend, same shape) into a standard Driver record."""
    raw_code = raw.get("current_status")
    is_active = bool(raw.get("driver_status"))
    status = map_status(FACTOR_STATUS_MAP, raw_code, is_active)

    if status == "Unknown" and is_active:
        logger.warning(
            "%s: driver '%s' has an unrecognized status code "
            "'%s' - set to Unknown. Add it to FACTOR_STATUS_MAP in "
            "eld_factor.py once you know what it means.",
            platform_label, raw.get("driver_name"), raw_code,
        )

    company_name = company_name_override or raw.get("company_name")
    driver_name = raw.get("driver_name", "").strip()

    violation = None
    if violations_by_key:
        key = ((company_name or "").strip().lower(), driver_name.strip().lower())
        violation = _resolve_violation(violations_by_key.get(key), driver_name, logger, platform_label)

    return Driver(
        name=driver_name,
        status=status,
        source=platform_label,
        raw_status=raw_code or "",
        vehicle_number=raw.get("vehicle_number"),
        company_name=company_name,
        violation=violation,
        driver_id=raw.get("driver_id"),
    )


def _word_sort_key(name):
    """Same words in any order collapse to the same key - catches the same
    physical driver spelled "Last First" in one record and "First Last" in
    another (confirmed happening for real - see _dedupe_duplicate_person_
    records). A local copy rather than importing asana_client's version,
    since this file never depends on anything Asana-specific."""
    return " ".join(sorted((name or "").strip().lower().split()))


def _dedupe_duplicate_person_records(tagged_raw_drivers, logger, platform_label="Factor ELD"):
    """Factor ELD sometimes lists the same physical driver twice under two
    different driver_ids - confirmed against live data in two flavors: both
    within one company (a stale "Makhmadkulov Akbar", inactive/no vehicle,
    next to a live "Akbar makhmadkulov", active/vehicle assigned) AND across
    two different companies (active at their new company, with a stale
    leftover record still sitting at their old one - e.g. "Zviad
    Tskhvaradze" active at Taurus Transportation INC but also inactive at
    PALASH FREIGHT LLC). This has to run globally across every company, not
    per-company, to catch the second kind - Asana tasks are matched purely
    by driver name regardless of company anyway, so a same-named duplicate
    causes the same bug no matter which company it's sitting in.

    Left as-is, this causes a real bug: the stale copy deletes the driver's
    Asana task, then the live copy - working from the same cycle's now-stale
    snapshot of Asana - tries to update that just-deleted task and 404s,
    leaving the driver with no task at all until the confusion happens to
    clear up on its own. Collapsing duplicates here, before any of that
    logic runs, fixes it at the source.

    tagged_raw_drivers: [(company_name, raw_dict), ...]

    Keeps whichever copy is active with a vehicle assigned; if several
    copies are (unusually) all active at once, keeps the first and logs a
    warning rather than guessing which one is "real". Entries with no name
    at all pass through untouched - nothing to key a dedupe on."""
    def _is_live(r):
        return bool(r.get("driver_status")) and bool((r.get("vehicle_number") or "").strip())

    best_by_key = {}
    passthrough = []
    for company_name, raw in tagged_raw_drivers:
        key = _word_sort_key(raw.get("driver_name"))
        if not key:
            passthrough.append((company_name, raw))
            continue
        existing = best_by_key.get(key)
        if existing is None:
            best_by_key[key] = (company_name, raw)
            continue

        existing_company, existing_raw = existing
        if _is_live(raw) and not _is_live(existing_raw):
            best_by_key[key] = (company_name, raw)
        elif _is_live(raw) and _is_live(existing_raw):
            logger.warning(
                "%s: '%s' has more than one active, vehicle-assigned "
                "record at once (at '%s' driver_id %s, and at '%s' driver_id "
                "%s) - keeping the first one seen, ignoring the rest.",
                platform_label, raw.get("driver_name"), existing_company, existing_raw.get("driver_id"),
                company_name, raw.get("driver_id"),
            )
        # else: existing already wins (it's live and this one isn't, or
        # neither is live - keep whichever was seen first).
    return list(best_by_key.values()) + passthrough


# Cache of {"company_id":..., "company_name":...} pairs for every company
# each tenant knows about, so we don't have to rediscover them every single
# cycle - the list of companies changes rarely, unlike driver statuses.
# Keyed by (tenant_id, session_token) - confirmed live (2026-07-22) that two
# teams sharing the exact same tenant_id (Texas and Missouri) can still have
# genuinely different company visibility per their own session token (Texas's
# token sees 19 companies including KARAHAN LOGISTICS LLC; Missouri's token
# sees 60 completely different ones, zero overlap). Keying by tenant_id alone
# let whichever team's thread ran first silently overwrite the other team's
# view for up to COMPANY_CACHE_TTL_SECONDS - Texas's real companies would
# vanish from its own sync for up to 30 minutes at a time whenever Missouri's
# concurrent cycle (see multi_sync.py's team-level concurrency) raced ahead.
_company_cache = {}
COMPANY_CACHE_TTL_SECONDS = 1800  # 30 minutes


def _discover_companies(session, logger, tenant_id, platform_label="Factor ELD"):
    """Return every company this tenant currently has, as a list of
    {"company_id", "company_name"} dicts. This uses the big multi-page
    all-companies endpoint just to learn WHICH companies exist - if a
    company is very rarely missed here due to live data shifting between
    pages, we simply notice it one refresh cycle later. That's a much
    smaller problem than using this same endpoint for actual duty-status
    accuracy (which is why driver data itself is always fetched per-company
    instead, below)."""
    now = time.time()
    cache_key = (tenant_id, session.headers.get("Authorization", ""))
    cache_entry = _company_cache.get(cache_key)
    if cache_entry is not None and (now - cache_entry["fetched_at"]) < COMPANY_CACHE_TTL_SECONDS:
        return cache_entry["companies"]

    raw_drivers = _fetch_paged(
        session, SYSTEM_LIST_API_BASE,
        {"sort_by": "default", "sort_order": "default"}, logger, platform_label,
    )
    seen = {}
    for raw in raw_drivers:
        company_id = raw.get("company_id")
        if company_id and company_id not in seen and not _is_excluded_company(raw.get("company_name")):
            seen[company_id] = raw.get("company_name")
    companies = [{"company_id": cid, "company_name": name} for cid, name in seen.items()]

    _company_cache[cache_key] = {"companies": companies, "fetched_at": now}
    logger.info("%s: discovered %s companies.", platform_label, len(companies))
    return companies


def _fetch_one_company(session_headers, company_id, logger, platform_label="Factor ELD"):
    """Fetch every raw driver dict for one company using the fast,
    single-page-sized per-company endpoint (reliable, unlike paging through
    the live all-companies list). Returns raw dicts (not yet converted to
    Driver records) - conversion happens later, after all companies have
    been fetched and merged, so it stays simple and thread-safe."""
    scoped_session = requests.Session()
    scoped_session.headers.update(session_headers)
    scoped_session.headers["Company_id"] = company_id
    return _fetch_paged(
        scoped_session, SINGLE_COMPANY_API_BASE,
        {"sort_by": "duty_status", "sort_order": "asc"}, logger, platform_label,
    )


def _fetch_paged(session, url, extra_params, logger, platform_label="Factor ELD"):
    """Fetch every page of drivers from one endpoint, returning the raw
    driver dicts, deduplicated by driver_id (Factor ELD's live data
    sometimes repeats the same driver_id across pages of the same call)."""
    raw_drivers = []
    seen_driver_ids = set()
    page = 1
    total_pages = 1  # we don't know the real number until the first response

    while page <= total_pages:
        params = {
            "page": page,
            "limit": PAGE_SIZE,
            "eld_status": "all",
            "duty_status": "all",
            "online_status": "all",
            "violation_status": "all",
            # "all" (not "active") so drivers who get turned off still show
            # up in the results - otherwise we'd never see them go inactive.
            "driver_status": "all",
            **extra_params,
        }
        body = _request_with_retries(session, url, params, logger, platform_label)

        for raw in body["data"]["drivers"]:
            driver_id = raw.get("driver_id")
            if driver_id is not None:
                if driver_id in seen_driver_ids:
                    continue
                seen_driver_ids.add(driver_id)
            raw_drivers.append(raw)

        paging = body["data"].get("paging", {})
        total_pages = paging.get("totalPages", page)
        page += 1

    return raw_drivers


def fetch_drivers(logger, session_token=None, tenant_id=None, platform_label="Factor ELD",
                    apply_company_filter=True, company_filter=None, company_name=None):
    """Return a list of eld_common.Driver records from Factor ELD (or Leader
    ELD - same backend, confirmed a different tenant on the exact same
    host/endpoints - see eld_leader.py, which calls this same function with
    its own credentials instead of duplicating this logic).

    session_token/tenant_id default to Factor ELD's own .env credentials,
    but can be overridden. apply_company_filter controls whether a company
    filter applies at all - Leader ELD always fetches every company, so its
    wrapper passes False. company_filter/company_name, if given, take
    priority over this process's own FACTOR_COMPANY_FILTER/FACTOR_COMPANY_NAME
    env vars - this is how a caller with its own per-team config supplies its
    own filter without relying on this process's .env."""
    session_token = session_token or os.environ.get("FACTOR_SESSION_TOKEN", "")
    tenant_id = tenant_id or os.environ.get("FACTOR_TENANT_ID", "")
    if not session_token or not tenant_id:
        logger.error(
            "FACTOR_SESSION_TOKEN or FACTOR_TENANT_ID is missing from .env - "
            "skipping %s this run.", platform_label,
        )
        return []

    company_filter = _get_company_filter(company_filter) if apply_company_filter else None

    session = requests.Session()
    session.headers.update({
        "Authorization": f"Bearer {session_token}",
        "Tenant_id": tenant_id,
    })

    try:
        violations_by_key = _fetch_active_violations(session, logger, platform_label)
    except Exception:
        # A hiccup on the violations endpoint shouldn't stop the whole sync -
        # everyone's Violation field just stays as it was until next cycle.
        logger.exception(
            "%s: failed to fetch violations - continuing without "
            "violation data this run.", platform_label,
        )
        violations_by_key = {}

    if company_filter is not None and len(company_filter) == 1:
        # Exactly one company to watch, and we already know its ID from
        # .env - no need to discover anything, just fetch it directly.
        companies_to_fetch = [{
            "company_id": company_filter[0],
            "company_name": (company_name or os.environ.get("FACTOR_COMPANY_NAME", "").strip()) or None,
        }]
    else:
        # All companies (or a specific multi-company list) - figure out
        # which companies exist (cached, see _discover_companies), then
        # fetch each one individually below. This is slower than one big
        # all-companies call, but each per-company call is small and
        # reliable, so we never miss a driver the way the big call can.
        companies_to_fetch = _discover_companies(session, logger, tenant_id, platform_label)
        if company_filter is not None:
            normalized_filter = {_normalize_id(c) for c in company_filter}
            companies_to_fetch = [
                c for c in companies_to_fetch
                if _normalize_id(c["company_id"]) in normalized_filter
            ]

    # Fetch every company's raw driver list at the same time (this is all
    # read-only network traffic, so there's no risk of the race conditions
    # that writes would have). Each thread only touches its own data - the
    # results are combined afterward, one at a time, which is where
    # deduplication and converting to Driver records happens safely.
    raw_by_company = []  # [(company_name, [raw_driver_dict, ...]), ...]
    with ThreadPoolExecutor(max_workers=MAX_PARALLEL_COMPANY_FETCHES) as pool:
        future_to_company = {
            pool.submit(_fetch_one_company, session.headers, company["company_id"], logger, platform_label): company
            for company in companies_to_fetch
        }
        for future in as_completed(future_to_company):
            company = future_to_company[future]
            try:
                raw_by_company.append((company["company_name"], future.result()))
            except Exception:
                # One company having a bad moment shouldn't stop us from
                # syncing every other company.
                logger.exception(
                    "%s: failed to fetch company '%s' - skipping it this run.",
                    platform_label, company.get("company_name") or company["company_id"],
                )

    # Now merge everything together, single-threaded: tag each raw driver
    # with its company, collapse same-physical-driver duplicates globally
    # across every company (see _dedupe_duplicate_person_records - this
    # must run across the whole fetched set, not per-company, since the
    # stale duplicate can be sitting at a different company than the live
    # one), skip any driver_id we've already seen, and convert to our
    # standard Driver record.
    tagged_raw_drivers = [
        (company_name, raw)
        for company_name, raw_drivers in raw_by_company
        for raw in raw_drivers
    ]
    deduped = _dedupe_duplicate_person_records(tagged_raw_drivers, logger, platform_label)

    drivers = []
    seen_driver_ids = set()
    for company_name, raw in deduped:
        if _is_excluded_company(company_name or raw.get("company_name")):
            continue
        driver_id = raw.get("driver_id")
        if driver_id is not None:
            if driver_id in seen_driver_ids:
                continue
            seen_driver_ids.add(driver_id)
        drivers.append(_raw_driver_to_record(
            raw, logger, company_name_override=company_name,
            violations_by_key=violations_by_key, platform_label=platform_label,
        ))

    logger.info(
        "%s: fetched %s driver(s) across %s compan%s.",
        platform_label, len(drivers), len(companies_to_fetch), "y" if len(companies_to_fetch) == 1 else "ies",
    )
    return drivers


def _fetch_drivers_list_one_status(session, status, logger):
    """Fetch every raw driver dict from the /api/v1/drivers endpoint for one
    status value ("active" or "inactive"), following its confirmed
    {"paging": {"totalPages", ...}} shape."""
    raw_drivers = []
    page = 1
    total_pages = 1
    while page <= total_pages:
        body = _request_with_retries(
            session, DRIVERS_LIST_API_BASE,
            {"page": page, "search": "", "limit": PAGE_SIZE, "status": status},
            logger,
        )
        raw_drivers.extend(body["data"]["drivers"])
        paging = body["data"].get("paging", {})
        total_pages = paging.get("totalPages", page)
        page += 1
    return raw_drivers


def fetch_driver_database_records(logger, session_token=None, tenant_id=None, platform_label="Factor ELD"):
    """Return every driver (active AND inactive) from Factor ELD, as
    DriverDatabaseRecord objects, for the standalone "Database" Asana board -
    a permanent reference list, unlike fetch_drivers() above which only
    covers currently-visible dispatch status.

    Deliberately does NOT run this file's usual _dedupe_duplicate_person_
    records step: that function exists to collapse a stale/inactive copy of
    the same physical driver so the DISPATCH boards only show one live task -
    exactly the opposite of what this board needs (every driver_id record
    kept, including inactive ones). driver_id here is only used to drop
    literal duplicate entries from Factor ELD's own paginated response (the
    same driver_id occasionally repeats across pages) - asana_client.py's
    database-board functions match each record by (company name, driver
    name) instead, scoped per company section.

    session_token/tenant_id default to Factor ELD's own .env credentials,
    but can be overridden - see fetch_odometer_issues for why (Leader ELD
    reuses this same function with its own credentials)."""
    session_token = session_token or os.environ.get("FACTOR_SESSION_TOKEN", "")
    tenant_id = tenant_id or os.environ.get("FACTOR_TENANT_ID", "")
    if not session_token or not tenant_id:
        logger.error(
            "FACTOR_SESSION_TOKEN or FACTOR_TENANT_ID is missing from .env - "
            "skipping the driver database fetch this run."
        )
        return []

    session = requests.Session()
    session.headers.update({
        "Authorization": f"Bearer {session_token}",
        "Tenant_id": tenant_id,
    })

    raw_drivers = []
    for status in DRIVERS_LIST_STATUSES:
        raw_drivers.extend(_fetch_drivers_list_one_status(session, status, logger))

    records = []
    seen_driver_ids = set()
    for raw in raw_drivers:
        driver_id = raw.get("driver_id")
        if not driver_id or driver_id in seen_driver_ids:
            continue
        if _is_excluded_company(raw.get("company_name")):
            continue
        seen_driver_ids.add(driver_id)

        first = (raw.get("first_name") or "").strip()
        last = (raw.get("last_name") or "").strip()
        name = f"{first} {last}".strip()
        if not name:
            continue

        login = (raw.get("user_name") or "").strip()
        records.append(DriverDatabaseRecord(
            driver_id=driver_id,
            name=name,
            company_name=(raw.get("company_name") or "").strip(),
            co_driver_name=(raw.get("co_driver_full_name") or "").strip(),
            vehicle_number=(raw.get("assigned_vehicle_number") or "").strip(),
            email=(raw.get("email") or "").strip(),
            phone_number=(raw.get("phone_number") or "").strip(),
            cdl=(raw.get("license_number") or "").strip(),
            state=(raw.get("license_state") or "").strip(),
            login=login,
        ))

    logger.info("%s: fetched %s driver record(s) for the Database board.", platform_label, len(records))
    return records
