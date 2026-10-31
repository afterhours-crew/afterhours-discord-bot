# Nightshift

Nightshift is the Python Discord bot for the Afterhours community, managed entirely with
[uv](https://docs.astral.sh/uv/). This is a starter with two server-only slash
commands: `/ping` checks the bot and `/about` links to its source. Replies are
private to the person using the command.
An optional activity feature maintains a role based on recent member messages.
An optional moderation feature, off by default, prepares member messages for
staff review against the community rules; see [Moderation](#moderation).

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
   Leave the Presence privileged intent disabled. Server Members Intent is needed
   only when enabling [Active roles](#active-roles); Message Content Intent only
   when enabling [Moderation](#moderation).
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
   Keep **Presence Intent** disabled; **Message Content Intent** is needed only
   for [Moderation](#moderation).
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

## Moderation

Nightshift can help staff enforce the community rules. It receives member
messages, runs deterministic filters and posts anything suspicious to a staff
review channel with Dismiss, Delete, Warn and Timeout buttons. This is **shadow
mode**: the bot only reports and records; it never deletes, warns, times out or
announces on its own. Every action is a staff click on a review post; see
[Staff actions](#staff-actions).
An optional local language model reviews what the filters let through; see
[Language-model review](#language-model-review).

Use Discord's built-in **AutoMod** (Server Settings → AutoMod) as the first
layer: keyword and regex lists, mention spam, suspected spam and known malicious
links, plus the explicit media filter. Those run inside Discord with no bot
permissions and no intent, so Nightshift does not duplicate them. The bot adds
what AutoMod cannot express: repeated messages, your own domain list mapped to
rule numbers, attachment types and, later, judgement calls against the rule text.

1. Create or choose a private **staff review channel** where Nightshift can
   **View Channel** and **Send Messages**. With Developer Mode enabled, copy its
   channel ID. Messages in that channel and its threads are never reviewed.
2. In the Developer Portal's **Bot** settings, enable **Message Content
   Intent**. Without it Discord delivers messages with empty content and the bot
   refuses to start with an error naming this setting.
3. Add the channel ID to `.env`; the rule file and queue size have defaults:

   ```dotenv
   MODERATION_STAFF_CHANNEL_ID=123456789012345678
   # MODERATION_RULES_PATH=rules/community-rules.md
   # MODERATION_QUEUE_SIZE=1000
   ```

4. Check the configuration offline, which also reads the rule file, then rebuild
   and recreate the service as for Active roles. Run `--sync-commands` once so
   the new `/moderation` command is registered.

The community rules live in [`rules/community-rules.md`](rules/community-rules.md),
which ships in the repository and the image. At startup the text is normalized
and saved in the SQLite configuration table with a revision that increases only
when the content changes. Review records cite that revision. Point
`MODERATION_RULES_PATH` at another UTF-8 file (up to 64 KiB) to use a different
text, for example from a mounted volume. Rule headings must look like
`# 2. No piracy`; the number is what filters, verdicts and warnings refer to.

Staff can change the active text without a rebuild through the `/rules`
commands (visible with **Manage Messages**, private replies): `/rules show`
reports the revision, digest, source and rule numbers; `/rules reload` re-reads
the file from disk; `/rules upload` replaces the text with an attached `.md` or
`.txt` file (UTF-8, at most 64 KiB, with numbered headings). An uploaded text
stays active across restarts until the file on disk changes, at which point the
file wins again; `/rules reload` also returns to the file. Every change is a new
revision in the database.

`/moderation` is a staff command, visible by default to members with **Manage
Messages**, and replies privately with the feature state, staff channel, rule
revision, intent status, intake and flag counters and the audit size. It never
shows message text, member identities or secrets.

Only ordinary messages and replies from members in the configured server are
queued; bots, webhooks, system messages, direct messages and the staff channel
are skipped. Edited messages are reviewed again; an edit to a message that was
already flagged adds to the same review post instead of opening a new one. The queue is bounded by `MODERATION_QUEUE_SIZE` (1–100000): when it
is full new messages are dropped, counted and reported in the log at most once
a minute, without content. Message text is held in memory only while queued and
is never written to the database or the log. Enabling moderation adds the
`guild_messages` and `message_content` intents and nothing else. Leaving
`MODERATION_STAFF_CHANNEL_ID` empty keeps the bot exactly as before.

### Filters

Each filter maps to a rule number from the rule file:

- **Repeated messages** (rule 6): the same text, ignoring case and spacing,
  posted `MODERATION_REPEAT_THRESHOLD` times (default 3) by one member within
  `MODERATION_REPEAT_WINDOW_SECONDS` (default 60). Memory is bounded to the most
  recent authors and messages.
- **Server invite links** (rule 6): `discord.gg` and `discord.com/invite` links.
- **Listed link domains**: `MODERATION_LINK_DENYLIST` holds `domain:rule` pairs
  (for example `example-piracy.site:2`); subdomains match. Empty by default.
- **Attachment types**: `MODERATION_ATTACHMENT_DENYLIST` holds `extension:rule`
  pairs. The default lists executables and installers under rule 2 and script
  types under rule 9.

### Review posts and audit

A flag posts to the staff channel with the rule number, the filter's reason, a
confidence value, the filter name, a jump link to the message, the author and
channel mentions (never pinged) and the rule revision. The post does not quote
the message; staff read it through the link. Posts are limited to
`MODERATION_FLAGS_PER_MINUTE` (default 20); beyond that, flags are still audited
but not posted, and the log says so once a minute. If the staff channel is
missing, in another server, not a text channel or not writable, posting pauses
for ten minutes with one warning and the bot keeps running.

Buttons keep working after a restart. The first decision wins; later clicks see
who resolved it and what happened. The audit table stores message, channel and
author IDs, rule, score, filter name, reason, rule revision, post ID, decision,
decider, outcome and timestamps, never message text. Rows expire after `MODERATION_AUDIT_RETENTION_DAYS` (default 90)
and the table is capped at `MODERATION_AUDIT_MAX_ROWS` (default 100000), oldest
first. Existing databases are migrated to the new schema at startup.

### Staff actions

The buttons on a review post act when a staff member clicks them; nothing
happens without that click.

- **Dismiss** resolves the post. Needs **Manage Messages**.
- **Delete** removes the flagged message. Needs **Manage Messages** for the
  clicker and for Nightshift in that channel.
- **Warn** sends the author a private, templated notice naming the rule and
  channel. Needs **Manage Messages**. Set `MODERATION_WARN_TEMPLATE` to change the
  text; it may use `{server}`, `{channel}`, `{rule}` and `{title}`.
- **Timeout** mutes the author for `MODERATION_TIMEOUT_MINUTES` (default 60).
  Needs **Moderate Members** for the clicker and for Nightshift, and the author's
  highest role must be below Nightshift's.

**Automatic actions** are off by default. Once shadow-mode data shows that a
deterministic filter is reliable, `MODERATION_AUTO_ACTIONS` can name
`source:rule:action:score` entries, for example `invite:6:delete:0.5` to delete
invite links or `link-domain:2:timeout:0.9` to time out members who post a
listed piracy domain. Only the deterministic sources `repeat`, `invite`,
`link-domain` and `attachment` qualify; model verdicts never act on their own.
Staff authors are never acted on automatically. An automatic action still
posts to the staff channel, without buttons, stating what was done, and is
audited with the bot as the decider.

Grant Nightshift exactly **View Channel**, **Send Messages** in the staff
channel, **Manage Messages** and **Moderate Members**; never Administrator. The
outcome is recorded in the audit and shown on the post, for example "message
deleted", "the member does not accept direct messages" or "the member's roles are
not below the bot's". Failures are reported privately in plain words, never with
raw API details. Buttons other than Dismiss stop acting after
`MODERATION_BUTTON_EXPIRY_HOURS` (default 72); the post then says to act manually.

### Language-model review

Messages that pass the deterministic filters can be judged by a language model
you run yourself. Nightshift talks to any **OpenAI-compatible chat completions
API**, which Ollama, llama.cpp's server and LM Studio all provide, so the model is
a configuration choice. The model's answer is a flag at most: it goes to the
staff channel like any other flag, in shadow mode, and never reaches members.

1. Run a model server reachable from the bot. With Ollama on the same Docker
   host, uncomment the `ollama` service in `compose.yaml`, start it and pull an
   instruct model that follows JSON instructions, for example
   `docker compose exec ollama ollama pull qwen2.5:7b-instruct`. You accept the
   model's own license when you pull it; record it in THIRD-PARTY-NOTICES.md if
   you ship it with your deployment. A 7–8B model needs roughly 6 GB of GPU or
   system memory and answers in a few seconds on a GPU, tens of seconds on CPU.
2. Set the endpoint and model in `.env`:

   ```dotenv
   MODERATION_LLM_URL=http://ollama:11434/v1
   MODERATION_LLM_MODEL=qwen2.5:7b-instruct
   ```

   `MODERATION_LLM_API_KEY` is sent as a bearer token when your server needs one
   and is never logged. The URL must be plain `http(s)://host[:port]/path` with no
   credentials or query string; `/chat/completions` is appended.
3. Check the configuration offline, rebuild and recreate the service. The bot
   starts without the model server and reports it unavailable until it answers.

The prompt contains the rule text and the optional
[`rules/classifier-guidance.md`](rules/classifier-guidance.md), which tunes
judgement, for example the rule 2 boundary between community-server play and
activation or DRM bypass. The model must answer with one JSON object: a rule
number or null, a score from 0 to 1 and a short reason. Anything else is "no
verdict" and is counted, not acted on. Verdicts at or above
`MODERATION_LLM_MIN_SCORE` (default 0.5) become flags whose reason starts with
`model:`; the reason is sanitized and capped before it reaches the staff post.
The model's text is never logged, because it can echo the message.

Message text sent to the model is capped at `MODERATION_LLM_MAX_CHARS` (default
2000), is processed in memory only, and is framed as untrusted data: instructions
inside a message are treated as part of the message. For judgement calls such as
harassment the model also sees the last `MODERATION_LLM_CONTEXT_MESSAGES`
(default 3, 0 disables) messages of the same channel, each cut to 200
characters and attributed to an anonymous member label; this tail lives in
memory only and is never stored.

Images are reviewed when `MODERATION_LLM_VISION_MODEL` names a model that
accepts images through the same API (for example `llava:7b` or a Qwen vision
model in Ollama). Up to four image attachments of at most 10 MB are passed to
the model by their Discord URL, so the model host fetches them directly; nothing
is stored. Image verdicts appear as `llm-image` flags. Without a vision model,
images are ignored. Members with Manage
Messages are skipped unless `MODERATION_LLM_SKIP_STAFF=false`. Requests run one
at a time (`MODERATION_LLM_CONCURRENCY`, at most 2) with a
`MODERATION_LLM_TIMEOUT_SECONDS` timeout. After
`MODERATION_LLM_FAILURE_THRESHOLD` consecutive failures (default 3) the bot stops
calling the model for `MODERATION_LLM_COOLDOWN_SECONDS` (default 300), keeps the
deterministic filters running and shows "unavailable" in `/moderation`.

To measure a model before trusting it, run the evaluation against the synthetic
labelled examples in [`rules/classifier-examples.json`](rules/classifier-examples.json),
which cover every rule, allowed look-alikes and prompt-injection attempts:

```sh
uv run --locked --env-file .env afterhours-bot-evaluate
```

It prints per-rule precision and recall and the mismatched example IDs, never
the texts, and exits non-zero when any example got no verdict. Add your own
examples to a copy of the file and pass `--examples`. This is a manual check
against a live endpoint; the offline test suite uses a fake endpoint and does
not prove any particular model's accuracy.

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
Tests cover configuration, server isolation, registration, error privacy,
startup behavior, activity scans, rule revisions and the moderation intake. A
real-server installation, slash-command run and message delivery with the
Message Content intent require an operator's Discord application and are a
separate manual check.

See [CONTRIBUTING.md](CONTRIBUTING.md) for repository rules and
[THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md) for dependency licenses.
Licensed under [MPL-2.0](LICENSE).
