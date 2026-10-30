# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""CLI entry point. Configuration checks never contact Discord."""

import argparse
import asyncio
import logging
import os

import discord

from afterhours_bot.bot import CommunityBot
from afterhours_bot.config import ConfigurationError, Settings

logger = logging.getLogger(__name__)


async def run_bot(settings: Settings, *, sync_commands: bool) -> None:
    async with CommunityBot(settings, sync_commands=sync_commands) as bot:
        await bot.start(settings.token)


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
        logger.info("Configuration is valid; no connection was made.")
        return 0
    try:
        asyncio.run(run_bot(settings, sync_commands=args.sync_commands))
    except KeyboardInterrupt:
        return 0
    except discord.LoginFailure:
        logger.error("Discord authentication failed. Check DISCORD_TOKEN.")
        return 1
    except (discord.DiscordException, OSError) as error:
        logger.error("Discord connection failed (%s).", type(error).__name__)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
