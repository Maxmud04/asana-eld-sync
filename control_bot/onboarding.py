"""
control_bot/onboarding.py

The self-service conversation that lets a brand-new team paste in their own
Factor ELD / Leader ELD / Asana credentials and get their full board set
auto-created, with no engineer involved (see the plan's Phase 5). State is
persisted per chat_id in config_store's onboarding_sessions table, so a
control-bot restart mid-conversation doesn't lose progress.

No passcode gate (a deliberate decision, not an oversight - see the plan):
live validation against real Factor/Leader/Asana accounts before anything
is created is itself the barrier against a stranger who happens to find the
bot's username.
"""

import re
import uuid

STATE_ASK_TEAM_NAME = "ASK_TEAM_NAME"
STATE_ASK_FACTOR_TOKEN = "ASK_FACTOR_TOKEN"
STATE_ASK_FACTOR_TENANT = "ASK_FACTOR_TENANT"
STATE_ASK_LEADER_TOKEN = "ASK_LEADER_TOKEN"
STATE_ASK_LEADER_TENANT = "ASK_LEADER_TENANT"
STATE_ASK_ASANA_TOKEN = "ASK_ASANA_TOKEN"
STATE_ASK_WORKSPACE_CHOICE = "ASK_WORKSPACE_CHOICE"
STATE_ASK_ORG_TEAM_CHOICE = "ASK_ORG_TEAM_CHOICE"
STATE_ASK_BOARD_COUNT = "ASK_BOARD_COUNT"
STATE_ASK_BOARDS_EXIST = "ASK_BOARDS_EXIST"
STATE_ASK_BOARD_LINKS = "ASK_BOARD_LINKS"
STATE_ASK_TEMPLATE_LINK = "ASK_TEMPLATE_LINK"
STATE_ASK_DATABASE_COUNT = "ASK_DATABASE_COUNT"
STATE_ASK_DATABASE_LINK = "ASK_DATABASE_LINK"
STATE_ASK_STAFF_ROSTER = "ASK_STAFF_ROSTER"
STATE_CONFIRM = "CONFIRM"

# Auto-generated dispatch board name suffixes when creating fresh boards
# from a template (see _handle_boards_exist's "no" branch) - onboarding
# never asks for names in that path, unlike the old free-text board-names
# step this replaced.
_BOARD_NAME_LETTERS = "ABCDEFGH"

# "David: D195" / "David - D195" / "David, D195" - one staff roster entry
# per line, name then code, any of a few common separators. Reused by
# router.py's "Staff Roster" menu (see add_staff_roster_entry) so a new
# hire later doesn't need a whole new onboarding conversation to parse the
# same "Name: Code" shape.
_ROSTER_LINE_PATTERN = re.compile(r"^\s*([A-Za-z][A-Za-z\-' ]*)\s*[:,\-]\s*([A-Za-z0-9#]+)\s*$")

# A pasted Asana project link ("https://app.asana.com/1/<workspace>/project/
# <project_id>/...") or a bare numeric project id - most teams already have
# a Database board with real driver history, so onboarding asks for the
# existing one instead of always creating a brand-new empty one (see
# _ask_database_count/_handle_database_link/provisioning.py's provision_team).
_ASANA_PROJECT_URL_PATTERN = re.compile(r"/project/(\d+)")


def _clean_pasted_value(text):
    """Strip copy-paste artifacts (a browser/phone keyboard's smart quotes,
    a trailing comma from a JSON viewer) off a pasted credential. Mirrors
    router.py's identically-named helper for the token-rotation flow -
    onboarding used to skip this and only .strip() whitespace, so a token
    wrapped in curly quotes (" ... ") got sent to Factor/Leader ELD with
    the quote characters still attached, making a perfectly valid token
    look "expired/unauthorized" (confirmed 2026-09-22 during a real
    onboarding attempt)."""
    return (
        text.strip().rstrip(",").strip()
        .strip('"').strip("'")
        .strip("“").strip("”").strip("‘").strip("’")
        .strip()
    )


def _parse_asana_project_ref(text):
    """Extract a project_id from a pasted Asana URL or bare numeric id, or
    None if text doesn't look like either."""
    text = text.strip()
    match = _ASANA_PROJECT_URL_PATTERN.search(text)
    if match:
        return match.group(1)
    return text if text.isdigit() else None


def _slugify(team_name):
    slug = re.sub(r"[^a-z0-9]+", "-", team_name.strip().lower()).strip("-")
    return slug or uuid.uuid4().hex[:8]


class OnboardingManager:
    def __init__(self, gateway, config_store, validators, provisioning, logger):
        self.gateway = gateway
        self.config_store = config_store
        self.validators = validators
        self.provisioning = provisioning
        self.logger = logger

    def begin(self, chat_id, sender_id):
        self.config_store.save_onboarding_session(chat_id, STATE_ASK_TEAM_NAME, {})
        self.gateway.send_message(
            chat_id,
            "Let's get your team set up. What's your team/company's name? "
            "(just a label - it doesn't have to match any Asana or Factor "
            "ELD name exactly - or /cancel any time to stop)",
        )

    def handle_reply(self, chat_id, sender_id, raw_text):
        state, data = self.config_store.get_onboarding_session(chat_id)
        text = raw_text.strip()
        if text.lower() == "/cancel":
            # Works in every state, not just STATE_CONFIRM (which already
            # had its own /cancel) - the real fix for a stuck/unwanted
            # onboarding conversation is an explicit, discoverable way out,
            # not router.py guessing when a session must be abandoned.
            self.config_store.clear_onboarding_session(chat_id)
            self.gateway.send_message(chat_id, "Cancelled. Send /start to try again.")
            return
        if text.lower() == "/start":
            # Confirmed happening for real: sending /start mid-conversation
            # (e.g. out of habit, or to restart after a mistake) was
            # silently swallowed as literal answer text for whatever
            # question was active - "Asana token" ended up being the
            # literal string "/start", which then failed a live API check
            # with a confusing 401. /start should always be safe to send -
            # restart the conversation cleanly instead.
            self.begin(chat_id, sender_id)
            return
        handler = {
            STATE_ASK_TEAM_NAME: self._handle_team_name,
            STATE_ASK_FACTOR_TOKEN: self._handle_factor_token,
            STATE_ASK_FACTOR_TENANT: self._handle_factor_tenant,
            STATE_ASK_LEADER_TOKEN: self._handle_leader_token,
            STATE_ASK_LEADER_TENANT: self._handle_leader_tenant,
            STATE_ASK_ASANA_TOKEN: self._handle_asana_token,
            STATE_ASK_WORKSPACE_CHOICE: self._handle_workspace_choice,
            STATE_ASK_ORG_TEAM_CHOICE: self._handle_org_team_choice,
            STATE_ASK_BOARD_COUNT: self._handle_board_count,
            STATE_ASK_BOARDS_EXIST: self._handle_boards_exist,
            STATE_ASK_BOARD_LINKS: self._handle_board_links,
            STATE_ASK_TEMPLATE_LINK: self._handle_template_link,
            STATE_ASK_DATABASE_COUNT: self._handle_database_count,
            STATE_ASK_DATABASE_LINK: self._handle_database_link,
            STATE_ASK_STAFF_ROSTER: self._handle_staff_roster,
            STATE_CONFIRM: self._handle_confirm,
        }.get(state)
        if handler is None:
            self.logger.error("Unknown onboarding state '%s' for chat %s - restarting.", state, chat_id)
            self.begin(chat_id, sender_id)
            return
        handler(chat_id, sender_id, data, text)

    def handle_callback(self, chat_id, sender_id, callback_data):
        """Entry point for a tapped inline-keyboard button during
        onboarding (see router.py's _handle_callback_query).

        Two different callback shapes, both routed through the SAME
        handler a typed reply would use, so the underlying logic is never
        duplicated between the button and (still-supported, in case
        someone types instead of tapping) text paths:
        - "onboard_workspace:2" / "onboard_orgteam:1" - a 0-based index
          into a dynamic choice list, converted to the 1-based text form
          those two handlers expect.
        - "onboard_boardcount:3" / "onboard_boardsexist:yes" /
          "onboard_dbcount:back" - a literal payload (a number, yes/no, or
          "back"), passed straight through as-is - these handlers parse
          plain text themselves, so there's no index to convert."""
        session = self.config_store.get_onboarding_session(chat_id)
        if session is None:
            return
        state, data = session
        prefix, _, payload = callback_data.partition(":")

        if prefix == "onboard_workspace" and state == STATE_ASK_WORKSPACE_CHOICE:
            try:
                one_based = str(int(payload) + 1)
            except ValueError:
                return
            self._handle_workspace_choice(chat_id, sender_id, data, one_based)
        elif prefix == "onboard_orgteam" and state == STATE_ASK_ORG_TEAM_CHOICE:
            try:
                one_based = str(int(payload) + 1)
            except ValueError:
                return
            self._handle_org_team_choice(chat_id, sender_id, data, one_based)
        elif prefix == "onboard_boardcount" and state == STATE_ASK_BOARD_COUNT:
            self._handle_board_count(chat_id, sender_id, data, payload)
        elif prefix == "onboard_boardsexist" and state == STATE_ASK_BOARDS_EXIST:
            self._handle_boards_exist(chat_id, sender_id, data, payload)
        elif prefix == "onboard_dbcount" and state == STATE_ASK_DATABASE_COUNT:
            self._handle_database_count(chat_id, sender_id, data, payload)

    def _advance(self, chat_id, next_state, data):
        self.config_store.save_onboarding_session(chat_id, next_state, data)

    def _handle_team_name(self, chat_id, sender_id, data, text):
        if not text:
            self.gateway.send_message(chat_id, "Please send a team name.")
            return
        data["team_name"] = text
        self._advance(chat_id, STATE_ASK_FACTOR_TOKEN, data)
        self.gateway.send_message(
            chat_id,
            "Paste your Factor ELD session token (or send /skip if this "
            "team doesn't use Factor ELD, or /cancel to stop).",
        )

    def _handle_factor_token(self, chat_id, sender_id, data, text):
        if text.lower() == "/skip":
            data["factor_session_token"] = None
            self._advance(chat_id, STATE_ASK_LEADER_TOKEN, data)
            self.gateway.send_message(chat_id, "Paste your Leader ELD session token (or /skip).")
            return
        data["factor_session_token"] = _clean_pasted_value(text)
        self._advance(chat_id, STATE_ASK_FACTOR_TENANT, data)
        self.gateway.send_message(chat_id, "What's your Factor ELD tenant_id?")

    def _handle_factor_tenant(self, chat_id, sender_id, data, text):
        data["factor_tenant_id"] = _clean_pasted_value(text)
        ok, message = self.validators.check_factor(data["factor_session_token"], data["factor_tenant_id"])
        if not ok:
            self._advance(chat_id, STATE_ASK_FACTOR_TOKEN, data)
            self.gateway.send_message(
                chat_id, f"That Factor ELD token/tenant was rejected: {message}\n\nPaste the token again.",
            )
            return
        self.gateway.send_message(chat_id, f"Factor ELD confirmed ({message}).")
        self._warn_if_token_reused(chat_id, "factor_session_token", data["factor_session_token"], "Factor ELD")
        self._advance(chat_id, STATE_ASK_LEADER_TOKEN, data)
        self.gateway.send_message(chat_id, "Paste your Leader ELD session token (or /skip).")

    def _handle_leader_token(self, chat_id, sender_id, data, text):
        if text.lower() == "/skip":
            data["leader_session_token"] = None
            self._advance(chat_id, STATE_ASK_ASANA_TOKEN, data)
            self.gateway.send_message(chat_id, "Now paste your Asana personal access token.")
            return
        data["leader_session_token"] = _clean_pasted_value(text)
        self._advance(chat_id, STATE_ASK_LEADER_TENANT, data)
        self.gateway.send_message(chat_id, "What's your Leader ELD tenant_id?")

    def _handle_leader_tenant(self, chat_id, sender_id, data, text):
        data["leader_tenant_id"] = _clean_pasted_value(text)
        ok, message = self.validators.check_leader(data["leader_session_token"], data["leader_tenant_id"])
        if not ok:
            self._advance(chat_id, STATE_ASK_LEADER_TOKEN, data)
            self.gateway.send_message(
                chat_id, f"That Leader ELD token/tenant was rejected: {message}\n\nPaste the token again.",
            )
            return
        self.gateway.send_message(chat_id, f"Leader ELD confirmed ({message}).")
        self._warn_if_token_reused(chat_id, "leader_session_token", data["leader_session_token"], "Leader ELD")
        self._advance(chat_id, STATE_ASK_ASANA_TOKEN, data)
        self.gateway.send_message(chat_id, "Now paste your Asana personal access token.")

    def _warn_if_token_reused(self, chat_id, token_field, session_token, label):
        """A token+tenant_id pair validating successfully only proves it's a
        REAL account - it says nothing about whether it's THIS team's own
        account. Confirmed live (2026-09-25/26, Central B and then ALGO D):
        pasting another team's SESSION TOKEN by habit/mistake passes
        validation every time and silently pulls that other team's exact
        companies onto the new team's boards - the single most expensive
        mistake made across this whole onboarding effort, each time only
        caught by manually diffing live driver data well after boards were
        already created and syncing.

        This checks the TOKEN, not the tenant_id - confirmed live
        (2026-09-26, onboarding LEADER D) that every existing team,
        including the original two (Texas/Missouri), shares the exact same
        factor_tenant_id/leader_tenant_id: that's this org's normal shared-
        account architecture, not a mistake, so warning on tenant_id match
        would fire on every single onboarding from now on and teach people
        to ignore it. The session token is what actually scopes which
        companies are visible - two teams sharing the same one is the real,
        rare, expensive mistake worth flagging. Only warns, never blocks -
        a genuinely shared token could still be intentional."""
        if not session_token:
            return
        matches = [
            t["team_name"] for t in self.config_store.list_teams()
            if t.get(token_field) == session_token
        ]
        if matches:
            self.gateway.send_message(
                chat_id,
                f"⚠️ Heads up: this exact {label} session token is already used by "
                f"{', '.join(matches)}. If that's not intentional (this should "
                "be a DIFFERENT login for this team), send /cancel now and "
                "restart with the correct one - continuing will very likely "
                "mix that team's companies onto this one's boards.",
            )

    def _handle_asana_token(self, chat_id, sender_id, data, text):
        text = _clean_pasted_value(text)
        data["asana_token"] = text
        ok, result = self.validators.check_asana(text)
        if not ok:
            self.gateway.send_message(chat_id, f"That Asana token was rejected: {result}\n\nPaste it again.")
            return
        workspaces = result
        if not workspaces:
            self.gateway.send_message(chat_id, "That token has no workspaces available - paste a different token.")
            return
        if len(workspaces) == 1:
            data["workspace_gid"] = workspaces[0]["gid"]
            self._after_workspace_chosen(chat_id, sender_id, data)
            return
        data["_workspace_choices"] = workspaces
        self._advance(chat_id, STATE_ASK_WORKSPACE_CHOICE, data)
        buttons = [(w["name"], f"onboard_workspace:{i}") for i, w in enumerate(workspaces)]
        self.gateway.send_buttons(chat_id, "Which Asana workspace?", buttons)

    def _handle_workspace_choice(self, chat_id, sender_id, data, text):
        choices = data.get("_workspace_choices", [])
        try:
            chosen = choices[int(text) - 1]
        except (ValueError, IndexError):
            self.gateway.send_message(chat_id, "Please reply with just the number of your workspace.")
            return
        data["workspace_gid"] = chosen["gid"]
        data.pop("_workspace_choices", None)
        self._after_workspace_chosen(chat_id, sender_id, data)

    def _after_workspace_chosen(self, chat_id, sender_id, data):
        info = self.validators.workspace_info(data["asana_token"], data["workspace_gid"])
        if info.get("is_organization"):
            asana_teams = self.validators.organization_teams(data["asana_token"], data["workspace_gid"])
            if asana_teams:
                data["_org_team_choices"] = asana_teams
                self._advance(chat_id, STATE_ASK_ORG_TEAM_CHOICE, data)
                buttons = [(t["name"], f"onboard_orgteam:{i}") for i, t in enumerate(asana_teams)]
                self.gateway.send_buttons(
                    chat_id,
                    "This is an organization workspace - which Asana Team "
                    "should your boards be created under? (a different "
                    "\"Team\" concept than your own company - just pick "
                    "where you want the boards to live)",
                    buttons,
                )
                return
        data["asana_team_gid"] = None
        self._ask_board_count(chat_id, data)

    def _handle_org_team_choice(self, chat_id, sender_id, data, text):
        choices = data.get("_org_team_choices", [])
        try:
            chosen = choices[int(text) - 1]
        except (ValueError, IndexError):
            self.gateway.send_message(chat_id, "Please reply with just the number.")
            return
        data["asana_team_gid"] = chosen["gid"]
        data.pop("_org_team_choices", None)
        self._ask_board_count(chat_id, data)

    # ---------- dispatch boards: how many, existing vs. template-created ----------

    def _ask_board_count(self, chat_id, data):
        self._advance(chat_id, STATE_ASK_BOARD_COUNT, data)
        buttons = [(str(n), f"onboard_boardcount:{n}") for n in (1, 2, 3, 4)]
        self.gateway.send_buttons(chat_id, "How many dispatch boards do you have?", buttons)

    def _handle_board_count(self, chat_id, sender_id, data, text):
        try:
            count = int(text.strip())
        except ValueError:
            count = None
        if count not in (1, 2, 3, 4):
            self.gateway.send_message(chat_id, "Please pick a number from the buttons (1-4).")
            return
        data["dispatch_board_count"] = count
        self._ask_boards_exist(chat_id, data)

    def _ask_boards_exist(self, chat_id, data):
        self._advance(chat_id, STATE_ASK_BOARDS_EXIST, data)
        buttons = [
            ("Yes, I have them", "onboard_boardsexist:yes"),
            ("No, create new", "onboard_boardsexist:no"),
            ("« Back", "onboard_boardsexist:back"),
        ]
        self.gateway.send_buttons(
            chat_id,
            f"Do you already have {data['dispatch_board_count']} board(s) created in Asana?",
            buttons,
        )

    def _handle_boards_exist(self, chat_id, sender_id, data, text):
        lowered = text.strip().lower()
        if lowered == "back":
            self._ask_board_count(chat_id, data)
            return
        if lowered not in ("yes", "no"):
            self.gateway.send_message(chat_id, "Please tap Yes or No.")
            return
        data["dispatch_boards_exist"] = (lowered == "yes")
        if lowered == "yes":
            self._advance(chat_id, STATE_ASK_BOARD_LINKS, data)
            self.gateway.send_message(
                chat_id,
                f"Paste the link (or project ID) for each of your {data['dispatch_board_count']} "
                "board(s), one per line.",
            )
        else:
            self._advance(chat_id, STATE_ASK_TEMPLATE_LINK, data)
            self.gateway.send_message(
                chat_id,
                "Paste the link (or project ID) of an existing board - yours or "
                "another team's - to copy the company/driver list from. Your new "
                "board(s) will use our standard columns (Status/Vehicle Number/"
                "Staff ID) and start with that same list of companies - it's a "
                "fresh copy, so editing one later never affects the other.",
            )

    def _handle_board_links(self, chat_id, sender_id, data, text):
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        needed = data["dispatch_board_count"]
        project_ids = []
        for line in lines:
            project_id = _parse_asana_project_ref(line)
            if project_id is None:
                self.gateway.send_message(
                    chat_id, f"Couldn't read a link/ID from '{line}' - paste all {needed} again, one per line.",
                )
                return
            project_ids.append(project_id)
        if len(project_ids) != needed:
            self.gateway.send_message(
                chat_id,
                f"That's {len(project_ids)} link(s), but you said {needed} board(s) - "
                f"paste all {needed}, one per line.",
            )
            return

        names = []
        for project_id in project_ids:
            ok, result = self.validators.check_asana_project(data["asana_token"], project_id)
            if not ok:
                self.gateway.send_message(
                    chat_id, f"Couldn't access project {project_id}: {result}\n\nPaste all {needed} links again.",
                )
                return
            names.append(result)

        data["existing_dispatch_project_ids"] = project_ids
        data["dispatch_board_names"] = names
        self.gateway.send_message(chat_id, f"Found: {', '.join(names)}.")
        self._ask_database_count(chat_id, data)

    def _handle_template_link(self, chat_id, sender_id, data, text):
        project_id = _parse_asana_project_ref(text)
        if project_id is None:
            self.gateway.send_message(chat_id, "Couldn't read a link or ID from that - paste it again.")
            return
        ok, result = self.validators.check_asana_project(data["asana_token"], project_id)
        if not ok:
            self.gateway.send_message(chat_id, f"Couldn't access that project: {result}\n\nPaste the link again.")
            return
        data["template_dispatch_project_id"] = project_id
        data["dispatch_board_names"] = [
            f"{data['team_name']} {_BOARD_NAME_LETTERS[i]}" if data["dispatch_board_count"] > 1 else data["team_name"]
            for i in range(data["dispatch_board_count"])
        ]
        self.gateway.send_message(chat_id, f"Using '{result}' as the template.")
        self._ask_database_count(chat_id, data)

    # ---------- Database board(s): how many, then a link (or /skip) for each ----------

    def _ask_database_count(self, chat_id, data):
        self._advance(chat_id, STATE_ASK_DATABASE_COUNT, data)
        buttons = [
            ("1", "onboard_dbcount:1"),
            ("2", "onboard_dbcount:2"),
            ("« Back", "onboard_dbcount:back"),
        ]
        self.gateway.send_buttons(
            chat_id,
            "How many Database boards do you have (a permanent record of every "
            "driver, active or inactive)?",
            buttons,
        )

    def _handle_database_count(self, chat_id, sender_id, data, text):
        lowered = text.strip().lower()
        if lowered == "back":
            self._ask_boards_exist(chat_id, data)
            return
        try:
            count = int(lowered)
        except ValueError:
            count = None
        if count not in (1, 2):
            self.gateway.send_message(chat_id, "Please pick 1 or 2 from the buttons.")
            return
        data["database_board_count"] = count
        data["existing_database_project_ids"] = []
        self._ask_next_database_link(chat_id, data)

    def _ask_next_database_link(self, chat_id, data):
        self._advance(chat_id, STATE_ASK_DATABASE_LINK, data)
        total = data["database_board_count"]
        index = len(data["existing_database_project_ids"]) + 1
        label = f"Database board {index}/{total}" if total > 1 else "Database board"
        self.gateway.send_message(
            chat_id,
            f"{label}: paste its project link or ID and syncing will only ever add "
            "new drivers to it - your existing history is never touched or "
            "rewritten. Send /skip if you don't have one yet and want a new one "
            "created.",
        )

    def _handle_database_link(self, chat_id, sender_id, data, text):
        if text.lower() == "/skip":
            data["existing_database_project_ids"].append(None)
        else:
            project_id = _parse_asana_project_ref(text)
            if project_id is None:
                self.gateway.send_message(
                    chat_id, "Couldn't read a project link or ID from that - paste it again, or /skip.",
                )
                return

            ok, result = self.validators.check_asana_project(data["asana_token"], project_id)
            if not ok:
                self.gateway.send_message(
                    chat_id, f"Couldn't access that project: {result}\n\nPaste the link/ID again, or /skip.",
                )
                return

            attached_fields = self.validators.ensure_database_project_ready(
                data["asana_token"], data["workspace_gid"], project_id,
            )
            data["existing_database_project_ids"].append(project_id)
            if attached_fields:
                self.gateway.send_message(
                    chat_id,
                    f"Found it: '{result}' - it had no usable columns yet, so I added our "
                    "standard set (Co-driver/Vehicle Id/Email/Phone Number/CDL/State/Login/"
                    "Password) to it.",
                )
            else:
                self.gateway.send_message(chat_id, f"Found it: '{result}'.")

        if len(data["existing_database_project_ids"]) < data["database_board_count"]:
            self._ask_next_database_link(chat_id, data)
        else:
            self._ask_staff_roster(chat_id, data)

    def _ask_staff_roster(self, chat_id, data):
        self._advance(chat_id, STATE_ASK_STAFF_ROSTER, data)
        self.gateway.send_message(
            chat_id,
            "Last thing: your staff roster for the Staff ID field (who "
            "edits driver logbooks). Send one person per line as "
            "'FirstName: Code' (e.g. 'David: D195'), or /skip to leave it "
            "empty and add people later.",
        )

    def _handle_staff_roster(self, chat_id, sender_id, data, text):
        roster = {}
        if text.lower() != "/skip":
            for line in text.splitlines():
                match = _ROSTER_LINE_PATTERN.match(line)
                if match:
                    roster[match.group(1).strip().lower()] = match.group(2).strip()
            if not roster:
                self.gateway.send_message(chat_id, "Couldn't read any 'Name: Code' lines - try again, or /skip.")
                return
        data["staff_roster"] = roster
        self._advance(chat_id, STATE_CONFIRM, data)

        if data.get("dispatch_boards_exist"):
            dispatch_line = f"Dispatch board(s): using existing - {', '.join(data['dispatch_board_names'])}"
        else:
            dispatch_line = (
                f"Dispatch board(s): {data['dispatch_board_count']} new board(s) "
                f"({', '.join(data['dispatch_board_names'])}), shaped like template "
                f"project {data['template_dispatch_project_id']}"
            )
        database_lines = "\n".join(
            f"Database board {i}: existing project {pid}" if pid else f"Database board {i}: new one will be created"
            for i, pid in enumerate(data["existing_database_project_ids"], 1)
        )
        summary = (
            f"Team: {data['team_name']}\n"
            f"Factor ELD: {'configured' if data.get('factor_session_token') else 'skipped'}\n"
            f"Leader ELD: {'configured' if data.get('leader_session_token') else 'skipped'}\n"
            f"Asana workspace: {data['workspace_gid']}\n"
            f"{dispatch_line}\n"
            f"{database_lines}\n"
            f"Staff roster entries: {len(roster)}\n\n"
            "Reply /confirm to create your boards now, or /cancel to start over."
        )
        self.gateway.send_message(chat_id, summary)

    def _handle_confirm(self, chat_id, sender_id, data, text):
        lowered = text.lower()
        if lowered == "/cancel":
            self.config_store.clear_onboarding_session(chat_id)
            self.gateway.send_message(chat_id, "Cancelled. Send /start to try again.")
            return
        if lowered != "/confirm":
            self.gateway.send_message(chat_id, "Reply /confirm to proceed, or /cancel to start over.")
            return

        team_id = _slugify(data["team_name"])
        self.gateway.send_message(chat_id, "Creating your boards now - this takes a minute...")
        try:
            self.provisioning.provision_team(team_id, data)
            self.config_store.add_team_admin(chat_id, team_id, sender_id)
            # provision_team() already wrote the .env before this admin's
            # chat_id was registered - rewrite it now so TEAM_CHAT_IDS
            # actually includes them. No restart needed: multi_sync.py's
            # shared loop queries config_store fresh every cycle and will
            # pick this team up (status="active") on its very next pass.
            self.provisioning.rewrite_env(team_id)
        except Exception:
            self.logger.exception("Provisioning failed for team '%s'", team_id)
            self.gateway.send_message(
                chat_id,
                "Something went wrong while creating your boards. Please "
                "check with an admin before retrying with /start.",
            )
            return
        self.config_store.clear_onboarding_session(chat_id)
        self.gateway.send_message(
            chat_id,
            "Done! Your boards are live and syncing has started. Send "
            "/status any time to check on it.",
        )
