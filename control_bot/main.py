"""
control_bot/main.py

Entrypoint for the singleton control-bot process - the one process allowed
to long-poll the shared Telegram bot token (see gateway.py's docstring for
why). Reads its own small .env (control_bot/.env, see .env.example in this
directory) - never the per-team ones under teams/<team_id>/.
"""

import logging
import os
import time

from dotenv import load_dotenv

from control_bot import validators
from control_bot.config_store import ConfigStore
from control_bot.gateway import TelegramGateway
from control_bot.onboarding import OnboardingManager
from control_bot.provisioning import Provisioner
from control_bot.router import TeamRouter
from control_bot.supervisor import Supervisor

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
    supervisor = Supervisor(teams_root_dir, logger)
    provisioning = Provisioner(config_store, supervisor, teams_root_dir, bot_token, logger)

    gateway = TelegramGateway(bot_token, on_message=None, logger=logger)
    onboarding = OnboardingManager(gateway, config_store, validators, provisioning, logger)
    router = TeamRouter(gateway, config_store, onboarding, provisioning, supervisor, logger)
    gateway.on_message = router.handle_update

    gateway.start()
    logger.info("Control bot running. Ctrl+C to stop.")
    try:
        while True:
            time.sleep(30)
    except KeyboardInterrupt:
        gateway.stop()


if __name__ == "__main__":
    main()
