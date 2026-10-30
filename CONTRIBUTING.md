# Contributing

Read the README and inspect `git status` first. Preserve existing work.
This repository owns the community Discord bot. Keep game, launcher and server
behavior in their own Afterhours repositories. Never introduce a dependency on
private research, a sibling checkout, captures, game content or player profiles.

## Workflow

Start a feature branch from the latest `origin/main`. Use `codex/`, `feature/`,
`bugfix/` or `docs/` as appropriate. Push completed checkpoint commits to that
branch. Open a pull request when the requested issue is implemented, tested and
documented; link its public issue. The owner merges. Never push directly to
`main` or `master`, force-push an open pull request, rewrite a tag, or merge
without the owner's instruction. All review conversations must be resolved.

Commit as `0x20F <0x20fa@gmail.com>`, without co-author trailers. Use factual
commit messages and pull request descriptions focused on the change.
Stage explicit paths and review the staged diff before pushing. Track tasks in
GitHub issues and keep their acceptance criteria and validation current.

## Python and dependencies

Use uv for Python installation, dependencies, environments, running, checking
and building. Do not use pip, Poetry, Pipenv, virtualenv or a second lockfile.
Use `uv sync --locked` for setup and `uv run --locked` for commands. Commit
`uv.lock` and `.python-version`. Direct runtime, development and build
dependencies are exact pins. Add or update deliberately with uv, review the
lockfile diff and license, and update THIRD-PARTY-NOTICES.md. Upgrade Python and
uv pins together with their CI/documentation references when needed.

Keep configuration, Discord transport and future community features separate.
Use async I/O at the Discord edge; never block the event loop. Bound external
work and use timeouts and cancellation. Do not add privileged intents, broad
permissions, automatic announcements, moderation actions, account linking or
stored member/message data without a scoped issue and an explicit owner request.
No telemetry. Tokens belong only in local environment variables or host secrets.
Never log tokens, message content, member data or raw HTTP/exception bodies.

## Checks

Run every README development command before pushing code. Add meaningful
regression tests for behavior changes. Tests use constructed data and mocked
Discord calls, require no token and make no external requests. Document manual
Discord checks separately; an offline test suite does not prove an installed
bot can authenticate, register commands or respond in a server.

All source and test files carry the MPL-2.0 notice. Keep dependency licenses
documented. No bundled secrets, environments, generated distributions or logs.
