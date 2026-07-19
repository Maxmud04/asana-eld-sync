"""
asana_client.py

This file knows how to talk to Asana. It:
  1. Finds each driver's task by name, searching across every Asana project
     we've been told to sync.
  2. Reads and updates that task's duty-status dropdown field (it auto-
     detects whether the field is called "Status", "New Driver", or
     "Duty Status" - different projects here use different names).

It does NOT know anything about Factor ELD or Leader ELD - it only ever
works with the standard eld_common.Driver records that the platform files
(eld_factor.py, eld_leader.py) produce. That separation is what lets us add
more ELD platforms later without changing anything in this file.
"""

import re
import time
import unicodedata

import requests

ASANA_API_BASE = "https://app.asana.com/api/1.0"

# We don't hardcode which custom field holds the duty status, because the
# three real Asana projects each name it slightly differently. Instead we
# look for a dropdown ("enum") field whose name matches one of these
# (case-insensitive), and use whichever one is found first.
STATUS_FIELD_NAME_CANDIDATES = ["status", "new driver", "duty status"]

# Some projects abbreviate the Status dropdown's own OPTION labels instead
# of using the full word (confirmed intentional for Maxmud Test A - its
# options are literally "DR"/"SB"/"OFF"/"ON", with stray invisible
# formatting characters mixed in). Maps a cleaned-up abbreviated label (see
# _clean_option_label) to the standard status word it stands for, so we can
# still write to it by our own standard name.
STATUS_ABBREVIATION_ALIASES = {
    "dr": "Driving",
    "sb": "Sleeping",
    "off": "Off Duty",
    "on": "On Duty",
}


def _clean_option_label(name):
    """Strip invisible formatting characters (category "Cf" - e.g. the
    left-to-right marks Asana let someone paste into an option's name) and
    collapse whitespace, so an abbreviated option label like
    "\u200E \u200EDR\u200E \u200E  \u200E" is recognized as plain "dr"."""
    cleaned = "".join(ch for ch in (name or "") if unicodedata.category(ch) != "Cf")
    return _WHITESPACE_PATTERN.sub(" ", cleaned).strip().lower()

# The dropdown for truck/unit number to look for, if a project has one. Not
# every project has this field (only Maxmud Test A does, right now) - when
# a project doesn't, we simply skip writing a vehicle number there instead
# of treating it as an error.
VEHICLE_FIELD_NAME_CANDIDATES = ["vehicle number"]

# The HOS violation dropdown to look for, if a project has one. Originally
# spelled "Woring Vilation" (typo and all); renamed in different projects to
# "Woring Violation" (Test B, confirmed live) and "Worning Violation" - all
# three stay in this list since different projects may still have any of
# them, plus a couple of more sensibly-spelled alternatives in case it's
# renamed again. Not every project has this field - same "just skip it"
# rule as the vehicle number field above.
VIOLATION_FIELD_NAME_CANDIDATES = [
    "woring vilation",
    "woring violation",
    "worning violation",
    "violation",
    "violations",
]

# Who most recently edited a driver's logbook in Factor ELD, shown two ways:
# a short code ("#J475") and the full name+code ("Joel J475"). Both are
# optional - not every project has them.
STAFF_ID_FIELD_NAME_CANDIDATES = ["staff id"]
STAFF_HISTORY_FIELD_NAME_CANDIDATES = ["staff id history"]

# Field names for the separate "Database" board (every driver's contact/
# reference info, grouped into per-company sections just like the dispatch
# boards - see the database-board methods near the end of this file).
# Field name for each "Odometer Jump" board (one per dispatch board - see
# the odometer-board methods near the end of this file). Per-company
# sectioned, same as the dispatch boards themselves.
ODOMETER_FIELD_NAME_CANDIDATES = ["odometer"]
ODOMETER_DATE_FIELD_NAME_CANDIDATES = ["date", "date and time"]

# Empty divider sections used to visually cluster the Odometer Jump board's
# per-company sections by dispatch board - see cleanup_empty_odometer_sections.
ODOMETER_DIVIDER_SECTION_NAMES = {"Texas A", "Texas B", "Texas C"}

DATABASE_FIELD_NAME_CANDIDATES = {
    "co_driver": ["co-driver", "co driver"],
    "vehicle_number": ["vehicle id"],
    "email": ["email"],
    "phone_number": ["ph number", "phone number"],
    "cdl": ["cdl"],
    "state": ["state"],
    "login": ["login"],
    "password": ["password"],
}

# A task title combining two drivers can use either "&" or "|" as the
# separator (we've seen both in real use), so we split on whichever one
# actually appears in the title.
CO_DRIVER_SEPARATOR_PATTERN = re.compile(r"[&|]")

# Matches runs of whitespace, so "Wismick  Augustin" (two spaces) and
# "Wismick Augustin" (one space) are treated as the same name.
_WHITESPACE_PATTERN = re.compile(r"\s+")


def normalize_name(name):
    """Lowercase, trim, and collapse internal whitespace so trivial
    formatting differences (extra spaces) don't break a name match."""
    return _WHITESPACE_PATTERN.sub(" ", (name or "").strip()).lower()


def word_sort_key(name):
    """A fallback match key: same words, regardless of order - catches
    names entered as "Last First" in one system and "First Last" in the
    other (e.g. "St Jean Moise" vs "Moise St Jean")."""
    return " ".join(sorted(normalize_name(name).split()))


# Keeps letters, digits, spaces, and "&" - strips everything else (emoji,
# stars, parentheses, punctuation). Company/section names in Asana are often
# decorated with things like "Top Notch Truckers INC ⭐️", which otherwise
# would never exact-match Factor ELD's plain "Top Notch Truckers INC".
_NON_NAME_CHARACTERS_PATTERN = re.compile(r"[^\w\s&]", re.UNICODE)


def vehicle_field_value(field_type, raw_value):
    """Convert a raw Factor ELD vehicle number into whatever value this
    project's vehicle number field should store: a "#"-prefixed string for
    a Text field (e.g. "2130" -> "#2130", "003" -> "#003" - leading zeros
    preserved), or a plain integer for a legacy Number field (leading zeros
    dropped). Returns None if there's nothing usable (blank), or if
    field_type is None (this project has no vehicle number field at all)."""
    if not field_type or raw_value is None:
        return None
    cleaned = str(raw_value).strip()
    if not cleaned:
        return None
    if field_type == "number":
        try:
            return int(cleaned)
        except ValueError:
            return None
    return f"#{cleaned}"


def normalize_company_name(name):
    """Lowercase, strip decorative symbols/emoji, and collapse whitespace,
    so a section named "Top Notch Truckers INC ⭐️" still matches a
    company named plain "Top Notch Truckers INC"."""
    cleaned = _NON_NAME_CHARACTERS_PATTERN.sub("", name or "")
    return _WHITESPACE_PATTERN.sub(" ", cleaned).strip().lower()


# Any task whose title contains this phrase is a flagged-exception task,
# not a normal driver roster entry - we never match or update these.
IGNORED_TASK_PHRASE = "has a problem"

MAX_RETRIES = 3
RETRY_DELAY_SECONDS = 2


class AsanaClient:
    def __init__(self, token, project_ids, logger):
        self.project_ids = project_ids
        self.logger = logger
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        })
        # Cache of per-project info (status field + its dropdown options),
        # so we only look it up once per sync run instead of once per driver.
        self._project_config_cache = {}
        # Same idea, for the separate "Database" board's field layout and
        # per-company sections (see the database-board methods near the end
        # of this file).
        self._database_config_cache = {}
        self._database_section_cache = {}
        # Same idea, for the separate "Odometer Jump" board (see the
        # odometer-board methods near the end of this file).
        self._odometer_config_cache = {}
        self._odometer_section_cache = {}

    # ---------- low-level HTTP helper with simple retries ----------

    def _request(self, method, url, **kwargs):
        """Send one HTTP request to Asana, retrying a few times on network
        or server errors before giving up. Also retries on 429 (rate
        limited), honoring Asana's Retry-After header - needed for
        onboarding's board-bootstrap burst of POSTs, which fires far more
        requests in a row than steady-state syncing ever does."""
        last_error = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                resp = self.session.request(method, url, timeout=30, **kwargs)
                if resp.status_code == 429:
                    retry_after = float(resp.headers.get("Retry-After", RETRY_DELAY_SECONDS))
                    self.logger.warning(
                        "Asana rate-limited us (attempt %s/%s) - waiting %ss.",
                        attempt, MAX_RETRIES, retry_after,
                    )
                    if attempt < MAX_RETRIES:
                        time.sleep(retry_after)
                        continue
                    raise requests.HTTPError("Asana rate limit (429) - out of retries")
                if resp.status_code >= 500:
                    raise requests.HTTPError(f"Asana server error {resp.status_code}")
                resp.raise_for_status()
                return resp.json()
            except requests.RequestException as exc:
                last_error = exc
                self.logger.warning(
                    "Asana request failed (attempt %s/%s): %s",
                    attempt, MAX_RETRIES, exc,
                )
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_DELAY_SECONDS)
        raise last_error

    # ---------- discovering each project's duty-status field ----------

    def _get_project_config(self, project_id):
        if project_id in self._project_config_cache:
            return self._project_config_cache[project_id]

        project = self._request("GET", f"{ASANA_API_BASE}/projects/{project_id}")
        project_name = project["data"]["name"]

        settings = self._request(
            "GET",
            f"{ASANA_API_BASE}/projects/{project_id}/custom_field_settings"
            "?opt_fields=custom_field.name,custom_field.resource_subtype,"
            "custom_field.enum_options.name,custom_field.enum_options.gid,"
            "custom_field.enum_options.enabled",
        )

        field_gid = None
        field_name = None
        options = {}
        vehicle_field_gid = None
        vehicle_field_type = None
        violation_field_gid = None
        violation_options = {}
        staff_id_field_gid = None
        staff_id_options = {}
        staff_history_field_gid = None
        staff_history_options = {}
        for setting in settings["data"]:
            cf = setting["custom_field"]
            subtype = cf.get("resource_subtype")
            name_lower = cf["name"].strip().lower()

            if subtype == "enum" and field_gid is None and name_lower in STATUS_FIELD_NAME_CANDIDATES:
                field_gid = cf["gid"]
                field_name = cf["name"]
                for opt in cf.get("enum_options", []):
                    if not opt.get("enabled", True):
                        # A disabled option can't actually be set - Asana
                        # rejects the whole request with a 400 if we try.
                        # Treating it as if it doesn't exist at all lets the
                        # normal "no matching dropdown option" warning handle
                        # it instead of failing the update outright.
                        continue
                    # Index both the exact name and a lower-cased version,
                    # so an exact match is tried first and a case-insensitive
                    # match is the fallback.
                    options[opt["name"]] = opt["gid"]
                    options[opt["name"].strip().lower()] = opt["gid"]
                    # Also register our own standard status word as an
                    # alias, if this option is a recognized abbreviation of
                    # it (see STATUS_ABBREVIATION_ALIASES) - lets us write
                    # to a project whose options are abbreviated instead of
                    # spelled out in full.
                    alias = STATUS_ABBREVIATION_ALIASES.get(_clean_option_label(opt["name"]))
                    if alias:
                        options[alias] = opt["gid"]
            elif subtype in ("text", "number") and vehicle_field_gid is None and name_lower in VEHICLE_FIELD_NAME_CANDIDATES:
                vehicle_field_gid = cf["gid"]
                vehicle_field_type = subtype
            elif subtype == "enum" and violation_field_gid is None and name_lower in VIOLATION_FIELD_NAME_CANDIDATES:
                violation_field_gid = cf["gid"]
                for opt in cf.get("enum_options", []):
                    if not opt.get("enabled", True):
                        continue
                    violation_options[opt["name"]] = opt["gid"]
                    violation_options[opt["name"].strip().lower()] = opt["gid"]
            elif subtype == "enum" and staff_id_field_gid is None and name_lower in STAFF_ID_FIELD_NAME_CANDIDATES:
                staff_id_field_gid = cf["gid"]
                for opt in cf.get("enum_options", []):
                    if not opt.get("enabled", True):
                        continue
                    staff_id_options[opt["name"]] = opt["gid"]
                    staff_id_options[opt["name"].strip().lower()] = opt["gid"]
            elif subtype == "enum" and staff_history_field_gid is None and name_lower in STAFF_HISTORY_FIELD_NAME_CANDIDATES:
                staff_history_field_gid = cf["gid"]
                for opt in cf.get("enum_options", []):
                    if not opt.get("enabled", True):
                        continue
                    staff_history_options[opt["name"]] = opt["gid"]
                    staff_history_options[opt["name"].strip().lower()] = opt["gid"]

        if field_gid is None:
            raise RuntimeError(
                f"Could not find a duty-status dropdown field in Asana "
                f"project '{project_name}' ({project_id}). Looked for a "
                f"field named one of: {STATUS_FIELD_NAME_CANDIDATES}"
            )

        config = {
            "project_id": project_id,
            "name": project_name,
            "field_gid": field_gid,
            "field_name": field_name,
            "options": options,
            "vehicle_field_gid": vehicle_field_gid,
            "vehicle_field_type": vehicle_field_type,
            "violation_field_gid": violation_field_gid,
            "violation_options": violation_options,
            "staff_id_field_gid": staff_id_field_gid,
            "staff_id_options": staff_id_options,
            "staff_history_field_gid": staff_history_field_gid,
            "staff_history_options": staff_history_options,
        }
        self._project_config_cache[project_id] = config
        return config

    def _option_gid_for_status(self, options, target_status):
        """Look up the dropdown option gid for a target status word. Tries
        an exact match first, then falls back to a case-insensitive match
        (some projects have the option spelled slightly differently, e.g.
        "ON DUTY" instead of "On Duty")."""
        if target_status in options:
            return options[target_status]
        return options.get(target_status.strip().lower())

    # ---------- fetching and indexing tasks ----------

    def _fetch_all_tasks(self, project_id):
        """Fetch every task in a project, following Asana's pagination
        until there are no more pages."""
        tasks = []
        url = (
            f"{ASANA_API_BASE}/projects/{project_id}/tasks"
            "?opt_fields=name,custom_fields.name,custom_fields.enum_value.name,"
            "custom_fields.number_value,custom_fields.text_value,"
            "memberships.section.gid,memberships.section.name,memberships.project.gid"
            "&limit=100"
        )
        while url:
            page = self._request("GET", url)
            tasks.extend(page["data"])
            next_page = page.get("next_page")
            url = next_page["uri"] if next_page else None
        return tasks

    @staticmethod
    def _read_custom_field_values(task, config):
        """Pull the current status (enum), vehicle number (text or number,
        depending on the project), violation (enum), and staff ID / staff ID
        history (both enum) values out of one task's custom_fields, using
        the field gids this project's config already found. Returns
        (current_status, current_vehicle_number, current_violation,
        current_staff_id, current_staff_history) - any can be None (field
        blank, or this project doesn't have that field at all)."""
        current_status = None
        current_vehicle_number = None
        current_violation = None
        current_staff_id = None
        current_staff_history = None
        for cf in task.get("custom_fields", []):
            if cf["gid"] == config["field_gid"]:
                if cf.get("enum_value"):
                    current_status = cf["enum_value"]["name"]
            elif config.get("vehicle_field_gid") and cf["gid"] == config["vehicle_field_gid"]:
                if config.get("vehicle_field_type") == "text":
                    current_vehicle_number = cf.get("text_value")
                else:
                    current_vehicle_number = cf.get("number_value")
            elif config.get("violation_field_gid") and cf["gid"] == config["violation_field_gid"]:
                if cf.get("enum_value"):
                    current_violation = cf["enum_value"]["name"]
            elif config.get("staff_id_field_gid") and cf["gid"] == config["staff_id_field_gid"]:
                if cf.get("enum_value"):
                    current_staff_id = cf["enum_value"]["name"]
            elif config.get("staff_history_field_gid") and cf["gid"] == config["staff_history_field_gid"]:
                if cf.get("enum_value"):
                    current_staff_history = cf["enum_value"]["name"]
        return (
            current_status, current_vehicle_number, current_violation,
            current_staff_id, current_staff_history,
        )

    def build_task_index(self):
        """
        Fetch every task from every configured project and build a lookup
        table from driver name -> list of matching tasks.

        A task whose title combines two names with "&" or "|" (e.g.
        "Ahmet Garayev & Mikail Jebril") is a co-driver task - two people
        sharing one vehicle and one Asana task. It's left out of the normal
        by-name index (a single name should never accidentally match half of
        a combined title) and returned separately as combined_tasks instead,
        with the same status/vehicle-number info a normal match has, so
        sync.py can update it directly when the pairing is still valid, or
        delete it once the pairing has dissolved (see sync.py).

        Returns a tuple: (index, fallback_index, combined_tasks)
          index          : { normalized_driver_name: [task_match, ...] }
          fallback_index : { word_sort_key: [task_match, ...] } - same
                            words in any order, e.g. "moise st jean" and
                            "st jean moise" both produce the same key. Only
                            used when the primary index has no exact match,
                            to catch names entered in a different word order
                            between Factor ELD and Asana.
          combined_tasks : [ {project_id, project_name, task_gid, task_title,
                               field_gid, options, current_status,
                               vehicle_field_gid, current_vehicle_number,
                               names: [driver name, ...]}, ... ]
        """
        index = {}
        fallback_index = {}
        combined_tasks = []
        for project_id in self.project_ids:
            config = self._get_project_config(project_id)
            tasks = self._fetch_all_tasks(project_id)
            for task in tasks:
                title = (task.get("name") or "").strip()
                if not title:
                    continue
                if IGNORED_TASK_PHRASE in title.lower():
                    continue

                names_in_title = [
                    n.strip() for n in CO_DRIVER_SEPARATOR_PATTERN.split(title) if n.strip()
                ]
                (
                    current_status, current_vehicle_number, current_violation,
                    current_staff_id, current_staff_history,
                ) = self._read_custom_field_values(task, config)
                current_section_gid, current_section_name = self._current_section_for_project(
                    task, project_id
                )

                if len(names_in_title) > 1:
                    combined_tasks.append({
                        "project_id": project_id,
                        "project_name": config["name"],
                        "task_gid": task["gid"],
                        "task_title": title,
                        "field_gid": config["field_gid"],
                        "options": config["options"],
                        "current_status": current_status,
                        "vehicle_field_gid": config.get("vehicle_field_gid"),
                        "vehicle_field_type": config.get("vehicle_field_type"),
                        "current_vehicle_number": current_vehicle_number,
                        "violation_field_gid": config.get("violation_field_gid"),
                        "violation_options": config.get("violation_options", {}),
                        "current_violation": current_violation,
                        "staff_id_field_gid": config.get("staff_id_field_gid"),
                        "staff_id_options": config.get("staff_id_options", {}),
                        "current_staff_id": current_staff_id,
                        "staff_history_field_gid": config.get("staff_history_field_gid"),
                        "staff_history_options": config.get("staff_history_options", {}),
                        "current_staff_history": current_staff_history,
                        "current_section_gid": current_section_gid,
                        "current_section_name": current_section_name,
                        "names": names_in_title,
                    })
                    continue

                match = {
                    "project_id": project_id,
                    "project_name": config["name"],
                    "task_gid": task["gid"],
                    "task_title": title,
                    "field_gid": config["field_gid"],
                    "options": config["options"],
                    "current_status": current_status,
                    "vehicle_field_gid": config.get("vehicle_field_gid"),
                    "vehicle_field_type": config.get("vehicle_field_type"),
                    "current_vehicle_number": current_vehicle_number,
                    "violation_field_gid": config.get("violation_field_gid"),
                    "violation_options": config.get("violation_options", {}),
                    "current_violation": current_violation,
                    "staff_id_field_gid": config.get("staff_id_field_gid"),
                    "staff_id_options": config.get("staff_id_options", {}),
                    "current_staff_id": current_staff_id,
                    "staff_history_field_gid": config.get("staff_history_field_gid"),
                    "staff_history_options": config.get("staff_history_options", {}),
                    "current_staff_history": current_staff_history,
                    "current_section_gid": current_section_gid,
                    "current_section_name": current_section_name,
                }

                index.setdefault(normalize_name(title), []).append(match)
                fallback_index.setdefault(word_sort_key(title), []).append(match)

        return index, fallback_index, combined_tasks

    @staticmethod
    def _current_section_for_project(task, project_id):
        """Find which section a task currently sits in, within one specific
        project (a task can technically belong to more than one project,
        though ours never intentionally do - this only looks at the
        membership for the project we're actually iterating). Returns
        (section_gid, section_name), both None if somehow unsectioned."""
        for membership in task.get("memberships", []):
            project = membership.get("project") or {}
            if project.get("gid") == project_id:
                section = membership.get("section") or {}
                return section.get("gid"), section.get("name")
        return None, None

    def delete_task(self, task_gid):
        """Permanently delete a task. Only ever called on old combined
        (co-driver) tasks, and only after the split-off driver's own
        replacement task has been confirmed to exist - see sync.py."""
        self._request("DELETE", f"{ASANA_API_BASE}/tasks/{task_gid}")

    def move_task_to_section(self, task_gid, current_project_id, new_section_info):
        """Move an existing driver task into a different company's section -
        used when a driver's company assignment changes in Factor ELD (e.g.
        moved carriers) and their Asana task is still sitting under the old
        company. Handles both a same-project section change (just re-add to
        the new section - Asana treats section membership within one
        project as exclusive, so this moves it out of the old one
        automatically) and a full cross-project move (add to the new
        project/section, then remove the old project membership so the task
        doesn't linger in two places)."""
        if new_section_info["project_id"] == current_project_id:
            self._request(
                "POST",
                f"{ASANA_API_BASE}/sections/{new_section_info['section_gid']}/addTask",
                json={"data": {"task": task_gid}},
            )
        else:
            self._request(
                "POST",
                f"{ASANA_API_BASE}/tasks/{task_gid}/addProject",
                json={"data": {
                    "project": new_section_info["project_id"],
                    "section": new_section_info["section_gid"],
                }},
            )
            self._request(
                "POST",
                f"{ASANA_API_BASE}/tasks/{task_gid}/removeProject",
                json={"data": {"project": current_project_id}},
            )

    # ---------- finding where to create a brand-new driver's task ----------

    def _fetch_sections(self, project_id):
        page = self._request(
            "GET", f"{ASANA_API_BASE}/projects/{project_id}/sections"
        )
        return page["data"]

    def build_section_index(self):
        """
        Fetch every section (company name) from every configured project and
        build a lookup table from company name -> where to create a new
        task for a driver in that company.

        Returns a dict: { normalized_company_name: {project_id, project_name,
        section_gid, section_name} }

        We only use existing sections as placement targets - if a driver's
        company has no section anywhere yet, we deliberately don't guess
        which project to put it in (see sync.py).
        """
        index = {}
        for project_id in self.project_ids:
            config = self._get_project_config(project_id)
            for section in self._fetch_sections(project_id):
                name = (section.get("name") or "").strip()
                if not name:
                    continue
                index[normalize_company_name(name)] = {
                    "project_id": project_id,
                    "project_name": config["name"],
                    "section_gid": section["gid"],
                    "section_name": name,
                }
        return index

    def get_project_names(self, project_ids=None):
        """Return {project_id: display_name} for the given project ids (or
        self.project_ids if omitted). Reuses whatever's already cached in
        _project_config_cache - if build_section_index()/build_task_index()
        already ran this cycle (they always call _get_project_config per
        project), this costs zero extra API calls. Used for the new-company
        alert's board-choice button labels (see sync.py/pending_companies.py) -
        "Texas A"/"Texas B" reads much better than a bare project id."""
        project_ids = project_ids if project_ids is not None else self.project_ids
        return {pid: self._get_project_config(pid)["name"] for pid in project_ids}

    def create_section(self, project_id, name):
        """Create a brand-new section in ANY project (unlike
        _get_or_create_database_section/_get_or_create_odometer_section,
        this doesn't check for an existing one first, and isn't scoped to
        those two boards) - used when a team's admin explicitly picks a
        dispatch board for a previously-unassigned company via the control
        bot's inline buttons (see control_bot/router.py's
        _handle_assign_callback). Returns the new section's gid."""
        section = self._request(
            "POST",
            f"{ASANA_API_BASE}/projects/{project_id}/sections",
            json={"data": {"name": name}},
        )
        return section["data"]["gid"]

    def _stage_enum_option(self, custom_fields, field_gid, options, value, project_name, field_label, task_label):
        """Look up value in one enum field's options and, if found, stage it
        into custom_fields for the next PUT. If field_gid is missing (this
        project doesn't have that field) or value is None (nothing to set),
        does nothing. If value is given but doesn't match any option, logs a
        warning and leaves the field untouched rather than guessing."""
        if not field_gid or value is None:
            return
        option_gid = options.get(value) or options.get(value.strip().lower())
        if option_gid is not None:
            custom_fields[field_gid] = option_gid
        else:
            self.logger.warning(
                "Project '%s': %s '%s' has no matching dropdown option - "
                "leaving it blank for task '%s'",
                project_name, field_label, value, task_label,
            )

    def create_task_for_driver(self, name, status, vehicle_number, violation, staff_id, staff_history, section_info):
        """Create a brand-new task for a driver (or co-driver pair) who
        doesn't have one yet, place it in the given company section, and set
        its initial status, vehicle number, violation, and staff ID fields
        (whichever the project actually has). Returns the new task's gid."""
        config = self._get_project_config(section_info["project_id"])

        task = self._request(
            "POST",
            f"{ASANA_API_BASE}/tasks",
            json={"data": {"name": name, "projects": [section_info["project_id"]]}},
        )
        task_gid = task["data"]["gid"]

        self._request(
            "POST",
            f"{ASANA_API_BASE}/sections/{section_info['section_gid']}/addTask",
            json={"data": {"task": task_gid}},
        )

        custom_fields = {}
        option_gid = self._option_gid_for_status(config["options"], status)
        if option_gid is not None:
            custom_fields[config["field_gid"]] = option_gid

        if config.get("vehicle_field_gid"):
            value = vehicle_field_value(config.get("vehicle_field_type"), vehicle_number)
            if value is not None:
                custom_fields[config["vehicle_field_gid"]] = value

        self._stage_enum_option(
            custom_fields, config.get("violation_field_gid"), config["violation_options"],
            violation, config["name"], "violation", name,
        )
        self._stage_enum_option(
            custom_fields, config.get("staff_id_field_gid"), config["staff_id_options"],
            staff_id, config["name"], "Staff ID", name,
        )
        self._stage_enum_option(
            custom_fields, config.get("staff_history_field_gid"), config["staff_history_options"],
            staff_history, config["name"], "Staff ID History", name,
        )

        if custom_fields:
            self._request(
                "PUT",
                f"{ASANA_API_BASE}/tasks/{task_gid}",
                json={"data": {"custom_fields": custom_fields}},
            )

        return task_gid

    # ---------- updating a task's status ----------

    def update_task_status(
        self, match, new_status, new_vehicle_number=None, new_violation=None,
        new_staff_id=None, new_staff_history=None,
    ):
        """Set one task's duty-status dropdown to new_status, and (if this
        project has the relevant field) its vehicle number, violation,
        and/or staff ID fields too, in a single request. Returns True if the
        status update was applied, False if the status word had no matching
        dropdown option (logged as a warning in that case) - a missing/
        unchanged vehicle number, violation, or staff ID never blocks the
        status update.

        new_violation being None means "no currently-active violation" and
        will CLEAR the field if it previously had a value - unlike status,
        blank is a normal, expected state for this field.

        new_staff_id/new_staff_history being None means "couldn't determine
        who edited this" (unrecognized editor, or the lookup failed) rather
        than "no one edited it" - unlike violation, this leaves whatever was
        there before untouched instead of clearing it."""
        custom_fields = {}
        status_applied = False

        option_gid = self._option_gid_for_status(match["options"], new_status)
        if option_gid is None:
            self.logger.warning(
                "Project '%s': status '%s' has no matching dropdown option - "
                "skipping update for task '%s'",
                match["project_name"], new_status, match["task_title"],
            )
        else:
            custom_fields[match["field_gid"]] = option_gid
            status_applied = True

        if match.get("vehicle_field_gid") and new_vehicle_number is not None:
            value = vehicle_field_value(match.get("vehicle_field_type"), new_vehicle_number)
            if value is not None and value != match.get("current_vehicle_number"):
                custom_fields[match["vehicle_field_gid"]] = value

        if match.get("violation_field_gid") and new_violation != match.get("current_violation"):
            if new_violation is None:
                custom_fields[match["violation_field_gid"]] = None
            else:
                violation_option_gid = (
                    match["violation_options"].get(new_violation)
                    or match["violation_options"].get(new_violation.strip().lower())
                )
                if violation_option_gid is None:
                    self.logger.warning(
                        "Project '%s': violation '%s' has no matching dropdown "
                        "option - skipping update for task '%s'",
                        match["project_name"], new_violation, match["task_title"],
                    )
                else:
                    custom_fields[match["violation_field_gid"]] = violation_option_gid

        if (
            match.get("staff_id_field_gid") and new_staff_id is not None
            and new_staff_id != match.get("current_staff_id")
        ):
            self._stage_enum_option(
                custom_fields, match["staff_id_field_gid"], match["staff_id_options"],
                new_staff_id, match["project_name"], "Staff ID", match["task_title"],
            )

        if (
            match.get("staff_history_field_gid") and new_staff_history is not None
            and new_staff_history != match.get("current_staff_history")
        ):
            self._stage_enum_option(
                custom_fields, match["staff_history_field_gid"], match["staff_history_options"],
                new_staff_history, match["project_name"], "Staff ID History", match["task_title"],
            )

        if custom_fields:
            url = f"{ASANA_API_BASE}/tasks/{match['task_gid']}"
            self._request("PUT", url, json={"data": {"custom_fields": custom_fields}})

        return status_applied

    # ---------- the standalone "Database" board ----------
    #
    # A permanent, all-companies reference list of every driver's contact
    # info (email, phone, CDL, login, etc.), grouped into per-company
    # sections just like the dispatch boards - but otherwise different in
    # every way that matters: one project (not three), a section is auto-
    # created the first time a company is seen (the dispatch boards
    # deliberately never do this - see build_section_index - but this board
    # exists specifically to hold every driver, so there's no "not
    # display-worthy yet" company to gatekeep), matched by (company name,
    # driver name) instead of a hidden ID field (confirmed you don't want an
    # extra "Driver ID" column here), and tasks are NEVER deleted, even once
    # a driver goes inactive or leaves - see sync.py's run_database_cycle.
    #
    # One consequence of matching on (company, name) instead of a stable ID:
    # if a driver switches companies, their old row stays right where it is
    # (under their old company's section) and a new row is created under the
    # new one - the old row is never updated or deleted. For a board whose
    # whole point is "never drop anything," that's treated as a feature, not
    # a bug: it's a running history of which companies a driver has been at,
    # not just a mirror of where they are today.

    def _get_database_project_config(self, project_id):
        if project_id in self._database_config_cache:
            return self._database_config_cache[project_id]

        project = self._request("GET", f"{ASANA_API_BASE}/projects/{project_id}")
        project_name = project["data"]["name"]

        settings = self._request(
            "GET",
            f"{ASANA_API_BASE}/projects/{project_id}/custom_field_settings"
            "?opt_fields=custom_field.name,custom_field.resource_subtype,custom_field.gid",
        )
        field_gids = {}
        for setting in settings["data"]:
            cf = setting["custom_field"]
            if cf.get("resource_subtype") != "text":
                continue
            name_lower = cf["name"].strip().lower()
            for key, candidates in DATABASE_FIELD_NAME_CANDIDATES.items():
                if key not in field_gids and name_lower in candidates:
                    field_gids[key] = cf["gid"]

        missing = [k for k in DATABASE_FIELD_NAME_CANDIDATES if k not in field_gids]
        if missing:
            raise RuntimeError(
                f"Database board project '{project_name}' ({project_id}) is "
                f"missing expected custom field(s): {missing}"
            )

        config = {
            "project_id": project_id,
            "name": project_name,
            "field_gids": field_gids,
        }
        self._database_config_cache[project_id] = config
        return config

    def _get_or_create_database_section(self, project_id, company_name):
        """Look up (by normalized name) which section a company already has
        in the Database board, creating a brand-new section for it if this
        is the first time we've seen it. Section gids are cached per project
        for the rest of this sync run."""
        cache = self._database_section_cache.setdefault(project_id, {})
        if not cache:
            for section in self._fetch_sections(project_id):
                name = (section.get("name") or "").strip()
                if name:
                    cache[normalize_company_name(name)] = section["gid"]

        display_name = (company_name or "Unknown Company").strip() or "Unknown Company"
        key = normalize_company_name(display_name)
        section_gid = cache.get(key)
        if section_gid is not None:
            return section_gid

        section = self._request(
            "POST",
            f"{ASANA_API_BASE}/projects/{project_id}/sections",
            json={"data": {"name": display_name}},
        )
        section_gid = section["data"]["gid"]
        cache[key] = section_gid
        self.logger.info(
            "Database board: created new section '%s' (first driver seen "
            "there).", display_name,
        )
        return section_gid

    def build_database_task_index(self, project_id):
        """Return {(normalized_company_name, normalized_driver_name):
        {"task_gid", "task_title", "current": {field_key: value, ...}}} for
        every task already in the Database board, scoped by which section
        (company) each task currently sits in."""
        config = self._get_database_project_config(project_id)
        reverse_field_gids = {gid: key for key, gid in config["field_gids"].items()}

        tasks = []
        url = (
            f"{ASANA_API_BASE}/projects/{project_id}/tasks"
            "?opt_fields=name,custom_fields.text_value,"
            "memberships.section.gid,memberships.section.name,memberships.project.gid"
            "&limit=100"
        )
        while url:
            page = self._request("GET", url)
            tasks.extend(page["data"])
            next_page = page.get("next_page")
            url = next_page["uri"] if next_page else None

        index = {}
        for task in tasks:
            title = (task.get("name") or "").strip()
            if not title:
                continue
            _section_gid, section_name = self._current_section_for_project(task, project_id)
            current = {}
            for cf in task.get("custom_fields", []):
                key = reverse_field_gids.get(cf["gid"])
                if key is not None:
                    current[key] = cf.get("text_value")
            index[(normalize_company_name(section_name or ""), normalize_name(title))] = {
                "task_gid": task["gid"],
                "task_title": title,
                "current": current,
            }
        return index

    def _database_desired_values(self, record):
        return {
            "co_driver": record.co_driver_name or None,
            "vehicle_number": vehicle_field_value("text", record.vehicle_number),
            "email": record.email or None,
            "phone_number": record.phone_number or None,
            "cdl": record.cdl or None,
            "state": record.state or None,
            "login": record.login or None,
            # Password mirrors Login every sync - Factor ELD has no real
            # password field (confirmed - not present anywhere in its driver
            # data), this is your team's own username=password convention.
            "password": record.login or None,
        }

    # Leader ELD / Factor ELD frequently return "" for these two fields even
    # when a real value has been entered by hand in Asana - never let a
    # blank upstream value clobber a manually-entered one.
    _SKIP_WHEN_SOURCE_EMPTY = {"email", "phone_number"}

    def create_database_task(self, project_id, record):
        """Create a brand-new Database board task for a driver who doesn't
        have one yet, under their company's section (creating that section
        if it doesn't exist yet). Returns the new task's gid."""
        config = self._get_database_project_config(project_id)
        field_gids = config["field_gids"]
        section_gid = self._get_or_create_database_section(project_id, record.company_name)

        task = self._request(
            "POST",
            f"{ASANA_API_BASE}/tasks",
            json={"data": {"name": record.name, "projects": [project_id]}},
        )
        task_gid = task["data"]["gid"]

        self._request(
            "POST",
            f"{ASANA_API_BASE}/sections/{section_gid}/addTask",
            json={"data": {"task": task_gid}},
        )

        custom_fields = {
            field_gids[key]: value
            for key, value in self._database_desired_values(record).items()
            if value is not None or key not in self._SKIP_WHEN_SOURCE_EMPTY
        }
        self._request(
            "PUT",
            f"{ASANA_API_BASE}/tasks/{task_gid}",
            json={"data": {"custom_fields": custom_fields}},
        )
        return task_gid

    def update_database_task(self, project_id, existing, record):
        """Update an existing Database board task's fields (and its name, if
        the driver's name changed upstream) to match record. Only ever
        writes fields that actually changed. Returns True if anything was
        updated, False if everything already matched. Never deletes, and
        never moves a task between sections - see the module-level note
        above this class's database-board methods for why."""
        config = self._get_database_project_config(project_id)
        field_gids = config["field_gids"]

        custom_fields = {}
        for key, new_value in self._database_desired_values(record).items():
            if new_value is None and key in self._SKIP_WHEN_SOURCE_EMPTY:
                continue
            if existing["current"].get(key) != new_value:
                custom_fields[field_gids[key]] = new_value

        name_changed = existing["task_title"] != record.name
        if not custom_fields and not name_changed:
            return False

        data = {}
        if custom_fields:
            data["custom_fields"] = custom_fields
        if name_changed:
            data["name"] = record.name
        self._request(
            "PUT", f"{ASANA_API_BASE}/tasks/{existing['task_gid']}", json={"data": data},
        )
        return True

    # ---------- the standalone "Odometer Jump" board(s) ----------
    #
    # A per-company-sectioned list of drivers who currently have an active
    # odometer problem ("Odometer jump" or "Odometer is missing"). One
    # separate project per dispatch board (Texas A/B/C each get their own
    # "<board> Odometer Jump" project - see sync.py's _sync_odometer_board,
    # which resolves which project a company's issue belongs to via
    # build_section_index()); within each, sections are company names,
    # exactly like the dispatch boards themselves. A company not yet
    # placed on any dispatch board is skipped rather than guessed at.
    # Unlike the Database board, a task in one of these projects only
    # exists while Factor/Leader ELD is still reporting the problem - the
    # moment it's fixed there, the task is deleted, the same way the
    # dispatch boards delete a task once a driver is no longer visible.

    def _get_odometer_project_config(self, project_id):
        if project_id in self._odometer_config_cache:
            return self._odometer_config_cache[project_id]

        project = self._request("GET", f"{ASANA_API_BASE}/projects/{project_id}")
        project_name = project["data"]["name"]

        settings = self._request(
            "GET",
            f"{ASANA_API_BASE}/projects/{project_id}/custom_field_settings"
            "?opt_fields=custom_field.name,custom_field.resource_subtype,"
            "custom_field.gid,custom_field.enum_options.name,"
            "custom_field.enum_options.gid,custom_field.enum_options.enabled",
        )
        field_gid = None
        options = {}
        date_field_gid = None
        for setting in settings["data"]:
            cf = setting["custom_field"]
            name_lower = cf["name"].strip().lower()
            if cf.get("resource_subtype") == "enum" and name_lower in ODOMETER_FIELD_NAME_CANDIDATES:
                field_gid = cf["gid"]
                for opt in cf.get("enum_options", []):
                    if not opt.get("enabled", True):
                        continue
                    options[opt["name"]] = opt["gid"]
                    options[opt["name"].strip().lower()] = opt["gid"]
            elif cf.get("resource_subtype") == "text" and name_lower in ODOMETER_DATE_FIELD_NAME_CANDIDATES:
                date_field_gid = cf["gid"]

        if field_gid is None:
            raise RuntimeError(
                f"Could not find an 'Odometer' dropdown field in Asana "
                f"project '{project_name}' ({project_id})."
            )

        config = {
            "project_id": project_id,
            "name": project_name,
            "field_gid": field_gid,
            "options": options,
            "date_field_gid": date_field_gid,
        }
        self._odometer_config_cache[project_id] = config
        return config

    def _get_or_create_odometer_section(self, project_id, company_name):
        """Same idea as _get_or_create_database_section - look up (by
        normalized name) which section a company already has in this
        project, creating a new one the first time it's seen."""
        cache = self._odometer_section_cache.setdefault(project_id, {})
        if not cache:
            for section in self._fetch_sections(project_id):
                name = (section.get("name") or "").strip()
                if name:
                    cache[normalize_company_name(name)] = section["gid"]

        display_name = (company_name or "Unknown Company").strip() or "Unknown Company"
        key = normalize_company_name(display_name)
        section_gid = cache.get(key)
        if section_gid is not None:
            return section_gid

        section = self._request(
            "POST",
            f"{ASANA_API_BASE}/projects/{project_id}/sections",
            json={"data": {"name": display_name}},
        )
        section_gid = section["data"]["gid"]
        cache[key] = section_gid
        self.logger.info(
            "Odometer Jump board: created new section '%s' (first driver "
            "seen there).", display_name,
        )
        return section_gid

    def build_odometer_task_index(self, project_id):
        """Return {(normalized_company_name, normalized_driver_name):
        {"task_gid", "task_title", "current_odometer", "current_date"}} for
        every task already in this Odometer Jump project, scoped by which
        section (company) each task currently sits in - same matching
        approach as the Database board (no hidden id field, matched by
        company+name)."""
        config = self._get_odometer_project_config(project_id)

        tasks = []
        url = (
            f"{ASANA_API_BASE}/projects/{project_id}/tasks"
            "?opt_fields=name,custom_fields.name,custom_fields.enum_value.name,"
            "custom_fields.text_value,"
            "memberships.section.gid,memberships.section.name,memberships.project.gid"
            "&limit=100"
        )
        while url:
            page = self._request("GET", url)
            tasks.extend(page["data"])
            next_page = page.get("next_page")
            url = next_page["uri"] if next_page else None

        index = {}
        for task in tasks:
            title = (task.get("name") or "").strip()
            if not title:
                continue
            _section_gid, section_name = self._current_section_for_project(task, project_id)
            current_odometer = None
            current_date = None
            for cf in task.get("custom_fields", []):
                if cf["gid"] == config["field_gid"] and cf.get("enum_value"):
                    current_odometer = cf["enum_value"]["name"]
                elif config.get("date_field_gid") and cf["gid"] == config["date_field_gid"]:
                    current_date = cf.get("text_value")
            index[(normalize_company_name(section_name or ""), normalize_name(title))] = {
                "task_gid": task["gid"],
                "task_title": title,
                "current_odometer": current_odometer,
                "current_date": current_date,
            }
        return index

    def create_odometer_task(self, project_id, company_name, driver_name, issue_type, occurred_at=None):
        """Create a brand-new Odometer Jump task for a driver who just
        started having an active odometer problem, under their company's
        section (creating it if needed). Returns the new task's gid."""
        config = self._get_odometer_project_config(project_id)
        section_gid = self._get_or_create_odometer_section(project_id, company_name)

        task = self._request(
            "POST",
            f"{ASANA_API_BASE}/tasks",
            json={"data": {"name": driver_name, "projects": [project_id]}},
        )
        task_gid = task["data"]["gid"]

        self._request(
            "POST",
            f"{ASANA_API_BASE}/sections/{section_gid}/addTask",
            json={"data": {"task": task_gid}},
        )

        custom_fields = {}
        option_gid = config["options"].get(issue_type) or config["options"].get(issue_type.strip().lower())
        if option_gid is None:
            self.logger.warning(
                "Project '%s': odometer issue '%s' has no matching dropdown "
                "option - leaving it blank for task '%s'",
                config["name"], issue_type, driver_name,
            )
        else:
            custom_fields[config["field_gid"]] = option_gid
        if config.get("date_field_gid") and occurred_at:
            custom_fields[config["date_field_gid"]] = occurred_at
        if custom_fields:
            self._request(
                "PUT",
                f"{ASANA_API_BASE}/tasks/{task_gid}",
                json={"data": {"custom_fields": custom_fields}},
            )
        return task_gid

    def update_odometer_task(self, project_id, existing, new_issue_type, occurred_at=None):
        """Update an existing Odometer Jump task's issue type and/or date.
        Only writes fields that actually changed from what's already
        there."""
        config = self._get_odometer_project_config(project_id)
        custom_fields = {}

        if existing.get("current_odometer") != new_issue_type:
            option_gid = config["options"].get(new_issue_type) or config["options"].get(new_issue_type.strip().lower())
            if option_gid is None:
                self.logger.warning(
                    "Project '%s': odometer issue '%s' has no matching dropdown "
                    "option - leaving task '%s' unchanged.",
                    config["name"], new_issue_type, existing["task_gid"],
                )
            else:
                custom_fields[config["field_gid"]] = option_gid

        if config.get("date_field_gid") and occurred_at and existing.get("current_date") != occurred_at:
            custom_fields[config["date_field_gid"]] = occurred_at

        if custom_fields:
            self._request(
                "PUT",
                f"{ASANA_API_BASE}/tasks/{existing['task_gid']}",
                json={"data": {"custom_fields": custom_fields}},
            )
        return bool(custom_fields)

    def cleanup_empty_odometer_sections(self, project_id):
        """Delete any company section in the Odometer Jump board that's now
        empty - e.g. every driver there just got fixed and removed. Company
        names come and go here as issues appear/resolve, so an empty
        section is just leftover clutter, not a placeholder worth keeping
        (the section is simply recreated later if that company has a new
        odometer issue - see _get_or_create_odometer_section). Always
        leaves at least one section behind, since Asana requires a board
        project to have one. ODOMETER_DIVIDER_SECTION_NAMES ("Texas A"/"Texas
        B"/"Texas C") are visual-only header sections deliberately kept
        empty forever, to cluster the company sections underneath them -
        never delete those just because they're empty."""
        sections = self._fetch_sections(project_id)
        if len(sections) <= 1:
            return

        for section in sections:
            name = (section.get("name") or "").strip()
            if name in ODOMETER_DIVIDER_SECTION_NAMES:
                continue
            tasks = self._request(
                "GET", f"{ASANA_API_BASE}/sections/{section['gid']}/tasks?opt_fields=gid&limit=1"
            )
            if tasks["data"]:
                continue
            self._request("DELETE", f"{ASANA_API_BASE}/sections/{section['gid']}")
            self._odometer_section_cache.get(project_id, {}).pop(
                normalize_company_name(section.get("name") or ""), None
            )
            self.logger.info(
                "Odometer Jump board: deleted now-empty section '%s'.", section.get("name"),
            )

    # ---------- bootstrapping a brand-new team's Asana board set ----------
    #
    # Used only by control_bot's onboarding flow (see the multi-tenant
    # control panel plan), never by the steady-state sync loop. Every other
    # method in this class assumes a project's fields already exist (see the
    # *_FIELD_NAME_CANDIDATES lists at the top of this file) - these methods
    # are what actually create them for a team that's never had any of this
    # before.

    def get_current_user(self):
        """GET /users/me - lets onboarding confirm a pasted Asana token is
        actually valid, and discover which workspace(s) it can see, before
        anything gets created."""
        return self._request(
            "GET",
            f"{ASANA_API_BASE}/users/me?opt_fields=name,email,workspaces.name,workspaces.gid",
        )["data"]

    def get_workspace_info(self, workspace_gid):
        """GET /workspaces/{gid}. is_organization matters because an
        organization-tier workspace requires a Team gid to create a project
        under - a different "Team" concept than our own per-company "team",
        see get_organization_teams()."""
        return self._request(
            "GET", f"{ASANA_API_BASE}/workspaces/{workspace_gid}?opt_fields=name,is_organization",
        )["data"]

    def get_organization_teams(self, workspace_gid):
        """GET /teams?organization={gid} - only meaningful when
        get_workspace_info() reports is_organization=True."""
        return self._request(
            "GET", f"{ASANA_API_BASE}/teams?organization={workspace_gid}&opt_fields=name",
        )["data"]

    def create_project(self, workspace_gid, name, team_gid=None):
        """POST a brand-new, empty board-layout project. Returns its gid."""
        data = {"name": name, "workspace": workspace_gid, "layout": "board"}
        if team_gid:
            data["team"] = team_gid
        project = self._request("POST", f"{ASANA_API_BASE}/projects", json={"data": data})
        return project["data"]["gid"]

    def create_enum_custom_field(self, workspace_gid, name, option_names=None):
        """POST a brand-new workspace-level enum custom field, optionally
        with the given option names created in that order. Omit/empty
        option_names to create the field with no options yet - Staff ID and
        Staff ID History are never generic/template-able the way Violation
        is (confirmed live: they're literally one team's own staff roster),
        so those two get created empty here and populated per-team from
        onboarding's roster collection via add_enum_option() below. Returns
        (field_gid, {option_name: option_gid})."""
        data = {"workspace": workspace_gid, "name": name, "resource_subtype": "enum"}
        if option_names:
            data["enum_options"] = [{"name": n} for n in option_names]
        field = self._request("POST", f"{ASANA_API_BASE}/custom_fields", json={"data": data})
        field_data = field["data"]
        options = {opt["name"]: opt["gid"] for opt in field_data.get("enum_options", [])}
        return field_data["gid"], options

    def add_enum_option(self, custom_field_gid, name, color=None):
        """POST one new option onto an existing enum custom field - used by
        onboarding to populate a brand-new team's Staff ID / Staff ID
        History fields from their own roster, one entry at a time. Returns
        the new option's gid."""
        data = {"name": name}
        if color:
            data["color"] = color
        option = self._request(
            "POST",
            f"{ASANA_API_BASE}/custom_fields/{custom_field_gid}/enum_options",
            json={"data": data},
        )
        return option["data"]["gid"]

    def create_text_custom_field(self, workspace_gid, name):
        """POST a brand-new workspace-level text custom field. Returns its
        gid."""
        field = self._request(
            "POST",
            f"{ASANA_API_BASE}/custom_fields",
            json={"data": {"workspace": workspace_gid, "name": name, "resource_subtype": "text"}},
        )
        return field["data"]["gid"]

    def attach_custom_field(self, project_id, custom_field_gid, is_important=True):
        """POST addCustomFieldSetting - makes an existing custom field
        actually show up on a project. Creating the field alone (the two
        methods above) isn't enough on its own."""
        self._request(
            "POST",
            f"{ASANA_API_BASE}/projects/{project_id}/addCustomFieldSetting",
            json={"data": {"custom_field": custom_field_gid, "is_important": is_important}},
        )

    def bootstrap_dispatch_project(self, workspace_gid, name, team_gid=None):
        """Create one brand-new dispatch board (the "Texas A/B/C"-equivalent)
        from scratch for a new team: Status (Driving/Sleeping/Off Duty/On
        Duty - clean full words, not the abbreviated DR/SB/OFF/ON form one
        existing team later customized to on their own boards), Vehicle
        Number (text), Violation (Shift/Break/Cycle/PTI Violation -
        confirmed generic across every existing team's boards), and empty
        Staff ID / Staff ID History enums (populated later, per-team, from
        that team's own roster collected during onboarding - unlike
        Violation, these are NOT generic). Deliberately does not create any
        company section - same as every existing dispatch board, a team
        adds its own sections as its own companies come in. Returns the new
        project_id."""
        project_id = self.create_project(workspace_gid, name, team_gid)

        status_field_gid, _ = self.create_enum_custom_field(
            workspace_gid, "Status", ["Driving", "Sleeping", "Off Duty", "On Duty"],
        )
        self.attach_custom_field(project_id, status_field_gid)

        vehicle_field_gid = self.create_text_custom_field(workspace_gid, "Vehicle Number")
        self.attach_custom_field(project_id, vehicle_field_gid)

        violation_field_gid, _ = self.create_enum_custom_field(
            workspace_gid, "Violation",
            ["Shift Violation", "Break Violation", "Cycle Violation", "PTI Violation"],
        )
        self.attach_custom_field(project_id, violation_field_gid)

        staff_id_field_gid, _ = self.create_enum_custom_field(workspace_gid, "Staff ID")
        self.attach_custom_field(project_id, staff_id_field_gid)

        staff_history_field_gid, _ = self.create_enum_custom_field(workspace_gid, "Staff ID History")
        self.attach_custom_field(project_id, staff_history_field_gid)

        self.logger.info("Bootstrapped new dispatch board '%s' (project %s).", name, project_id)
        return project_id

    def bootstrap_database_project(self, workspace_gid, name, team_gid=None):
        """Create one brand-new Database board from scratch: the 8 text
        fields every existing Database TX task already has. Field names
        match DATABASE_FIELD_NAME_CANDIDATES exactly so the existing
        lookup-by-name logic in _get_database_project_config recognizes
        them with no further changes. Returns the new project_id."""
        project_id = self.create_project(workspace_gid, name, team_gid)
        for field_name in [
            "Co-driver", "Vehicle Id", "Email", "Phone Number",
            "CDL", "State", "Login", "Password",
        ]:
            field_gid = self.create_text_custom_field(workspace_gid, field_name)
            self.attach_custom_field(project_id, field_gid)
        self.logger.info("Bootstrapped new Database board '%s' (project %s).", name, project_id)
        return project_id

    def bootstrap_odometer_project(self, workspace_gid, name, team_gid=None):
        """Create one brand-new Odometer Jump board from scratch: the
        Odometer enum ("Odometer jump"/"Odometer is missing", confirmed the
        only two issue types eld_factor.py's FACTOR_ODOMETER_ERROR_MAP ever
        produces) and a Date and time text field. Returns the new
        project_id. Deliberately does not pre-create any company sections -
        same as the dispatch boards, a company gets its own section the
        first time it actually needs one (see
        _get_or_create_odometer_section). Call this once per dispatch
        board a new team has (see sync.py/provisioning.py) - one Odometer
        Jump project per dispatch board, not one shared project."""
        project_id = self.create_project(workspace_gid, name, team_gid)

        odometer_field_gid, _ = self.create_enum_custom_field(
            workspace_gid, "Odometer", ["Odometer jump", "Odometer is missing"],
        )
        self.attach_custom_field(project_id, odometer_field_gid)

        date_field_gid = self.create_text_custom_field(workspace_gid, "Date and time")
        self.attach_custom_field(project_id, date_field_gid)

        self.logger.info("Bootstrapped new Odometer Jump board '%s' (project %s).", name, project_id)
        return project_id
