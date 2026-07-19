"""
control_bot/gateway.py

The low-level Telegram HTTP transport for the control-bot process - lifted
from telegram_control.py's _call/_send_message/_poll_loop, generalized to
call back into a supplied handler instead of hardcoding single-team command
logic itself (that routing now lives in router.py, since it has to dispatch
by team instead of assuming there's only ever one, like telegram_control.py
does).

This is the ONLY thing in the whole multi-tenant setup allowed to call
Telegram's getUpdates (long-polling) - see the plan's note that two
processes can't poll the same bot token without racing each other for
updates. Every per-team sync.py process only ever SENDS messages (see
telegram_notifier.py), never polls.
"""

import logging
import threading
import time

import requests

TELEGRAM_API_BASE = "https://api.telegram.org/bot{token}/{method}"
POLL_TIMEOUT_SECONDS = 30


class TelegramGateway:
    def __init__(self, bot_token, on_message, logger=None):
        self.bot_token = bot_token
        self.on_message = on_message
        self.logger = logger or logging.getLogger(__name__)
        self._last_update_id = 0
        self._stop_requested = False

    def start(self):
        thread = threading.Thread(target=self._poll_loop, daemon=True)
        thread.start()
        self.logger.info("Control-bot Telegram gateway started - listening for messages.")

    def stop(self):
        self._stop_requested = True

    def _call(self, method, **params):
        url = TELEGRAM_API_BASE.format(token=self.bot_token, method=method)
        resp = requests.get(url, params=params, timeout=POLL_TIMEOUT_SECONDS + 10)
        resp.raise_for_status()
        return resp.json()

    def send_message(self, chat_id, text, reply_markup=None):
        url = TELEGRAM_API_BASE.format(token=self.bot_token, method="sendMessage")
        payload = {"chat_id": chat_id, "text": text}
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        try:
            requests.post(url, json=payload, timeout=15)
        except requests.RequestException as exc:
            self.logger.warning("Failed to send Telegram reply: %s", exc)

    def send_buttons(self, chat_id, text, buttons, columns=2):
        """Send text with a Telegram inline keyboard - buttons is a list of
        (label, callback_data) pairs, laid out `columns` per row (2 by
        default, matching the reference screenshot's layout). Tapping a
        button delivers an update["callback_query"] to on_message, with
        that exact callback_data in its "data" field - see router.py."""
        rows = [
            [{"text": label, "callback_data": data} for label, data in buttons[i:i + columns]]
            for i in range(0, len(buttons), columns)
        ]
        self.send_message(chat_id, text, reply_markup={"inline_keyboard": rows})

    def answer_callback_query(self, callback_query_id, text=None):
        """Acknowledge a button tap - Telegram shows a permanent loading
        spinner on the tapped button until this is called, regardless of
        whether anything else is done in response."""
        url = TELEGRAM_API_BASE.format(token=self.bot_token, method="answerCallbackQuery")
        payload = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text
        try:
            requests.post(url, json=payload, timeout=15)
        except requests.RequestException as exc:
            self.logger.warning("Failed to answer Telegram callback query: %s", exc)

    def edit_message_text(self, chat_id, message_id, text, buttons=None, columns=2):
        """Replace a previous message's text - and, if buttons is given,
        its inline keyboard too (e.g. drilling from a single "Company
        Assign" button into the real board choices, or removing the
        keyboard entirely once it's been acted on by omitting buttons)."""
        url = TELEGRAM_API_BASE.format(token=self.bot_token, method="editMessageText")
        payload = {"chat_id": chat_id, "message_id": message_id, "text": text}
        if buttons:
            rows = [
                [{"text": label, "callback_data": data} for label, data in buttons[i:i + columns]]
                for i in range(0, len(buttons), columns)
            ]
            payload["reply_markup"] = {"inline_keyboard": rows}
        try:
            requests.post(url, json=payload, timeout=15)
        except requests.RequestException as exc:
            self.logger.warning("Failed to edit Telegram message: %s", exc)

    def _poll_loop(self):
        """Runs until stop() is called."""
        while not self._stop_requested:
            try:
                result = self._call(
                    "getUpdates", offset=self._last_update_id + 1, timeout=POLL_TIMEOUT_SECONDS,
                )
            except requests.RequestException as exc:
                self.logger.warning("Telegram polling error (will retry): %s", exc)
                time.sleep(5)
                continue

            for update in result.get("result", []):
                self._last_update_id = update["update_id"]
                try:
                    self.on_message(update)
                except Exception:
                    # Never let one bad message kill the whole listener.
                    self.logger.exception("Error handling a Telegram message - ignoring it.")
