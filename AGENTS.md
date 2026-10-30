# Project instructions

Read README.md and CONTRIBUTING.md, then inspect git status. Preserve existing
work. This repository owns Nightshift, the Python bot for the Afterhours community.
Keep it independent of game/server/launcher code and private research data.

Use uv only for Python tooling and dependency management. Keep exact direct
dependency pins, uv.lock and .python-version committed. Use uv sync --locked
and uv run --locked; never add another environment or package manager. Record
dependency licenses in THIRD-PARTY-NOTICES.md. No telemetry or secrets in Git.

Work on a feature branch based on the latest origin/main. Commit as
`0x20F <0x20fa@gmail.com>`, without co-author trailers or tool attribution in
commits, pull requests or repository prose. Push checkpoint commits to the
feature branch. Open/update its pull request once the requested issue is
implemented, tested and documented. The owner merges. Never push to main/master,
force-push an open pull request, rewrite tags or merge without owner instruction.
Stage explicit paths and inspect the staged diff.

Use GitHub issues for tasks and acceptance criteria. Keep changes within their
scope. Default to server-only slash commands, minimal intents and permissions,
private replies and no stored member/message data. New community features and
permission increases need an explicit request. Never log tokens or raw payloads.
Keep automated checks offline; do not connect a bot or publish Discord messages
without explicit authorization and operator-provided configuration.

Before pushing, run the five development checks in README.md. Record what
actually ran; mock tests do not establish a live Discord installation. Apply
the MPL-2.0 notice to source/test files and maintain dependency license records.
