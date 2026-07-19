"""
control_bot/register_existing_team.py

One-off script (run once, not part of the bot's conversational onboarding
in onboarding.py) that adopts the already-existing production team - Texas
A/B/C, Database TX, Odometer Jump, already-live Factor/Leader tokens - into
config_store, so the control bot's /rotatefactor, /rotateleader,
/rotateasana, /status, /pause, /resume commands work for it too. No Asana
boards get created; everything here already exists, this only registers
config that already lives in the repo root's .env.

Run from the repo root: python -m control_bot.register_existing_team
"""

import logging
import os

from dotenv import load_dotenv

import eld_factor
from control_bot.config_store import ConfigStore
from control_bot.provisioning import Provisioner

TEAM_ID = "original"
TEAM_NAME = "Texas"
WORKSPACE_GID = "1209191933192029"  # confirmed live via GET /projects/{id} - "ALGO Workspace"

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("register_existing_team")

_HERE = os.path.dirname(__file__)
_REPO_ROOT = os.path.dirname(_HERE)


def main():
    load_dotenv(os.path.join(_REPO_ROOT, ".env"))
    load_dotenv(os.path.join(_HERE, ".env"))  # for shared_bot_token below

    config_store = ConfigStore(
        os.path.join(_HERE, "config.db"), os.path.join(_HERE, "master.key"),
    )
    if config_store.get_team(TEAM_ID) is not None:
        logger.error("Team '%s' is already registered - not overwriting. "
                      "Use the bot's /rotate* commands to update credentials instead.", TEAM_ID)
        return

    known_chat_ids = [
        int(x.strip()) for x in os.environ.get("KNOWN_TELEGRAM_CHAT_IDS", "").split(",") if x.strip()
    ]

    config_store.create_team(
        TEAM_ID, TEAM_NAME,
        status="active",
        workspace_gid=WORKSPACE_GID,
        asana_token=os.environ["ASANA_TOKEN"],
        asana_project_ids=os.environ["ASANA_PROJECT_IDS"],
        asana_database_project_id=os.environ.get("ASANA_DATABASE_PROJECT_ID", ""),
        asana_odometer_project_id=os.environ.get("ASANA_ODOMETER_PROJECT_IDS", ""),
        factor_session_token=os.environ.get("FACTOR_SESSION_TOKEN") or None,
        factor_tenant_id=os.environ.get("FACTOR_TENANT_ID", ""),
        leader_session_token=os.environ.get("LEADER_SESSION_TOKEN") or None,
        leader_tenant_id=os.environ.get("LEADER_TENANT_ID", ""),
        staff_roster=eld_factor.STAFF_ID_BY_FIRST_NAME,
    )
    for chat_id in known_chat_ids:
        config_store.add_team_admin(chat_id, TEAM_ID, telegram_user_id=None)

    teams_root_dir = os.path.join(_REPO_ROOT, "teams")
    shared_bot_token = os.environ["TELEGRAM_BOT_TOKEN"]
    provisioning = Provisioner(config_store, teams_root_dir, shared_bot_token, logger)
    provisioning.rewrite_env(TEAM_ID)

    logger.info(
        "Registered team '%s' with %s admin chat(s). Generated teams/%s/.env - "
        "run sync.py from that directory going forward instead of the repo root.",
        TEAM_ID, len(known_chat_ids), TEAM_ID,
    )


if __name__ == "__main__":
    main()
