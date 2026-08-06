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
    # (session/access token field, display label, matching tenant_id field
    # or None). Factor/Leader ELD scope which companies are visible by
    # tenant_id, not the token alone (confirmed live: a team can hold a
    # perfectly valid token whose tenant_id is a DIFFERENT team's -
    # exactly what put one team's driver data onto another team's boards)
    # - so those two rotate as a token+tenant_id PAIR, validated together,
    # never the token alone against a possibly-stale tenant_id. Asana has
    # no such concept, so it stays a single-step rotation.
    "/rotatefactor": ("factor_session_token", "Factor ELD", "factor_tenant_id"),
    "/rotateleader": ("leader_session_token", "Leader ELD", "leader_tenant_id"),
    "/rotateasana": ("asana_token", "Asana", None),
}

# The bot's main menu, styled after Telegram's own nested settings menus
# (e.g. @BotFather's) - one button per top-level action, each drilling into
# its own page with a "« Back to Bot" button that always returns here.
MAIN_MENU_BUTTONS = [
    ("Company Assign", "menu:companyassign"),
    ("Move Company", "menu:movecompany"),
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
                    chat_id, self._main_menu_text(chat_id, team_id), self._main_menu_buttons(chat_id, team_id), columns=1,
                )
            return

        session = self.config_store.get_onboarding_session(chat_id)
        if session is not None:
            state, data = session
            if state.startswith("AWAITING_ROTATION:") or state.startswith("AWAITING_ROTATION_TENANT:"):
                # Always legitimate - only ever created for an already-
                # registered team (see _prompt_rotation) - takes priority
                # over everything else for this chat_id. _handle_rotation_reply
                # itself branches on which of the two states this is (token
                # step vs the tenant_id step that follows for Factor/Leader).
                self._handle_rotation_reply(chat_id, state, data, raw_text)
            elif state == "AWAITING_COMPANY_NAME":
                # Always legitimate - only ever created for an already-
                # registered team (see _prompt_create_section).
                self._handle_create_section_reply(chat_id, data, raw_text)
            elif state == "AWAITING_MOVE_COMPANY_NAME":
                # Always legitimate - only ever created for an already-
                # registered team (see _prompt_move_company_name).
                self._handle_move_company_name_reply(chat_id, data, raw_text)
            elif state == "AWAITING_STAFF_ADD":
                # Always legitimate - only ever created for an already-
                # registered team (see _prompt_staff_roster_add).
                self._handle_staff_add_reply(chat_id, data, raw_text)
            elif state == "AWAITING_COMMIT_LABEL":
                # Always legitimate - only ever created for an already-
                # registered team (see _prompt_commit_label).
                self._handle_commit_label_reply(chat_id, data, raw_text)
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

    def _main_menu_buttons(self, chat_id, team_id=None):
        buttons = list(MAIN_MENU_BUTTONS)
        if team_id is not None:
            # Every team controls its own sync directly from the main menu
            # - not just via the hidden /pause and /resume text commands
            # (which still work too, unchanged). Label reflects current
            # status so it's always the single correct next action.
            team = self.config_store.get_team(team_id)
            if team["status"] == "paused":
                buttons.append(("▶️ Resume Sync", "menu:resume"))
            else:
                buttons.append(("⏸ Pause Sync", "menu:pause"))
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
            elif data.startswith("moveco:"):
                self._handle_move_company_callback(chat_id, message_id, data)
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
                chat_id, self._main_menu_text(chat_id, team_id), self._main_menu_buttons(chat_id, team_id), columns=1,
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
                chat_id, message_id, self._main_menu_text(chat_id, team_id), buttons=self._main_menu_buttons(chat_id, team_id),
            )
        elif action == "companyassign":
            self._show_menu_create_section_boards(chat_id, message_id, team_id)
        elif action.startswith("createsection:"):
            self._prompt_create_section(chat_id, message_id, team_id, action.split(":", 1)[1])
        elif action == "movecompany":
            self._show_menu_movecompany_source_boards(chat_id, message_id, team_id)
        elif action == "rotate":
            buttons = [(label, f"menu:rotate:{cmd}") for cmd, (_, label, _tenant_field) in _ROTATABLE_FIELDS.items()]
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
        elif action == "staffroster:setlabel":
            self._prompt_commit_label(chat_id, message_id, team_id)
        elif action == "pause":
            self.config_store.update_team(team_id, status="paused")
            self.gateway.edit_message_text(
                chat_id, message_id, "Sync paused.", buttons=self._main_menu_buttons(chat_id, team_id),
            )
        elif action == "resume":
            self.config_store.update_team(team_id, status="active")
            self.gateway.edit_message_text(
                chat_id, message_id, "Sync resumed.", buttons=self._main_menu_buttons(chat_id, team_id),
            )
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
        commit_label = team.get("algo_service_account_label")
        text += f"\n\nCommit label for shared/admin logbook edits: {commit_label or '(not set - left blank)'}"
        buttons = [("Add Person", "menu:staffroster:add"), ("Set Commit Label", "menu:staffroster:setlabel"), BACK_BUTTON]
        self.gateway.edit_message_text(chat_id, message_id, text, buttons=buttons)

    def _prompt_staff_roster_add(self, chat_id, message_id, team_id):
        self.config_store.save_onboarding_session(chat_id, "AWAITING_STAFF_ADD", {"team_id": team_id})
        self.gateway.edit_message_text(
            chat_id, message_id,
            "Send the new person as 'FirstName: Code' (e.g. 'David: D195'), "
            "or /cancel to stop.",
        )

    def _prompt_commit_label(self, chat_id, message_id, team_id):
        self.config_store.save_onboarding_session(chat_id, "AWAITING_COMMIT_LABEL", {"team_id": team_id})
        self.gateway.edit_message_text(
            chat_id, message_id,
            "When a driver's logbook was last edited by a shared/admin "
            "account (not a named staff member), what should Staff ID "
            "History show for that? Send the label (e.g. your team name), "
            "or /skip to leave it blank instead, or /cancel to stop.",
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

    def _team_all_dispatch_project_ids(self, team):
        """Every dispatch board a team has, across BOTH board groups when
        a second one exists (see config_store's asana_project_ids_2,
        multi_sync.py's second-board-group support) - used by "Move
        Company" so a company can move between ANY two of a team's
        boards, not just within the primary group."""
        ids = [p.strip() for p in team["asana_project_ids"].split(",") if p.strip()]
        ids += [p.strip() for p in (team.get("asana_project_ids_2") or "").split(",") if p.strip()]
        return ids

    def _show_menu_movecompany_source_boards(self, chat_id, message_id, team_id):
        """The menu's "Move Company" entry - step 1 of 3 (pick the board
        the company is CURRENTLY on, type its exact name, pick the
        destination board - see _handle_move_company_name_reply/
        _handle_move_company_callback)."""
        team = self.config_store.get_team(team_id)
        client = asana_client.AsanaClient(team["asana_token"], [], self.logger)
        project_ids = self._team_all_dispatch_project_ids(team)
        buttons = [
            (name, f"moveco:src:{team_id}:{project_id}")
            for project_id, name in client.get_project_names(project_ids).items()
        ]
        buttons.append(BACK_BUTTON)
        self.gateway.edit_message_text(
            chat_id, message_id, "Which board is the company currently on?", buttons=buttons,
        )

    def _handle_move_company_callback(self, chat_id, message_id, data):
        """Routes both button-tap steps of the "Move Company" flow.
        callback_data shape: "moveco:src:<team_id>:<project_id>" (step 1)
        or "moveco:target:<team_id>:<project_id>" (step 3, after the
        company name was typed - see _handle_move_company_name_reply).
        team_id is embedded (not resolved from "currently active team")
        the same way companyassign:/assign: already do, since the button
        was generated for a specific team that might not still be this
        chat's active one by the time it's tapped."""
        _, sub_action, team_id, project_id = data.split(":", 3)
        if team_id not in self.config_store.team_ids_for_chat(chat_id):
            self.gateway.send_message(chat_id, "This button isn't for your team - ignoring.")
            return
        if sub_action == "src":
            self._prompt_move_company_name(chat_id, message_id, team_id, project_id)
        elif sub_action == "target":
            self._execute_move_company(chat_id, message_id, team_id, project_id)
        else:
            self.logger.warning("Unrecognized moveco sub-action: %r", data)

    def _prompt_move_company_name(self, chat_id, message_id, team_id, source_project_id):
        team = self.config_store.get_team(team_id)
        client = asana_client.AsanaClient(team["asana_token"], [], self.logger)
        source_project_name = client.get_project_names([source_project_id]).get(source_project_id, source_project_id)
        self.config_store.save_onboarding_session(
            chat_id, "AWAITING_MOVE_COMPANY_NAME",
            {"team_id": team_id, "source_project_id": source_project_id, "source_project_name": source_project_name},
        )
        self.gateway.edit_message_text(
            chat_id, message_id,
            f"Send the exact company name to move off of {source_project_name} (or /cancel to stop).",
            buttons=[BACK_BUTTON],
        )

    def _handle_move_company_name_reply(self, chat_id, data, raw_text):
        text = raw_text.strip()
        if text.lower() == "/cancel":
            self.config_store.clear_onboarding_session(chat_id)
            self.gateway.send_buttons(chat_id, "Cancelled.", [BACK_BUTTON])
            return

        team_id = data["team_id"]
        source_project_id = data["source_project_id"]
        source_project_name = data["source_project_name"]
        team = self.config_store.get_team(team_id)
        client = asana_client.AsanaClient(team["asana_token"], [], self.logger)

        typed_key = asana_client.normalize_company_name(text)
        match = next(
            (
                s for s in client._fetch_sections(source_project_id)
                if asana_client.normalize_company_name(s.get("name") or "") == typed_key
            ),
            None,
        )
        if match is None:
            self.gateway.send_message(
                chat_id,
                f"Couldn't find '{text}' on {source_project_name} - check the spelling "
                "and send it again, or /cancel to stop.",
            )
            return  # stays in AWAITING_MOVE_COMPANY_NAME so they can retype

        self.config_store.save_onboarding_session(
            chat_id, "AWAITING_MOVE_COMPANY_NAME",
            {
                "team_id": team_id, "source_project_id": source_project_id,
                "source_project_name": source_project_name,
                "company_name": match["name"], "source_section_gid": match["gid"],
            },
        )

        other_project_ids = [pid for pid in self._team_all_dispatch_project_ids(team) if pid != source_project_id]
        buttons = [
            (name, f"moveco:target:{team_id}:{project_id}")
            for project_id, name in client.get_project_names(other_project_ids).items()
        ]
        buttons.append(BACK_BUTTON)
        self.gateway.send_buttons(chat_id, f"Move '{match['name']}' to which board?", buttons)

    def _execute_move_company(self, chat_id, message_id, team_id, dest_project_id):
        """Step 3's answer - does the actual move. Every driver task
        currently in the source board's section for this company gets
        moved to a matching (or newly-created) section on the
        destination board via the already-existing move_task_to_section
        (asana_client.py - already handles the cross-project add/remove),
        then the now-empty source section is deleted.

        Each task's field values (Status/Vehicle Number/Violation/Staff
        ID/Staff ID History) are captured before the move and restored
        immediately after, rather than left for the next sync cycle to
        eventually rewrite - confirmed live (moving Austin Logistics/
        Mobal Trucking between Texas Day A/B) that a cross-project move
        only changes section/project membership, never custom field
        VALUES, since the destination project's fields are separate
        objects even when they share the same names."""
        session = self.config_store.get_onboarding_session(chat_id)
        if session is None or session[0] != "AWAITING_MOVE_COMPANY_NAME" or "company_name" not in session[1]:
            self.gateway.edit_message_text(
                chat_id, message_id,
                "That move isn't in progress anymore - start over from the menu.",
                buttons=[BACK_BUTTON],
            )
            return

        _, data = session
        source_project_id = data["source_project_id"]
        source_project_name = data["source_project_name"]
        company_name = data["company_name"]
        source_section_gid = data["source_section_gid"]

        if dest_project_id == source_project_id:
            self.gateway.edit_message_text(
                chat_id, message_id, "That's already the current board - nothing to do.", buttons=[BACK_BUTTON],
            )
            self.config_store.clear_onboarding_session(chat_id)
            return

        team = self.config_store.get_team(team_id)
        source_client = asana_client.AsanaClient(team["asana_token"], [source_project_id], self.logger)
        dest_client = asana_client.AsanaClient(team["asana_token"], [dest_project_id], self.logger)
        dest_project_name = dest_client.get_project_names([dest_project_id]).get(dest_project_id, dest_project_id)

        pre_task_index, _, _ = source_client.build_task_index()
        captured_by_gid = {}
        for matches in pre_task_index.values():
            for m in matches:
                if m.get("current_section_gid") == source_section_gid and m["task_gid"] not in captured_by_gid:
                    captured_by_gid[m["task_gid"]] = {
                        "status": m.get("current_status"),
                        # current_vehicle_number is already formatted for
                        # THIS project's field type (e.g. "#123" for a
                        # text field) - strip that back off so
                        # update_task_status's own vehicle_field_value
                        # call formats it correctly for the DESTINATION
                        # project's field instead of double-prefixing it.
                        "vehicle_number": (
                            str(m["current_vehicle_number"]).lstrip("#")
                            if m.get("current_vehicle_number") is not None else None
                        ),
                        "violation": m.get("current_violation"),
                        "staff_id": m.get("current_staff_id"),
                        "staff_history": m.get("current_staff_history"),
                    }

        dest_key = asana_client.normalize_company_name(company_name)
        dest_match = next(
            (
                s for s in dest_client._fetch_sections(dest_project_id)
                if asana_client.normalize_company_name(s.get("name") or "") == dest_key
            ),
            None,
        )
        dest_section_gid = dest_match["gid"] if dest_match else dest_client.create_section(dest_project_id, company_name)
        dest_section_info = {"project_id": dest_project_id, "section_gid": dest_section_gid}

        task_gids = source_client.list_section_task_gids(source_section_gid)
        for task_gid in task_gids:
            try:
                source_client.move_task_to_section(task_gid, source_project_id, dest_section_info)
            except Exception:
                self.logger.exception("Move Company: failed to move task %s for '%s'.", task_gid, company_name)

        if captured_by_gid:
            post_task_index, _, _ = dest_client.build_task_index()
            post_matches_by_gid = {
                m["task_gid"]: m
                for matches in post_task_index.values()
                for m in matches
                if m.get("current_section_gid") == dest_section_gid
            }
            for task_gid, values in captured_by_gid.items():
                new_match = post_matches_by_gid.get(task_gid)
                if new_match is None:
                    continue
                try:
                    dest_client.update_task_status(
                        new_match, values["status"], values["vehicle_number"], values["violation"],
                        values["staff_id"], values["staff_history"],
                    )
                except Exception:
                    self.logger.exception("Move Company: failed to restore field values for task %s.", task_gid)

        source_client.delete_section(source_section_gid)

        self.config_store.clear_onboarding_session(chat_id)
        self.gateway.edit_message_text(
            chat_id, message_id,
            f"Moved {len(task_gids)} driver(s) for '{company_name}' from "
            f"{source_project_name} to {dest_project_name}.",
            buttons=[BACK_BUTTON],
        )

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

    def _handle_commit_label_reply(self, chat_id, data, raw_text):
        text = raw_text.strip()
        if text.lower() == "/cancel":
            self.config_store.clear_onboarding_session(chat_id)
            self.gateway.send_buttons(chat_id, "Cancelled.", [BACK_BUTTON])
            return

        team_id = data["team_id"]
        if text.lower() == "/skip":
            self.provisioning.set_commit_label(team_id, "")
            self.config_store.clear_onboarding_session(chat_id)
            self.gateway.send_buttons(
                chat_id, "Cleared - shared/admin logbook edits will be left blank.", [BACK_BUTTON],
            )
            return

        label = _clean_pasted_value(text)
        self.provisioning.set_commit_label(team_id, label)
        self.config_store.clear_onboarding_session(chat_id)
        self.gateway.send_buttons(
            chat_id, f"Commit label set to '{label}' - takes effect on the next sync cycle.", [BACK_BUTTON],
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
        field_name, label, tenant_field = _ROTATABLE_FIELDS[command_text]
        self.config_store.save_onboarding_session(
            chat_id, f"AWAITING_ROTATION:{field_name}",
            {"team_id": team_id, "label": label, "tenant_field": tenant_field},
        )
        # "« Back to Bot" here is equivalent to /cancel (see menu:main's
        # handling, which always clears any pending onboarding/rotation
        # session first) - never a dead end that leaves a rotation
        # silently waiting for the next thing you happen to type.
        self.gateway.send_buttons(chat_id, f"Paste the new {label} token now (or /cancel to stop).", [BACK_BUTTON])

    def _validate_rotation_pair(self, field_name, new_token, new_tenant_id):
        """Live-checks a Factor/Leader ELD token TOGETHER with its
        tenant_id, before committing either - see _ROTATABLE_FIELDS'
        comment for why these two are never rotated independently."""
        if field_name == "factor_session_token":
            return validators.check_factor(new_token, new_tenant_id)
        if field_name == "leader_session_token":
            return validators.check_leader(new_token, new_tenant_id)
        return False, "unknown field"

    def _handle_rotation_reply(self, chat_id, state, data, raw_text):
        if raw_text.strip().lower() == "/cancel":
            self.config_store.clear_onboarding_session(chat_id)
            self.gateway.send_buttons(chat_id, "Cancelled - nothing changed.", [BACK_BUTTON])
            return

        if state.startswith("AWAITING_ROTATION_TENANT:"):
            self._handle_rotation_tenant_reply(chat_id, state, data, raw_text)
            return

        field_name = state.split(":", 1)[1]
        new_value = _clean_pasted_value(raw_text)
        # Cheap, fast-failing check before ever hitting the network - the
        # exact shape of a real past incident: a bot command pasted
        # instead of a token silently overwrote a real credential because
        # nothing checked the value looked like one first.
        if not new_value or new_value.startswith("/"):
            self.gateway.send_message(
                chat_id, "That doesn't look like a token - paste the actual token value, or /cancel to stop.",
            )
            return

        team_id = data["team_id"]
        tenant_field = data.get("tenant_field")

        if tenant_field is None:
            # Asana - no tenant_id concept, single-step as before.
            ok, result = validators.check_asana(new_value)
            message = f"{len(result)} workspace(s) visible" if ok else result
            if not ok:
                self.gateway.send_message(
                    chat_id, f"That {data['label']} token was rejected: {message}\n\nPaste it again, or /cancel to stop.",
                )
                return
            self.config_store.update_team(team_id, **{field_name: new_value})
            self.config_store.clear_onboarding_session(chat_id)
            self.provisioning.rewrite_env(team_id)
            self.gateway.send_buttons(
                chat_id, f"{data['label']} token updated ({message}) - takes effect on the next sync cycle.", [BACK_BUTTON],
            )
            return

        # Factor/Leader ELD - hold the new token, ask for its matching
        # tenant_id next, and validate the PAIR together before saving
        # either (see _ROTATABLE_FIELDS' comment).
        data["pending_token"] = new_value
        self.config_store.save_onboarding_session(chat_id, f"AWAITING_ROTATION_TENANT:{field_name}", data)
        self.gateway.send_message(chat_id, f"Now paste the matching {data['label']} tenant_id (or /cancel to stop).")

    def _handle_rotation_tenant_reply(self, chat_id, state, data, raw_text):
        new_tenant_id = _clean_pasted_value(raw_text)
        if not new_tenant_id or new_tenant_id.startswith("/"):
            self.gateway.send_message(
                chat_id, "That doesn't look like a tenant_id - paste it again, or /cancel to stop.",
            )
            return

        field_name = state.split(":", 1)[1]
        team_id = data["team_id"]
        tenant_field = data["tenant_field"]
        ok, message = self._validate_rotation_pair(field_name, data["pending_token"], new_tenant_id)
        if not ok:
            self.gateway.send_message(
                chat_id,
                f"That {data['label']} token/tenant_id pair was rejected: {message}\n\n"
                f"Paste the {data['label']} token again, or /cancel to stop.",
            )
            # Back to square one (fresh token) rather than re-asking just
            # for a tenant_id against a token we now know is unverified.
            self.config_store.save_onboarding_session(
                chat_id, f"AWAITING_ROTATION:{field_name}",
                {"team_id": team_id, "label": data["label"], "tenant_field": tenant_field},
            )
            return

        self.config_store.update_team(team_id, **{field_name: data["pending_token"], tenant_field: new_tenant_id})
        self.config_store.clear_onboarding_session(chat_id)
        self.provisioning.rewrite_env(team_id)
        self.gateway.send_buttons(
            chat_id,
            f"{data['label']} token + tenant_id updated ({message}) - takes effect on the next sync cycle.",
            [BACK_BUTTON],
        )
