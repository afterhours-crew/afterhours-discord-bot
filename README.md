# Nightshift

Nightshift is the Python Discord bot for the Afterhours community, managed entirely with
[uv](https://docs.astral.sh/uv/). This is a starter with two server-only slash
commands: `/ping` checks the bot and `/about` links to its source. Replies are
private to the person using the command.
An optional activity feature maintains a role based on recent member messages.

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
   Leave Message Content and Presence privileged intents disabled. Server Members
   Intent is needed only when enabling [Active roles](#active-roles).
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

## Active roles

Nightshift can grant and remove a role according to a rolling message window.
It checks immediately after connecting, then once an hour. Existing messages
count, including after a restart. A local SQLite database stores settings, scan
cursors and the small amount of activity metadata needed for the rolling count.

1. Create or choose your **Active** role. With Developer Mode enabled, copy its
   role ID from Discord's role settings.
2. Place that role below Nightshift's highest role. Nightshift needs **Manage
   Roles**, **View Channels** and **Read Message History** for the channels it
   should count.
3. In the Developer Portal's **Bot** settings, enable **Server Members Intent**.
   Keep **Message Content Intent** and **Presence Intent** disabled.
4. Add these values to `.env`, replacing the example role ID:

   ```dotenv
   ACTIVE_ROLE_ID=123456789012345678
   ACTIVE_WINDOW_DAYS=14
   ACTIVE_MIN_MESSAGES=3
   ```

   The minimum is inclusive: `3` means at least three messages, or more than two.
   Days can be 1–365 and the minimum can be 1–10000. Leaving `ACTIVE_ROLE_ID`
   empty disables this feature and the members intent.

5. Rebuild and recreate the service to apply the configuration:

   ```sh
   docker compose up -d --build --force-recreate nightshift
   docker compose logs -f nightshift
   ```

Counts cover all visible text-capable server channels and accessible active and
archived threads, including forum posts. Private threads count when Nightshift
can access them. Ordinary human messages and replies count; bots, webhooks,
system messages do not. Once counted, a message keeps counting if it is later
deleted. Messages deleted before Nightshift observes them cannot be recovered.
Message content is never inspected, logged or stored.

The first run backfills the configured window. Later checks fetch only messages
after each channel/thread's saved cursor, including after a restart. Counts
expire from the local timestamps without rereading old messages. Newly accessible
histories get their own initial backfill. Increasing the window or message
minimum triggers a backfill because the smaller settings may have discarded
needed metadata; unchanged or decreased settings preserve the cursors.

SQLite retains only the most recent messages up to the configured minimum per
member and source, within the configured window. Non-secret activity settings
are saved in a versioned configuration table; `.env` remains authoritative for
these settings. Tokens and full member profiles are never saved in the database.
Disabling the feature clears its cached activity and cursors while retaining
unrelated configuration.

A complete scan grants the role to qualifying members and removes it from
members below the threshold. Other roles and bots are left alone. If member or
history enumeration fails, a visible channel lacks history permission, or a limit
is reached before enumeration completes, no roles change for that pass. The next hourly check retries.
Individual role-update failures are reported as aggregate counts and retried
on later checks. A timeout during role updates leaves successful changes in place;
the next pass reads current membership and reconciles what remains.

Each pass allows at most 15 minutes, 100000 members, 5000 history sources and
250000 newly fetched messages. At most 250000 activity records are retained per
server. Completed histories save metadata and their cursor together; an incomplete
history keeps its prior cursor for retry. No role changes happen until the whole
pass has complete member/history coverage. At most 1000 role changes are attempted
per pass; remaining changes are deferred. Logs report scan counts, failures and deferred work
without member identities. A new live installation should be checked separately
from the offline tests.

## Persistent data

Docker Compose creates the named `nightshift-data` volume at `/data` for
`/data/nightshift.sqlite3`. Keep `NIGHTSHIFT_DATABASE_PATH` unset in `.env` for
this setup. The non-root container can write that volume while its application
filesystem remains read-only. SQLite is included with Python; no extra database
service is needed.

`docker compose down` and image rebuilds preserve the volume. Removing the volume
(for example, with `docker compose down -v`) deletes the saved settings and cursors
and requires a new backfill. Stop Nightshift before copying the volume for backup
so that the database and its journal files remain consistent.

Local runs use `data/nightshift.sqlite3` by default. Set `NIGHTSHIFT_DATABASE_PATH`
to another persistent file path if needed. Database files are excluded from Git
and the Docker build context. A metadata-only first backfill is required when
upgrading from the earlier stateless scanner, since it saved no cursor state.

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
