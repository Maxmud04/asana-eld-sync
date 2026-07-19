"""
telegram_control.py

A tiny Telegram bot that lets you pause and resume the sync loop, and check
its status, straight from a Telegram chat - useful if you want to
temporarily stop the sync (for example during maintenance in Factor ELD or
Asana) without shutting down the whole program.

If ALLOWED_TELEGRAM_IDS (in your .env file) lists one or more Telegram user
IDs, only those accounts can control it - messages from anyone else are
ignored. If it's left empty, the bot has NO allowlist at all: any Telegram
account that finds it can run every command below, including /settoken,
which overwrites your live Factor ELD credential. That's an explicit,
deliberate choice - not a default - since anyone who discovers the bot's
username could then pause the sync or replace the token at will.

Supported commands (send these as a normal message to your bot):
    /pause              - stop running sync cycles until resumed
    /resume             - go back to the normal schedule
    /status             - ask whether the sync is currently paused or running
    /settoken <token>   - update the Factor ELD session token (used when the
                          old one expires - lifetime varies, confirmed
                          anywhere from under a day to about 30 days)
                          without needing to edit .env by hand or restart
                          the program

This talks to Telegram's HTTP API directly (no extra library needed) and
runs in its own background thread, so it works alongside sync.py's normal
timed loop without blocking it.

SECURITY NOTE: /settoken saves the token both in memory (takes effect
immediately) and in your .env file (so it survives a restart). Since it
travels through a Telegram message, it's slightly less private than editing
.env directly - fine given only your allow-listed Telegram account(s) can
use this bot, but worth knowing.
"""

import os
import re
import threading
import time

import requests

# Matches a JWT-shaped token (three dot-separated base64url segments, e.g.
# "eyJhbGciOi....eyJleHAiOi....abc123") without needing the /settoken prefix
# typed first - lets someone just paste the raw token straight from their
# browser. Deliberately strict (won't match ordinary chat text) since it's
# used to auto-apply a credential update with no other confirmation step.
_JWT_LIKE_PATTERN = re.compile(r"^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$")

TELEGRAM_API_BASE = "https://api.telegram.org/bot{token}/{method}"
POLL_TIMEOUT_SECONDS = 30  # how long each check-for-new-messages call waits
ENV_FILE_PATH = ".env"

# A persistent on-screen keyboard, laid out 2x2, so commands can be tapped
# instead of typed. Tapping a button just sends its label as a normal text
# message; _handle_update() matches on the "/word" inside each label, so the
# icon is just decoration and doesn't need special-case handling.
REPLY_KEYBOARD = {
    "keyboard": [
        ["⏸️ /pause", "▶️ /resume"],
        ["📊 /status", "🔑 /settoken"],
    ],
    "resize_keyboard": True,
}


def _update_env_file(key, value):
    """Update one KEY=value line in the .env file, leaving every other line
    untouched. Adds the line at the end if the key wasn't already there."""
    with open(ENV_FILE_PATH, "r", encoding="utf-8") as f:
        lines = f.readlines()

    found = False
    new_lines = []
    for line in lines:
        if line.strip().startswith(f"{key}="):
            new_lines.append(f"{key}={value}\n")
            found = True
        else:
            new_lines.append(line)
    if not found:
        new_lines.append(f"{key}={value}\n")

    with open(ENV_FILE_PATH, "w", encoding="utf-8") as f:
        f.writelines(new_lines)


class TelegramControl:
    def __init__(self, bot_token, allowed_ids, logger):
        self.bot_token = bot_token
        self.allowed_ids = set(allowed_ids)
        # An empty allowlist means "no restriction" - every Telegram account
        # can issue commands. See the module docstring's SECURITY NOTE.
        self.open_access = not self.allowed_ids
        self.logger = logger
        self.paused = False
        self._last_update_id = 0
        self._stop_requested = False
        # Every chat that has ever sent this bot a message, so notify_all()
        # has somewhere to proactively push an alert to (e.g. "the Factor
        # ELD token just died") without needing a hardcoded chat id in .env.
        # Persisted to .env (see _save_known_chat_ids) so a restart of this
        # program - which happens often - doesn't forget who to alert until
        # they happen to message the bot again.
        raw_ids = os.environ.get("KNOWN_TELEGRAM_CHAT_IDS", "").strip()
        self._known_chat_ids = {
            int(x.strip()) for x in raw_ids.split(",") if x.strip()
        }

    def is_paused(self):
        return self.paused

    def start(self):
        """Start listening for Telegram messages in a background thread,
        so it doesn't block the main sync loop."""
        thread = threading.Thread(target=self._poll_loop, daemon=True)
        thread.start()
        self.logger.info(
            "Telegram control bot started - listening for /pause, /resume, /status"
        )
        if self.open_access:
            self.logger.warning(
                "ALLOWED_TELEGRAM_IDS is empty - the Telegram bot has NO "
                "allowlist. Any Telegram account that finds it can pause the "
                "sync or overwrite the Factor ELD token with /settoken."
            )

    def stop(self):
        self._stop_requested = True

    def _call(self, method, **params):
        url = TELEGRAM_API_BASE.format(token=self.bot_token, method=method)
        resp = requests.get(url, params=params, timeout=POLL_TIMEOUT_SECONDS + 10)
        resp.raise_for_status()
        return resp.json()

    def _send_message(self, chat_id, text, reply_markup=REPLY_KEYBOARD):
        url = TELEGRAM_API_BASE.format(token=self.bot_token, method="sendMessage")
        payload = {"chat_id": chat_id, "text": text}
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        try:
            requests.post(url, json=payload, timeout=15)
        except requests.RequestException as exc:
            self.logger.warning("Failed to send Telegram reply: %s", exc)

    def _save_known_chat_ids(self):
        try:
            _update_env_file(
                "KNOWN_TELEGRAM_CHAT_IDS",
                ",".join(str(cid) for cid in sorted(self._known_chat_ids)),
            )
        except OSError as exc:
            self.logger.warning("Failed to save known Telegram chat ids to .env: %s", exc)

    def notify_all(self, text):
        """Proactively push a message to every chat that's ever messaged
        this bot (see _known_chat_ids) - used for alerts the sync loop
        raises on its own, like a dead Factor ELD token, rather than in
        reply to an incoming command. Does nothing if no one has messaged
        the bot yet (nowhere to send it)."""
        for chat_id in self._known_chat_ids:
            self._send_message(chat_id, text)

    def _poll_loop(self):
        """Repeatedly ask Telegram for any new messages (long polling) and
        react to commands. Runs until stop() is called."""
        while not self._stop_requested:
            try:
                result = self._call(
                    "getUpdates",
                    offset=self._last_update_id + 1,
                    timeout=POLL_TIMEOUT_SECONDS,
                )
            except requests.RequestException as exc:
                self.logger.warning("Telegram polling error (will retry): %s", exc)
                time.sleep(5)
                continue

            for update in result.get("result", []):
                self._last_update_id = update["update_id"]
                try:
                    self._handle_update(update)
                except Exception:
                    # Never let one bad message kill the whole listener -
                    # log it and keep polling for the next message.
                    self.logger.exception("Error handling a Telegram message - ignoring it.")

    def _handle_update(self, update):
        message = update.get("message")
        if not message:
            return

        chat_id = message["chat"]["id"]
        sender_id = message.get("from", {}).get("id")
        # Keep the original text (preserving case) for anything that carries
        # a real value, like a token - only use a lower-cased copy to figure
        # out which command was sent.
        raw_text = (message.get("text") or "").strip()
        text = raw_text.lower()

        if not self.open_access and sender_id not in self.allowed_ids:
            self.logger.warning(
                "Ignored Telegram message from unauthorized user id %s", sender_id
            )
            return

        # Only track chats we've actually authorized above - an unauthorized
        # sender shouldn't end up receiving proactive alerts about the
        # sync's internal health (see notify_all).
        if chat_id not in self._known_chat_ids:
            self._known_chat_ids.add(chat_id)
            self._save_known_chat_ids()

        if text == "/start":
            self._send_message(chat_id, "Menu:")
        elif "/pause" in text:
            self.paused = True
            self.logger.info("Sync paused via Telegram by user %s", sender_id)
            self._send_message(chat_id, "Sync paused. Send /resume to continue.")
        elif "/resume" in text:
            self.paused = False
            self.logger.info("Sync resumed via Telegram by user %s", sender_id)
            self._send_message(chat_id, "Sync resumed - back to the normal schedule.")
        elif "/status" in text:
            state = "paused" if self.paused else "running normally"
            self._send_message(chat_id, f"Sync is currently {state}.")
        elif "/settoken" in text:
            new_token = raw_text[raw_text.lower().rindex("/settoken") + len("/settoken"):].strip()
            if not new_token:
                self._send_message(
                    chat_id, "Usage: /settoken <paste the new Factor ELD token here>"
                )
            else:
                self._apply_new_token(chat_id, sender_id, new_token)
        elif _JWT_LIKE_PATTERN.match(
            raw_text.strip().rstrip(",").strip().strip('"').strip("'").strip()
        ):
            # No /settoken typed at all - just a bare token pasted straight
            # in. Recognized by shape (three dot-separated base64url
            # segments) rather than requiring the command prefix.
            self._apply_new_token(chat_id, sender_id, raw_text)
        else:
            self._send_message(
                chat_id,
                "Unknown command. Available commands: /pause, /resume, /status, /settoken <token>",
            )

    def _apply_new_token(self, chat_id, sender_id, raw_token):
        """Save a new Factor ELD session token, in memory and in .env, and
        confirm back to the sender. Shared by the explicit /settoken command
        and by recognizing a bare pasted token (see _JWT_LIKE_PATTERN)."""
        # People often copy the token straight out of a browser's JSON
        # viewer, which includes the surrounding quotes and a trailing comma
        # (e.g. "eyJ...", ) - strip those off automatically so a copy-paste
        # artifact doesn't quietly break authentication.
        new_token = raw_token.strip().rstrip(",").strip().strip('"').strip("'").strip()
        if not new_token:
            self._send_message(
                chat_id, "Usage: /settoken <paste the new Factor ELD token here>"
            )
            return

        os.environ["FACTOR_SESSION_TOKEN"] = new_token
        try:
            _update_env_file("FACTOR_SESSION_TOKEN", new_token)
        except OSError as exc:
            self.logger.warning("Failed to save new token to .env: %s", exc)
        self.logger.info(
            "Factor ELD session token updated via Telegram by user %s", sender_id
        )
        self._send_message(
            chat_id,
            "Factor ELD token updated - it'll be used starting with the next sync cycle.",
        )
