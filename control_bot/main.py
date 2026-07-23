"""
control_bot/main.py

Entrypoint for the singleton control-bot process - the one process allowed
to long-poll the shared Telegram bot token (see gateway.py's docstring for
why). Reads its own small .env (control_bot/.env, see .env.example in this
directory) - never the per-team ones under teams/<team_id>/.

This process now also runs multi_sync.py's shared per-team sync loop on a
background thread, alongside the Telegram bot itself - one process, every
active team's dispatch/Database/Odometer sync, no per-team OS process to
spawn (see multi_sync.py's own docstring for why this replaced the old
supervisor.py-based design). Set MULTI_SYNC_ENABLED=false to disable this
and run only the bot (e.g. if you're still running teams via the old
per-process sync.py + supervisor.py setup instead).
"""

import logging
import os
import threading
import time

from dotenv import load_dotenv

import multi_sync
from control_bot import validators
from control_bot.config_store import ConfigStore
from control_bot.gateway import TelegramGateway
from control_bot.onboarding import OnboardingManager
from control_bot.provisioning import Provisioner
from control_bot.router import TeamRouter

_HERE = os.path.dirname(__file__)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler("control_bot.log", encoding="utf-8")],
)
logger = logging.getLogger("control_bot")


def main():
    load_dotenv(os.path.join(_HERE, ".env"))

    bot_token = os.environ["CONTROL_BOT_TELEGRAM_TOKEN"]
    db_path = os.environ.get("CONFIG_DB_PATH", os.path.join(_HERE, "config.db"))
    master_key_path = os.environ.get("CONFIG_MASTER_KEY_PATH", os.path.join(_HERE, "master.key"))
    teams_root_dir = os.environ.get("TEAMS_ROOT_DIR", os.path.join(os.path.dirname(_HERE), "teams"))

    config_store = ConfigStore(db_path, master_key_path)
    provisioning = Provisioner(config_store, teams_root_dir, bot_token, logger)

    gateway = TelegramGateway(bot_token, on_message=None, logger=logger)
    onboarding = OnboardingManager(gateway, config_store, validators, provisioning, logger)
    router = TeamRouter(gateway, config_store, onboarding, provisioning, logger)
    gateway.on_message = router.handle_update

    gateway.start()

    if os.environ.get("MULTI_SYNC_ENABLED", "true").strip().lower() != "false":
        poll_interval_minutes = float(os.environ.get("POLL_INTERVAL_MINUTES", "5"))
        sync_logger = logging.getLogger("multi_sync")
        sync_thread = threading.Thread(
            target=multi_sync.sync_loop_forever,
            args=(config_store, bot_token, sync_logger, poll_interval_minutes),
            daemon=True,
        )
        sync_thread.start()
        logger.info("Multi-team sync loop started (every %s minute(s)).", poll_interval_minutes)

        fmcsa_interval_seconds = float(os.environ.get("FMCSA_CHECK_INTERVAL_SECONDS", "30"))
        fmcsa_logger = logging.getLogger("multi_sync.fmcsa")
        fmcsa_thread = threading.Thread(
            target=multi_sync.fmcsa_check_loop_forever,
            args=(config_store, bot_token, fmcsa_logger, fmcsa_interval_seconds),
            daemon=True,
        )
        fmcsa_thread.start()
        logger.info("HOS Audit Transfer check loop started (every %s second(s)).", fmcsa_interval_seconds)
    else:
        logger.info("MULTI_SYNC_ENABLED=false - bot only, no sync loop in this process.")

    logger.info("Control bot running. Ctrl+C to stop.")
    try:
        while True:
            time.sleep(30)
    except KeyboardInterrupt:
        gateway.stop()


if __name__ == "__main__":
    main()
