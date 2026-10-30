# Nightshift

Nightshift is the Python Discord bot for the Afterhours community, managed entirely with
[uv](https://docs.astral.sh/uv/). This is a starter with two server-only slash
commands: `/ping` checks the bot and `/about` links to its source. Replies are
private to the person using the command.

The bot runs separately from the game, server and launcher. It requires Discord
connectivity. This repository does not implement launcher Rich Presence.

## Docker Compose

Install Docker with the Compose plugin and start the Docker engine. No host
Python or uv installation is needed for this setup.

1. Create the bot application, install it in your server and obtain its token
   and server ID as described in steps 1–3 under [Local setup](#local-setup).
   Copy `.env.example` to `.env` and fill in those two values:

   ```sh
   cp .env.example .env
   chmod 600 .env
   ```

2. Build the image and validate configuration without connecting to Discord:

   ```sh
   docker compose build --pull
   docker compose run --rm nightshift --check-config
   ```

3. On first setup, register the commands:

   ```sh
   docker compose run --rm nightshift --sync-commands
   ```

   Wait for the registration log, then press Ctrl+C to stop that instance.
   Repeat this step only when command definitions change.

4. Start the service in the background:

   ```sh
   docker compose up -d
   docker compose logs -f nightshift
   ```

The service uses `restart: unless-stopped`: Docker restarts it after an exit or
when the Docker engine starts again, and respects an intentional stop. Configure
the Docker engine to start automatically if you want this behavior after a host
restart. Logs rotate automatically. The token is passed at runtime from `.env`
and is excluded from the image and build context.

To update the source and rebuild:

```sh
git pull --ff-only
docker compose build --pull
docker compose up -d
```

To stop the service:

```sh
docker compose down
```

## Local setup

Install uv **0.12.9**. The repository pins Python **3.14.7**; uv downloads it if
needed. From this repository:

```sh
uv sync --locked
```

1. Create an application and bot in the
   [Discord Developer Portal](https://discord.com/developers/applications).
   Keep Message Content, Server Members and Presence privileged intents disabled.
2. Install it in your community server with the `bot` and `applications.commands`
   scopes. The starter needs no additional bot permissions; do not grant
   Administrator. Only server installation is used.
3. Copy `.env.example` to `.env`. Set `DISCORD_TOKEN` to the bot token and
   `DISCORD_GUILD_ID` to the community server ID. Enable Developer Mode in Discord
   to copy the server ID. Keep `.env` local; host deployments can set environment
   variables instead. Never put the token in a command argument, issue or log.
4. Check configuration offline:

   ```sh
   uv run --locked --env-file .env afterhours-bot --check-config
   ```

5. On first setup, explicitly register the commands in that server and start:

   ```sh
   uv run --locked --env-file .env afterhours-bot --sync-commands
   ```

6. For subsequent starts:

   ```sh
   uv run --locked --env-file .env afterhours-bot
   ```

Stop with Ctrl+C. Run one instance per bot token. Re-run with `--sync-commands`
after command definitions change. Sync replaces this application's command set
in the configured server; use a dedicated application for this bot. Nothing is
registered globally or on reconnect. Changing server ID does not remove commands
previously registered in another server; their use is rejected by the bot.

For a runtime-only installation, use `uv sync --locked --no-dev` and
`uv run --locked --no-dev --env-file .env afterhours-bot`.

## Development

```sh
uv run --locked ruff format --check .
uv run --locked ruff check .
uv run --locked mypy
uv run --locked pytest
uv build --no-sources
```

CI runs these checks on Linux and Windows without tokens or Discord access.
Tests cover configuration, server isolation, registration, error privacy and
startup behavior. A real-server installation and slash-command run require an
operator's Discord application and are a separate manual check.

See [CONTRIBUTING.md](CONTRIBUTING.md) for repository rules and
[THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md) for dependency licenses.
Licensed under [MPL-2.0](LICENSE).
