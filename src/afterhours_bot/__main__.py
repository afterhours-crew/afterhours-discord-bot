# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""CLI entry point. Configuration checks never contact Discord."""

import argparse
import asyncio
import logging
import os
import sqlite3

import discord

from afterhours_bot.bot import CommunityBot
from afterhours_bot.config import ConfigurationError, Settings
from afterhours_bot.rules import ModerationRulesError, load_rules
from afterhours_bot.storage import ActivityStateError

logger = logging.getLogger(__name__)


async def run_bot(settings: Settings, *, sync_commands: bool) -> None:
    async with CommunityBot(settings, sync_commands=sync_commands) as bot:
        await bot.start(settings.token)


def _intent_advice(settings: Settings) -> str:
    needed = []
    if settings.active_role is not None:
        needed.append("Server Members Intent for Active roles")
    if settings.moderation is not None:
        needed.append("Message Content Intent for moderation")
    if not needed:
        return "Enable Server Members Intent in the Discord Developer Portal for Active roles."
    return "Enable " + " and ".join(needed) + " in the Discord Developer Portal."


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Nightshift, the Afterhours community Discord bot")
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument(
        "--check-config", action="store_true", help="Validate configuration without connecting."
    )
    actions.add_argument(
        "--sync-commands",
        action="store_true",
        help="Register this bot's commands in the configured server, then run. Use on first setup.",
    )
    args = parser.parse_args(argv)
    try:
        settings = Settings.from_env(os.environ)
    except ConfigurationError as error:
        parser.error(str(error))

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("discord").setLevel(logging.WARNING)
    if args.check_config:
        if settings.moderation is not None:
            try:
                rules = load_rules(settings.moderation.rules_path)
            except ModerationRulesError as error:
                parser.error(f"MODERATION_RULES_PATH: {error}")
            logger.info(
                "Moderation rules readable: %d bytes (%s).",
                len(rules.text.encode("utf-8")),
                rules.short_digest,
            )
        logger.info("Configuration is valid; no connection was made.")
        return 0
    try:
        asyncio.run(run_bot(settings, sync_commands=args.sync_commands))
    except KeyboardInterrupt:
        return 0
    except ModerationRulesError as error:
        logger.error("Moderation rules could not be loaded: %s", error)
        return 1
    except (sqlite3.Error, ActivityStateError) as error:
        logger.error(
            "Database initialization failed (%s). Check the database path, volume permissions "
            "and schema compatibility.",
            type(error).__name__,
        )
        return 1
    except discord.PrivilegedIntentsRequired:
        logger.error(_intent_advice(settings))
        return 1
    except discord.LoginFailure:
        logger.error("Discord authentication failed. Check DISCORD_TOKEN.")
        return 1
    except (discord.DiscordException, OSError) as error:
        logger.error("Discord connection failed (%s).", type(error).__name__)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
