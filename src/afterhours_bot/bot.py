# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Guild-scoped slash commands and the Discord connection edge."""

import logging

import discord
from discord import app_commands

from afterhours_bot.config import Settings

logger = logging.getLogger(__name__)


class CommunityTree(app_commands.CommandTree["CommunityBot"]):
    async def interaction_check(self, interaction: discord.Interaction[CommunityBot]) -> bool:
        if interaction.guild_id != self.client.settings.guild_id:
            await interaction.response.send_message(
                "This bot is available in the Afterhours community server.", ephemeral=True
            )
            return False
        return True

    async def on_error(
        self,
        interaction: discord.Interaction[CommunityBot],
        error: app_commands.AppCommandError,
    ) -> None:
        # Exception text can contain user input or HTTP details; log only the class.
        logger.error("Command failed (%s).", type(error).__name__)
        message = "The command could not be completed. Please try again later."
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)


class CommunityBot(discord.Client):
    def __init__(self, settings: Settings, *, sync_commands: bool = False) -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        super().__init__(intents=intents, allowed_mentions=discord.AllowedMentions.none())
        self.settings = settings
        self.sync_commands = sync_commands
        self.tree = CommunityTree(self)
        guild = discord.Object(id=settings.guild_id)

        @self.tree.command(
            name="ping", description="Check whether the bot is responding.", guild=guild
        )
        @app_commands.guild_only()
        async def ping(interaction: discord.Interaction[CommunityBot]) -> None:
            await interaction.response.send_message("Pong!", ephemeral=True)

        @self.tree.command(
            name="about", description="About the Afterhours community bot.", guild=guild
        )
        @app_commands.guild_only()
        async def about(interaction: discord.Interaction[CommunityBot]) -> None:
            await interaction.response.send_message(
                "The Afterhours community bot.\n"
                "Source: https://github.com/afterhours-crew/afterhours-discord-bot",
                ephemeral=True,
            )

    async def setup_hook(self) -> None:
        if self.sync_commands:
            commands = await self.tree.sync(guild=discord.Object(id=self.settings.guild_id))
            logger.info("Registered %d community commands.", len(commands))

    async def on_ready(self) -> None:
        logger.info("Community bot connected.")
