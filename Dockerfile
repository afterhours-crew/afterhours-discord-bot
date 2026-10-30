# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

FROM ghcr.io/astral-sh/uv:0.12.9@sha256:8b940d3a9d65bed080436972241af2e21c84b5e8c9193f7014ed71479ee795ff AS uv
FROM python:3.14.7-slim-bookworm@sha256:82bc3c539b8813ada9d68c63b40158fa002f7f33de9bf3312a3dfdc0620dff56 AS builder

COPY --from=uv /uv /usr/local/bin/uv
ENV UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy
WORKDIR /app

COPY .python-version pyproject.toml uv.lock README.md LICENSE THIRD-PARTY-NOTICES.md ./
COPY src/ ./src/
RUN uv sync --locked --no-dev --no-editable

FROM python:3.14.7-slim-bookworm@sha256:82bc3c539b8813ada9d68c63b40158fa002f7f33de9bf3312a3dfdc0620dff56 AS runtime

LABEL org.opencontainers.image.title="Nightshift" \
      org.opencontainers.image.source="https://github.com/afterhours-crew/afterhours-discord-bot" \
      org.opencontainers.image.licenses="MPL-2.0"

RUN groupadd --gid 10001 nightshift \
    && useradd --uid 10001 --gid nightshift --no-create-home \
       --home-dir /app --shell /usr/sbin/nologin nightshift

WORKDIR /app
COPY --from=uv /uv /usr/local/bin/uv
COPY --from=builder /app/.venv /app/.venv
COPY .python-version pyproject.toml uv.lock README.md LICENSE THIRD-PARTY-NOTICES.md ./

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_NO_CACHE=1

USER 10001:10001
STOPSIGNAL SIGINT
ENTRYPOINT ["uv", "run", "--locked", "--no-sync", "--offline", "--no-dev", "afterhours-bot"]
