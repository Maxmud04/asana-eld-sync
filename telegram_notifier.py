"""
telegram_notifier.py

Outbound-only Telegram alerting for one team's isolated sync.py process, in
the multi-tenant control panel design where a single shared Telegram bot
token is the whole control surface (see the plan: onboarding, token
rotation, /status, all handled by one singleton control_bot process).

Telegram's long-polling (getUpdates) requires exclusive ownership of a bot
token - two processes can't poll the same token at once without racing each
other for updates. Only the control_bot process is allowed to poll. Sending
a message has no such restriction, so each team's own isolated sync.py
process can safely keep using that same shared bot token for OUTBOUND-ONLY
alerts (a dead token, an expiring-soon warning) via this class.

This duck-types the two methods sync.py actually calls on its "control"
object - is_paused() and notify_all() - so main() only needs to decide
which class to construct; run_one_cycle(), run_database_cycle(),
_check_token_expiry_warning(), and _handle_factor_fetch_failure() all stay
exactly as they are, unchanged. telegram_control.py (with its interactive
/pause, /resume, /settoken commands) is left as-is for single-tenant/local
use - it is NOT used at all in this multi-tenant mode, since its polling
would conflict with the control_bot process polling the same token.

Pause/resume for an isolated per-team process is driven by a flag FILE
(paused_flag_path, default "paused.flag" in this process's own working
directory) that the control_bot process writes/deletes in response to a
team's /pause or /resume command - never by anything in this file.
"""

import os

import requests

TELEGRAM_API_BASE = "https://api.telegram.org/bot{token}/{method}"
PAUSED_FLAG_PATH = "paused.flag"


class TelegramNotifier:
    def __init__(self, bot_token, chat_ids, logger, paused_flag_path=PAUSED_FLAG_PATH):
        self.bot_token = bot_token
        self.chat_ids = set(chat_ids)
        self.logger = logger
        self.paused_flag_path = paused_flag_path

    def is_paused(self):
        """Reads a flag file rather than in-memory state, since pause/resume
        for this process is driven by the separate control_bot process (see
        module docstring), not by anything happening inside this process."""
        return os.path.exists(self.paused_flag_path)

    def notify_all(self, text):
        """Send one outbound alert to every chat_id configured for this
        team. Safe to call from N different per-team processes all sharing
        the same bot token, since sending a message (unlike getUpdates)
        needs no exclusive ownership. Does nothing if no chat_ids are
        configured for this team yet."""
        self.notify_with_buttons(text, None)

    def notify_with_buttons(self, text, buttons, columns=2):
        """Same as notify_all, but with an inline keyboard attached -
        buttons is a list of (label, callback_data) pairs, or None/empty
        for plain text. Used for the new-company → pick-a-board alert
        (see sync.py/pending_companies.py) - tapping a button is handled by
        control_bot/router.py, since only that process polls for updates;
        sending never requires the same exclusive ownership polling does."""
        if not self.bot_token or not self.chat_ids:
            return
        url = TELEGRAM_API_BASE.format(token=self.bot_token, method="sendMessage")
        payload_base = {"text": text}
        if buttons:
            rows = [
                [{"text": label, "callback_data": data} for label, data in buttons[i:i + columns]]
                for i in range(0, len(buttons), columns)
            ]
            payload_base["reply_markup"] = {"inline_keyboard": rows}
        for chat_id in self.chat_ids:
            try:
                requests.post(url, json={**payload_base, "chat_id": chat_id}, timeout=15)
            except requests.RequestException as exc:
                self.logger.warning("Failed to send Telegram alert to chat %s: %s", chat_id, exc)
