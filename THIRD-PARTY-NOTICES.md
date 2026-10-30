# Third-party licenses

The bot is MPL-2.0. It depends on the following packages locked in `uv.lock`.
Licenses were reviewed from the installed package metadata and bundled license
files on 2026-10-30. Upstream license files remain authoritative; uv installs
them with the packages. No game content or private research is bundled.

## Runtime

| Package | Version | License |
| --- | --- | --- |
| discord.py | 2.7.1 | MIT |
| aiohappyeyeballs | 2.7.1 | PSF-2.0 |
| aiohttp | 3.14.5 | Apache-2.0 AND MIT |
| aiosignal | 1.4.0 | Apache-2.0 |
| attrs | 26.1.0 | MIT |
| audioop-lts | 0.2.2 | PSF-2.0 |
| frozenlist | 1.8.0 | Apache-2.0 |
| idna | 3.20 | BSD-3-Clause |
| multidict | 7.2.1 | Apache-2.0 |
| propcache | 0.5.4 | Apache-2.0 |
| yarl | 1.25.1 | Apache-2.0 |

## Development and build

| Package | Version | License |
| --- | --- | --- |
| uv / uv_build | 0.12.9 | MIT OR Apache-2.0 |
| mypy | 2.4.1 | MIT |
| pytest | 9.2.0 | MIT |
| ruff | 0.17.3 | MIT |
| ast-serialize | 0.13.1 | MIT |
| colorama (Windows) | 0.4.6 | BSD-3-Clause |
| iniconfig | 2.3.1 | MIT |
| librt | 0.16.0 | MIT |
| mypy-extensions | 1.1.0 | MIT |
| packaging | 26.3 | Apache-2.0 OR BSD-2-Clause |
| pathspec | 1.1.1 | MPL-2.0 |
| pluggy | 1.7.0 | MIT |
| Pygments | 2.21.0 | BSD-2-Clause |
| typing-extensions | 4.16.0 | PSF-2.0 |

Python 3.14.7 is provided by uv's managed Python distribution and carries the
Python Software Foundation license and its bundled third-party notices.

The optional container uses the official `python:3.14.7-slim-bookworm` and
`ghcr.io/astral-sh/uv:0.12.9` images, pinned by multi-platform digest in Dockerfile.
The Python image includes Debian Bookworm; its packages retain their notices
under `/usr/share/doc`. Python uses the PSF license; uv uses MIT OR Apache-2.0.

References: [discord.py](https://github.com/Rapptz/discord.py),
[uv](https://github.com/astral-sh/uv), [Ruff](https://github.com/astral-sh/ruff),
[mypy](https://github.com/python/mypy), [pytest](https://github.com/pytest-dev/pytest).
