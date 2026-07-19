"""
eld_common.py

This file defines the ONE standard shape that both ELD platforms (Factor ELD
and, later, Leader ELD) get converted into. Every platform's own file
(eld_factor.py, eld_leader.py) is responsible for reading its own raw data
and turning it into a list of these simple "Driver" records.

Because of this, the rest of the program (sync.py, asana_client.py) never
needs to know anything about Factor ELD or Leader ELD specifically - it only
ever works with this one common shape. That's what makes it easy to add a
third or fourth ELD platform later without touching Asana-related code.
"""

from dataclasses import dataclass

# These are the ONLY six words that ever get written into Asana's "Duty
# Status" field. Every platform's raw status code must be translated into
# one of these before it reaches Asana.
STANDARD_STATUSES = [
    "Driving",
    "Sleeping",
    "Off Duty",
    "On Duty",
    "Off Platform",
    "Unknown",
]


@dataclass
class Driver:
    """One driver's current duty status, in our standard shape."""

    name: str                    # driver's full name, used to find their Asana task
    status: str                  # one of STANDARD_STATUSES (already translated)
    source: str                  # which platform this came from, e.g. "Factor ELD"
    raw_status: str               # the original code the platform sent, kept for logging
    vehicle_number: str = None    # truck/unit number, if the platform provides one -
                                   # written into Asana's "Vehicle number" field
    company_name: str = None      # the driver's company/carrier name, if the platform
                                   # provides one - used to find which Asana section a
                                   # brand-new driver's task should be created under
    violation: str = None          # one of STANDARD_VIOLATION_TYPES, or None if the
                                   # driver has no currently-active HOS violation
    driver_id: str = None          # the platform's own internal driver ID, if it has
                                   # one - used to look up who last edited this
                                   # driver's logbook (see eld_factor.fetch_staff_editors)


@dataclass
class DriverDatabaseRecord:
    """One driver's reference/contact info, for the standalone 'Database'
    Asana board - a permanent record of every driver (active AND inactive),
    unlike Driver above which only covers currently-visible dispatch status.
    driver_id is the reliable matching key here (not name - this board spans
    every company at once, and duplicate/placeholder names do happen)."""

    driver_id: str
    name: str
    company_name: str = ""
    co_driver_name: str = ""
    vehicle_number: str = ""
    email: str = ""
    phone_number: str = ""
    cdl: str = ""
    state: str = ""
    login: str = ""


# The four violation categories Asana's "Woring Vilation" dropdown supports.
# A driver with no currently-active violation gets this field cleared
# (blank) rather than set to some "None" option - unlike duty status, "no
# violation" is the normal/expected state, not something worth flagging.
STANDARD_VIOLATION_TYPES = [
    "Shift Violation",
    "Break Violation",
    "Cycle Violation",
    "PTI Violation",
]


# When two drivers share one vehicle (a co-driver team), their shared Asana
# task can only show one status at a time. This is the priority order used
# to pick which one wins - e.g. if one co-driver is Driving and the other is
# Sleeping (the normal case for a team truck), the task shows "Driving"
# because that's the more "active" of the two.
STATUS_PRIORITY_ORDER = [
    "Driving",
    "On Duty",
    "Sleeping",
    "Off Duty",
    "Unknown",
    "Off Platform",
]


def higher_priority_status(status_a, status_b):
    """Given two drivers' statuses, return whichever one ranks higher in
    STATUS_PRIORITY_ORDER (the more "active" one). Used only for co-driver
    pairs sharing a single Asana task - solo drivers are unaffected."""
    def rank(status):
        try:
            return STATUS_PRIORITY_ORDER.index(status)
        except ValueError:
            return len(STATUS_PRIORITY_ORDER)

    return status_a if rank(status_a) <= rank(status_b) else status_b


def invisibility_reason(driver):
    """A driver should only show up in Asana while they're active on their
    platform AND have a vehicle assigned there - a driver with no truck
    isn't actually working a load, so Asana shouldn't list them at all.
    Returns None if the driver should be visible, otherwise a short
    human-readable reason. Shared (not sync.py-private) since control_bot's
    /trucks command needs the exact same "active" definition sync.py's own
    dispatch logic already uses, rather than inventing a second one."""
    if driver.status == "Off Platform":
        return "is Off Platform"
    if not (driver.vehicle_number or "").strip():
        return "has no vehicle assigned in Factor ELD"
    return None


def map_status(code_to_status, raw_code, is_active):
    """
    Turn one platform's raw status code into one of our standard statuses.

    code_to_status : a dictionary specific to one platform, for example
                      {"DS_D": "Driving", "DS_SB": "Sleeping", ...}
    raw_code       : the raw status string that platform sent for this driver
    is_active      : False if the platform says this driver/device is turned
                      off or disconnected

    Rule #1: if the driver is not active on the platform at all, we always
    report "Off Platform", no matter what raw_code says.

    Rule #2: otherwise, look up raw_code in the platform's own mapping table.
    The lookup ignores upper/lower case and extra spaces, so "driving",
    " Driving ", and "DRIVING" all match the same entry.

    Rule #3: if we don't recognize the code, return "Unknown" so it's
    obvious in Asana that this driver needs a human to look at them, rather
    than silently leaving the wrong status in place.
    """
    if not is_active:
        return "Off Platform"

    if not raw_code:
        return "Unknown"

    normalized_code = raw_code.strip().upper()

    # Build a normalized (upper-case, trimmed) copy of the platform's mapping
    # table so lookups aren't case-sensitive.
    normalized_map = {
        key.strip().upper(): value for key, value in code_to_status.items()
    }

    return normalized_map.get(normalized_code, "Unknown")
