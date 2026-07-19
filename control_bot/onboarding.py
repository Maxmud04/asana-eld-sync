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
STATE_ASK_STAFF_ROSTER = "ASK_STAFF_ROSTER"
STATE_CONFIRM = "CONFIRM"

# "David: D195" / "David - D195" / "David, D195" - one staff roster entry
# per line, name then code, any of a few common separators.
_ROSTER_LINE_PATTERN = re.compile(r"^\s*([A-Za-z][A-Za-z\-' ]*)\s*[:,\-]\s*([A-Za-z0-9#]+)\s*$")


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
        onboarding (see router.py's _handle_callback_query). Reuses the
        exact same _handle_workspace_choice/_handle_org_team_choice index
        parsing the text-reply path already had - callback_data carries a
        0-based index ("onboard_workspace:2"), converted to the 1-based
        text form those methods expect, so the underlying logic isn't
        duplicated between the button and (still-supported, in case
        someone types a number out of habit) text paths."""
        session = self.config_store.get_onboarding_session(chat_id)
        if session is None:
            return
        state, data = session
        prefix, _, index_str = callback_data.partition(":")
        try:
            one_based = str(int(index_str) + 1)
        except ValueError:
            return
        if prefix == "onboard_workspace" and state == STATE_ASK_WORKSPACE_CHOICE:
            self._handle_workspace_choice(chat_id, sender_id, data, one_based)
        elif prefix == "onboard_orgteam" and state == STATE_ASK_ORG_TEAM_CHOICE:
            self._handle_org_team_choice(chat_id, sender_id, data, one_based)

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
        data["factor_session_token"] = text
        self._advance(chat_id, STATE_ASK_FACTOR_TENANT, data)
        self.gateway.send_message(chat_id, "What's your Factor ELD tenant_id?")

    def _handle_factor_tenant(self, chat_id, sender_id, data, text):
        data["factor_tenant_id"] = text
        ok, message = self.validators.check_factor(data["factor_session_token"], text)
        if not ok:
            self._advance(chat_id, STATE_ASK_FACTOR_TOKEN, data)
            self.gateway.send_message(
                chat_id, f"That Factor ELD token/tenant was rejected: {message}\n\nPaste the token again.",
            )
            return
        self.gateway.send_message(chat_id, f"Factor ELD confirmed ({message}).")
        self._advance(chat_id, STATE_ASK_LEADER_TOKEN, data)
        self.gateway.send_message(chat_id, "Paste your Leader ELD session token (or /skip).")

    def _handle_leader_token(self, chat_id, sender_id, data, text):
        if text.lower() == "/skip":
            data["leader_session_token"] = None
            self._advance(chat_id, STATE_ASK_ASANA_TOKEN, data)
            self.gateway.send_message(chat_id, "Now paste your Asana personal access token.")
            return
        data["leader_session_token"] = text
        self._advance(chat_id, STATE_ASK_LEADER_TENANT, data)
        self.gateway.send_message(chat_id, "What's your Leader ELD tenant_id?")

    def _handle_leader_tenant(self, chat_id, sender_id, data, text):
        data["leader_tenant_id"] = text
        ok, message = self.validators.check_leader(data["leader_session_token"], text)
        if not ok:
            self._advance(chat_id, STATE_ASK_LEADER_TOKEN, data)
            self.gateway.send_message(
                chat_id, f"That Leader ELD token/tenant was rejected: {message}\n\nPaste the token again.",
            )
            return
        self.gateway.send_message(chat_id, f"Leader ELD confirmed ({message}).")
        self._advance(chat_id, STATE_ASK_ASANA_TOKEN, data)
        self.gateway.send_message(chat_id, "Now paste your Asana personal access token.")

    def _handle_asana_token(self, chat_id, sender_id, data, text):
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
        self._ask_staff_roster(chat_id, data)

    def _handle_org_team_choice(self, chat_id, sender_id, data, text):
        choices = data.get("_org_team_choices", [])
        try:
            chosen = choices[int(text) - 1]
        except (ValueError, IndexError):
            self.gateway.send_message(chat_id, "Please reply with just the number.")
            return
        data["asana_team_gid"] = chosen["gid"]
        data.pop("_org_team_choices", None)
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
        summary = (
            f"Team: {data['team_name']}\n"
            f"Factor ELD: {'configured' if data.get('factor_session_token') else 'skipped'}\n"
            f"Leader ELD: {'configured' if data.get('leader_session_token') else 'skipped'}\n"
            f"Asana workspace: {data['workspace_gid']}\n"
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
            # actually includes them, then bounce the process to pick it up.
            self.provisioning.rewrite_env(team_id)
            self.provisioning.supervisor.rotate_and_restart(team_id)
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
