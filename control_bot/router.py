"""
control_bot/router.py

Routes an incoming Telegram message to the right team, unlike
telegram_control.py which assumes there's only ever one. An unknown
chat_id with no onboarding session in progress starts onboarding.py's
conversation (private chats only - see the plan's decision to restrict
onboarding to DMs, since tokens get pasted in plain chat text). A known
team's chat_id gets the interactive commands (/status, /pause, /resume,
/rotatefactor, /rotateleader, /rotateasana) applied to THEIR row only.
"""

import re

import asana_client
import eld_factor
import eld_leader
from control_bot import validators
from control_bot.onboarding import _ROSTER_LINE_PATTERN
from eld_common import invisibility_reason

_JWT_LIKE_PATTERN = re.compile(r"^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$")

_ROTATABLE_FIELDS = {
    "/rotatefactor": ("factor_session_token", "Factor ELD"),
    "/rotateleader": ("leader_session_token", "Leader ELD"),
    "/rotateasana": ("asana_token", "Asana"),
}

# The bot's main menu, styled after Telegram's own nested settings menus
# (e.g. @BotFather's) - one button per top-level action, each drilling into
# its own page with a "« Back to Bot" button that always returns here.
MAIN_MENU_BUTTONS = [
    ("Company Assign", "menu:companyassign"),
    ("Rotate Tokens", "menu:rotate"),
    ("Truck Numbers", "menu:trucks"),
    ("Staff Roster", "menu:staffroster"),
]
MAIN_MENU_TEXT = "Main Menu:"
BACK_BUTTON = ("« Back to Bot", "menu:main")


def _clean_pasted_value(raw_text):
    """Same copy-paste-artifact cleanup telegram_control.py's
    _apply_new_token already does (trailing comma/quotes from a browser's
    JSON viewer)."""
    return raw_text.strip().rstrip(",").strip().strip('"').strip("'").strip()


class TeamRouter:
    def __init__(self, gateway, config_store, onboarding, provisioning, logger):
        self.gateway = gateway
        self.config_store = config_store
        self.onboarding = onboarding
        self.provisioning = provisioning
        self.logger = logger

    def handle_update(self, update):
        callback_query = update.get("callback_query")
        if callback_query:
            self._handle_callback_query(callback_query)
            return

        message = update.get("message")
        if not message:
            return
        chat_id = message["chat"]["id"]
        sender_id = message.get("from", {}).get("id")
        chat_type = message.get("chat", {}).get("type", "private")
        raw_text = (message.get("text") or "").strip()
        text = raw_text.lower()

        if text == "/menu":
            # Universal override - always means "abandon whatever I was
            # doing and show me the menu," even mid-onboarding or
            # mid-rotation (confirmed happening for real: without this,
            # /menu just got swallowed as literal answer text at whatever
            # question was currently active, silently corrupting it -
            # "team name" and "Leader ELD token" both ended up literally
            # set to "/menu"). Takes priority over any in-progress session.
            self.config_store.clear_onboarding_session(chat_id)
            team_id = self._resolve_active_team(chat_id)
            if team_id is None:
                self.gateway.send_message(chat_id, "Send /start to onboard your team first.")
            else:
                self.gateway.send_buttons(
                    chat_id, self._main_menu_text(chat_id, team_id), self._main_menu_buttons(chat_id), columns=1,
                )
            return

        session = self.config_store.get_onboarding_session(chat_id)
        if session is not None:
            state, data = session
            if state.startswith("AWAITING_ROTATION:"):
                # Always legitimate - only ever created for an already-
                # registered team (see _prompt_rotation) - takes priority
                # over everything else for this chat_id.
                self._handle_rotation_reply(chat_id, state, data, raw_text)
            elif state == "AWAITING_COMPANY_NAME":
                # Always legitimate - only ever created for an already-
                # registered team (see _prompt_create_section).
                self._handle_create_section_reply(chat_id, data, raw_text)
            elif state == "AWAITING_STAFF_ADD":
                # Always legitimate - only ever created for an already-
                # registered team (see _prompt_staff_roster_add).
                self._handle_staff_add_reply(chat_id, data, raw_text)
            else:
                # A chat that already administers other teams can
                # legitimately be mid-onboarding for one MORE (see "Add
                # Another Team") - onboarding sessions are no longer
                # assumed stale just because this chat is registered.
                # /cancel (handled inside onboarding.handle_reply, in
                # every state) is the real way out of a stuck/unwanted one.
                self.onboarding.handle_reply(chat_id, sender_id, raw_text)
            return

        team_id = self._resolve_active_team(chat_id)
        if team_id is None:
            team_ids = self.config_store.team_ids_for_chat(chat_id)
            if team_ids:
                # Registered for more than one team but none currently
                # selected - needs an explicit choice, not a guess.
                self.gateway.send_buttons(
                    chat_id, "Pick a team first:", self._main_menu_buttons(chat_id), columns=1,
                )
                return
            if text == "/start":
                if chat_type != "private":
                    self.gateway.send_message(
                        chat_id,
                        "Please message me directly (not in a group) to "
                        "onboard your team - credentials get pasted in "
                        "plain chat text during setup.",
                    )
                    return
                self.onboarding.begin(chat_id, sender_id)
            else:
                self.gateway.send_message(chat_id, "Send /start to onboard your team.")
            return

        self._handle_known_team_command(chat_id, team_id, raw_text, text)

    def _resolve_active_team(self, chat_id):
        """Which team a bare command (no team_id embedded anywhere, unlike
        the callback-driven flows below) currently applies to for this
        chat. None if unregistered, or registered for several teams with
        none currently selected (see "Switch Team"). The single-team case
        (by far the common one) never needs an explicit selection - it's
        kept active automatically."""
        team_ids = self.config_store.team_ids_for_chat(chat_id)
        if not team_ids:
            return None
        if len(team_ids) == 1:
            self.config_store.set_active_team(chat_id, team_ids[0])
            return team_ids[0]
        active = self.config_store.get_active_team(chat_id)
        return active if active in team_ids else None

    def _main_menu_text(self, chat_id, team_id):
        team_ids = self.config_store.team_ids_for_chat(chat_id)
        if len(team_ids) > 1 and team_id is not None:
            team = self.config_store.get_team(team_id)
            return f"Main Menu (Team: {team['team_name']}):"
        return MAIN_MENU_TEXT

    def _main_menu_buttons(self, chat_id):
        buttons = list(MAIN_MENU_BUTTONS)
        buttons.append(("Add Another Team", "menu:addteam"))
        if len(self.config_store.team_ids_for_chat(chat_id)) > 1:
            buttons.append(("Switch Team", "menu:switchteam"))
        return buttons

    def _handle_callback_query(self, callback_query):
        """Routes a tapped inline-keyboard button. Always answered (even on
        an error path) so Telegram clears the button's loading spinner -
        see gateway.answer_callback_query's docstring."""
        callback_query_id = callback_query["id"]
        chat_id = callback_query["message"]["chat"]["id"]
        message_id = callback_query["message"]["message_id"]
        sender_id = callback_query.get("from", {}).get("id")
        data = callback_query.get("data") or ""

        try:
            if data.startswith("onboard_workspace:") or data.startswith("onboard_orgteam:"):
                self.onboarding.handle_callback(chat_id, sender_id, data)
            elif data.startswith("menu:"):
                self._handle_menu_callback(chat_id, message_id, sender_id, data)
            elif data.startswith("companyassign:"):
                self._handle_company_assign_callback(chat_id, message_id, data)
            elif data.startswith("assign:"):
                self._handle_assign_callback(chat_id, message_id, sender_id, data)
            elif data.startswith("skip:"):
                self._handle_skip_callback(chat_id, message_id, data)
            else:
                self.logger.warning("Unrecognized callback_data: %r", data)
        finally:
            self.gateway.answer_callback_query(callback_query_id)

    def _handle_known_team_command(self, chat_id, team_id, raw_text, text):
        if text == "/status":
            self._send_status(chat_id, team_id)
        elif text == "/trucks":
            self.gateway.send_message(chat_id, "Checking now...")
            self.gateway.send_buttons(chat_id, self._truck_counts_text(team_id), [BACK_BUTTON])
        elif text == "/pause":
            self.config_store.update_team(team_id, status="paused")
            self.gateway.send_buttons(chat_id, "Sync paused. Send /resume to continue.", [BACK_BUTTON])
        elif text == "/resume":
            self.config_store.update_team(team_id, status="active")
            self.gateway.send_buttons(chat_id, "Sync resumed.", [BACK_BUTTON])
        elif text in _ROTATABLE_FIELDS:
            self._prompt_rotation(chat_id, team_id, text)
        elif _JWT_LIKE_PATTERN.match(_clean_pasted_value(raw_text)):
            # A bare pasted token with no command first - ambiguous which
            # credential this is for in multi-tenant mode (unlike the
            # single-tenant bot, which only ever has one rotatable secret),
            # so ask instead of guessing.
            self.gateway.send_message(
                chat_id,
                "That looks like a token - which one is it? Send "
                "/rotatefactor, /rotateleader, or /rotateasana first, then paste it.",
            )
        else:
            # /start, /menu, or anything unrecognized - the main menu is
            # the primary control surface; slash-commands above still work
            # as shortcuts for anyone used to them.
            self.gateway.send_buttons(
                chat_id, self._main_menu_text(chat_id, team_id), self._main_menu_buttons(chat_id), columns=1,
            )

    def _truck_counts_text(self, team_id):
        """Live, on-demand active-truck counts for Factor ELD and Leader
        ELD using this team's own stored credentials. Both fetch_drivers()
        calls are already fully parameterized (see the plan's Phase 1) -
        no changes needed to either platform module. Shared by the
        /trucks command and the menu's "Truck Numbers" page."""
        team = self.config_store.get_team(team_id)
        lines = []
        if team.get("factor_session_token"):
            drivers = eld_factor.fetch_drivers(
                self.logger, session_token=team["factor_session_token"],
                tenant_id=team["factor_tenant_id"], apply_company_filter=False,
            )
            active = sum(1 for d in drivers if invisibility_reason(d) is None)
            lines.append(f"Factor ELD: {active} active trucks")
        if team.get("leader_session_token"):
            drivers = eld_leader.fetch_drivers(
                self.logger, session_token=team["leader_session_token"], tenant_id=team["leader_tenant_id"],
            )
            active = sum(1 for d in drivers if invisibility_reason(d) is None)
            lines.append(f"Leader ELD: {active} active trucks")
        return "\n".join(lines) or "Neither platform is configured for this team."

    def _handle_company_assign_callback(self, chat_id, message_id, data):
        """The first-page "Company Assign" button was tapped - drills into
        the real per-board choices + Skip (matching the two-step menu
        style requested, e.g. BotFather's own settings menu: one item up
        front, its actual options only appear once you tap in). Builds the
        board list fresh from this team's own stored Asana credentials
        rather than anything sync.py passed along, since by this point
        sync.py's own alert is long done - only the pending_companies
        entry (see config_store.get_pending_company) and this
        callback_data survive between the two steps.
        callback_data shape: "companyassign:<team_id>:<pending_id>"."""
        _, callback_team_id, pending_id = data.split(":", 2)
        if callback_team_id not in self.config_store.team_ids_for_chat(chat_id):
            self.gateway.send_message(chat_id, "This button isn't for your team - ignoring.")
            return

        entry = self.config_store.get_pending_company(callback_team_id, pending_id)
        if entry is None:
            self.gateway.edit_message_text(
                chat_id, message_id, "That company was already assigned (or is no longer pending).",
                buttons=[BACK_BUTTON],
            )
            return

        team = self.config_store.get_team(callback_team_id)
        client = asana_client.AsanaClient(team["asana_token"], [], self.logger)
        project_ids = [p.strip() for p in team["asana_project_ids"].split(",") if p.strip()]
        buttons = [
            (name, f"assign:{callback_team_id}:{pending_id}:{project_id}")
            for project_id, name in client.get_project_names(project_ids).items()
        ]
        buttons.append(("Skip", f"skip:{callback_team_id}:{pending_id}"))
        buttons.append(BACK_BUTTON)
        self.gateway.edit_message_text(
            chat_id, message_id, f"'{entry['company_name']}' - which board should it join?", buttons=buttons,
        )

    def _handle_menu_callback(self, chat_id, message_id, sender_id, data):
        """Routes a tap within the main menu / one of its submenus.
        callback_data shape: "menu:<action>" where action is "main",
        "companyassign", "rotate", "trucks", "addteam", "switchteam", or
        "rotate:<rotate-command>" / "switchteam:<team_id>" (the actual
        choice made inside a submenu)."""
        _, action = data.split(":", 1)

        # These two don't need (or, for addteam, must not require) an
        # already-resolved active team - handled before that lookup.
        if action == "addteam":
            self.onboarding.begin(chat_id, sender_id)
            return
        if action == "switchteam":
            self._show_menu_switch_team(chat_id, message_id)
            return
        if action.startswith("switchteam:"):
            self._handle_switch_team_choice(chat_id, message_id, action.split(":", 1)[1])
            return

        team_id = self._resolve_active_team(chat_id)
        if team_id is None:
            return

        if action == "main":
            # "« Back to Bot" always fully resets state - clears any
            # dangling rotation-in-progress session (see _prompt_rotation)
            # so a subsequent typed message is never silently swallowed as
            # a token paste after tapping Back instead of /cancel.
            self.config_store.clear_onboarding_session(chat_id)
            self.gateway.edit_message_text(
                chat_id, message_id, self._main_menu_text(chat_id, team_id), buttons=self._main_menu_buttons(chat_id),
            )
        elif action == "companyassign":
            self._show_menu_create_section_boards(chat_id, message_id, team_id)
        elif action.startswith("createsection:"):
            self._prompt_create_section(chat_id, message_id, team_id, action.split(":", 1)[1])
        elif action == "rotate":
            buttons = [(label, f"menu:rotate:{cmd}") for cmd, (_, label) in _ROTATABLE_FIELDS.items()]
            buttons.append(BACK_BUTTON)
            self.gateway.edit_message_text(chat_id, message_id, "Which token would you like to rotate?", buttons=buttons)
        elif action == "trucks":
            self.gateway.edit_message_text(chat_id, message_id, "Checking now...")
            self.gateway.edit_message_text(
                chat_id, message_id, self._truck_counts_text(team_id), buttons=[BACK_BUTTON],
            )
        elif action.startswith("rotate:"):
            rotate_command = action.split(":", 1)[1]
            self.gateway.edit_message_text(chat_id, message_id, f"Selected: {_ROTATABLE_FIELDS[rotate_command][1]}")
            self._prompt_rotation(chat_id, team_id, rotate_command)
        elif action == "staffroster":
            self._show_menu_staff_roster(chat_id, message_id, team_id)
        elif action == "staffroster:add":
            self._prompt_staff_roster_add(chat_id, message_id, team_id)
        else:
            self.logger.warning("Unrecognized menu action: %r", action)

    def _show_menu_staff_roster(self, chat_id, message_id, team_id):
        team = self.config_store.get_team(team_id)
        roster = team.get("staff_roster") or {}
        if roster:
            lines = "\n".join(f"  {name.title()}: {code}" for name, code in sorted(roster.items()))
            text = f"Current staff roster:\n{lines}"
        else:
            text = "No staff roster entries yet."
        buttons = [("Add Person", "menu:staffroster:add"), BACK_BUTTON]
        self.gateway.edit_message_text(chat_id, message_id, text, buttons=buttons)

    def _prompt_staff_roster_add(self, chat_id, message_id, team_id):
        self.config_store.save_onboarding_session(chat_id, "AWAITING_STAFF_ADD", {"team_id": team_id})
        self.gateway.edit_message_text(
            chat_id, message_id,
            "Send the new person as 'FirstName: Code' (e.g. 'David: D195'), "
            "or /cancel to stop.",
        )

    def _show_menu_create_section_boards(self, chat_id, message_id, team_id):
        """The menu's "Company Assign" entry - lets you pick a board first,
        then type a company name to create a section for it there
        immediately (see _prompt_create_section/_handle_create_section_reply).
        Separate from the automatic new-company alert flow (companyassign:/
        assign: callbacks below), which already knows a specific company
        name from a detected driver and only asks which board - this is
        the proactive, menu-driven equivalent for creating one on demand."""
        team = self.config_store.get_team(team_id)
        client = asana_client.AsanaClient(team["asana_token"], [], self.logger)
        project_ids = [p.strip() for p in team["asana_project_ids"].split(",") if p.strip()]
        buttons = [
            (name, f"menu:createsection:{project_id}")
            for project_id, name in client.get_project_names(project_ids).items()
        ]
        buttons.append(BACK_BUTTON)
        self.gateway.edit_message_text(chat_id, message_id, "Which board?", buttons=buttons)

    def _prompt_create_section(self, chat_id, message_id, team_id, project_id):
        team = self.config_store.get_team(team_id)
        client = asana_client.AsanaClient(team["asana_token"], [], self.logger)
        project_name = client.get_project_names([project_id]).get(project_id, project_id)
        self.config_store.save_onboarding_session(
            chat_id, "AWAITING_COMPANY_NAME", {"team_id": team_id, "project_id": project_id, "project_name": project_name},
        )
        self.gateway.edit_message_text(
            chat_id, message_id,
            f"Send the company name(s) to create in {project_name} - one per "
            "line for several at once (or /cancel to stop).",
            buttons=[BACK_BUTTON],
        )

    def _handle_create_section_reply(self, chat_id, data, raw_text):
        text = raw_text.strip()
        if text.lower() == "/cancel":
            self.config_store.clear_onboarding_session(chat_id)
            self.gateway.send_buttons(chat_id, "Cancelled.", [BACK_BUTTON])
            return
        names = [line.strip() for line in text.splitlines() if line.strip()]
        if not names:
            self.gateway.send_message(chat_id, "Please send at least one company name, or /cancel to stop.")
            return

        team = self.config_store.get_team(data["team_id"])
        client = asana_client.AsanaClient(team["asana_token"], [], self.logger)
        project_id = data["project_id"]

        # Skip any name that already has a section here rather than blindly
        # creating a duplicate (create_section itself has no such check -
        # see its docstring) - matters once a board already has some
        # sections, and cheap insurance against re-pasting the same bulk
        # list twice by mistake.
        existing = {
            asana_client.normalize_company_name(s.get("name") or "")
            for s in client._fetch_sections(project_id)
        }
        created, skipped = [], []
        for name in names:
            if asana_client.normalize_company_name(name) in existing:
                skipped.append(name)
                continue
            client.create_section(project_id, name)
            existing.add(asana_client.normalize_company_name(name))
            created.append(name)

        self.config_store.clear_onboarding_session(chat_id)
        lines = []
        if created:
            lines.append(f"Created {len(created)} section(s) in {data['project_name']}: {', '.join(created)}")
            lines.append("Drivers there will start showing up next sync cycle.")
        if skipped:
            lines.append(f"Already existed, skipped: {', '.join(skipped)}")
        self.gateway.send_buttons(chat_id, "\n".join(lines) or "Nothing to do.", [BACK_BUTTON])

    def _handle_staff_add_reply(self, chat_id, data, raw_text):
        text = raw_text.strip()
        if text.lower() == "/cancel":
            self.config_store.clear_onboarding_session(chat_id)
            self.gateway.send_buttons(chat_id, "Cancelled.", [BACK_BUTTON])
            return

        match = _ROSTER_LINE_PATTERN.match(text)
        if not match:
            self.gateway.send_message(
                chat_id, "Couldn't read that - send it as 'FirstName: Code' (e.g. 'David: D195'), or /cancel.",
            )
            return

        first_name, code = match.group(1).strip(), match.group(2).strip()
        self.provisioning.add_staff_roster_entry(data["team_id"], first_name, code)
        self.config_store.clear_onboarding_session(chat_id)
        self.gateway.send_buttons(
            chat_id, f"Added '{first_name.title()}: {code}' to the staff roster.", [BACK_BUTTON],
        )

    def _show_menu_switch_team(self, chat_id, message_id):
        team_ids = self.config_store.team_ids_for_chat(chat_id)
        buttons = [
            (self.config_store.get_team(tid)["team_name"], f"menu:switchteam:{tid}")
            for tid in team_ids
        ]
        buttons.append(BACK_BUTTON)
        self.gateway.edit_message_text(chat_id, message_id, "Switch to which team?", buttons=buttons)

    def _handle_switch_team_choice(self, chat_id, message_id, chosen_team_id):
        if chosen_team_id not in self.config_store.team_ids_for_chat(chat_id):
            self.gateway.send_message(chat_id, "That team isn't linked to this chat - ignoring.")
            return
        self.config_store.set_active_team(chat_id, chosen_team_id)
        team = self.config_store.get_team(chosen_team_id)
        self.gateway.edit_message_text(
            chat_id, message_id, f"Switched to team: {team['team_name']}", buttons=[BACK_BUTTON],
        )

    def _handle_assign_callback(self, chat_id, message_id, sender_id, data):
        """A board button was tapped in response to a new-company alert
        (see sync.py's detection + telegram_notifier.notify_with_buttons).
        callback_data shape: "assign:<team_id>:<pending_id>:<project_id>"."""
        _, callback_team_id, pending_id, project_id = data.split(":", 3)
        if callback_team_id not in self.config_store.team_ids_for_chat(chat_id):
            self.gateway.send_message(chat_id, "This button isn't for your team - ignoring.")
            return

        entry = self.config_store.pop_pending_company(callback_team_id, pending_id)
        if entry is None:
            self.gateway.edit_message_text(
                chat_id, message_id, "That company was already assigned (or is no longer pending).",
                buttons=[BACK_BUTTON],
            )
            return

        team = self.config_store.get_team(callback_team_id)
        client = asana_client.AsanaClient(team["asana_token"], [], self.logger)
        client.create_section(project_id, entry["company_name"])
        project_name = client._get_project_config(project_id)["name"]
        self.gateway.edit_message_text(
            chat_id, message_id,
            f"'{entry['company_name']}' assigned to {project_name} - "
            f"it'll start showing up there next sync cycle.",
            buttons=[BACK_BUTTON],
        )

    def _handle_skip_callback(self, chat_id, message_id, data):
        """"Skip" was tapped - deliberately does NOT remove the entry (unlike
        an actual board assignment), so add_pending_company_if_new's dedup
        check keeps treating this company as already-seen and sync.py never
        re-alerts on it, even though nothing was created for it.
        callback_data shape: "skip:<team_id>:<pending_id>"."""
        _, callback_team_id, pending_id = data.split(":", 2)
        if callback_team_id not in self.config_store.team_ids_for_chat(chat_id):
            self.gateway.send_message(chat_id, "This button isn't for your team - ignoring.")
            return

        entry = self.config_store.get_pending_company(callback_team_id, pending_id)
        company_name = entry["company_name"] if entry else "that company"
        self.gateway.edit_message_text(
            chat_id, message_id, f"Skipped '{company_name}' - won't ask again.", buttons=[BACK_BUTTON],
        )

    def _send_status(self, chat_id, team_id):
        team = self.config_store.get_team(team_id)
        state = "paused" if team["status"] == "paused" else "active"
        self.gateway.send_buttons(
            chat_id, f"Team: {team['team_name']}\nSync: {state}",
            [BACK_BUTTON],
        )

    def _prompt_rotation(self, chat_id, team_id, command_text):
        field_name, label = _ROTATABLE_FIELDS[command_text]
        self.config_store.save_onboarding_session(
            chat_id, f"AWAITING_ROTATION:{field_name}", {"team_id": team_id, "label": label},
        )
        # "« Back to Bot" here is equivalent to /cancel (see menu:main's
        # handling, which always clears any pending onboarding/rotation
        # session first) - never a dead end that leaves a rotation
        # silently waiting for the next thing you happen to type.
        self.gateway.send_buttons(chat_id, f"Paste the new {label} token now (or /cancel to stop).", [BACK_BUTTON])

    def _validate_rotation_value(self, field_name, new_value, team):
        """Live-checks a rotation's pasted value the same way onboarding
        validates a brand-new team's credentials (see validators.py),
        before ever committing it. Added after a real incident: pasting a
        stray "/trucks" message while mid-rotation silently overwrote the
        real Asana token, because nothing here previously checked the
        value was a real, working credential before saving it."""
        if field_name == "factor_session_token":
            return validators.check_factor(new_value, team["factor_tenant_id"])
        if field_name == "leader_session_token":
            return validators.check_leader(new_value, team["leader_tenant_id"])
        if field_name == "asana_token":
            ok, result = validators.check_asana(new_value)
            return (True, f"{len(result)} workspace(s) visible") if ok else (False, result)
        return False, "unknown field"

    def _handle_rotation_reply(self, chat_id, state, data, raw_text):
        if raw_text.strip().lower() == "/cancel":
            self.config_store.clear_onboarding_session(chat_id)
            self.gateway.send_buttons(chat_id, "Cancelled - token left unchanged.", [BACK_BUTTON])
            return

        field_name = state.split(":", 1)[1]
        new_value = _clean_pasted_value(raw_text)
        # Cheap, fast-failing check before ever hitting the network - the
        # exact shape of the incident above (a bot command pasted instead
        # of a token).
        if not new_value or new_value.startswith("/"):
            self.gateway.send_message(
                chat_id, "That doesn't look like a token - paste the actual token value, or /cancel to stop.",
            )
            return

        team_id = data["team_id"]
        team = self.config_store.get_team(team_id)
        ok, message = self._validate_rotation_value(field_name, new_value, team)
        if not ok:
            self.gateway.send_message(
                chat_id,
                f"That {data['label']} token was rejected: {message}\n\nPaste it again, or /cancel to stop.",
            )
            return

        self.config_store.update_team(team_id, **{field_name: new_value})
        self.config_store.clear_onboarding_session(chat_id)
        self.provisioning.rewrite_env(team_id)
        self.gateway.send_buttons(
            chat_id,
            f"{data['label']} token updated ({message}) - takes effect on the next sync cycle.",
            [BACK_BUTTON],
        )
