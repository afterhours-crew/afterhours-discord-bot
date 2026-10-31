# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The community rule text and its content digest, used for revision tracking."""

import hashlib
from dataclasses import dataclass
from pathlib import Path

MAX_RULES_BYTES = 65_536


class ModerationRulesError(RuntimeError):
    """The rule file is missing, unreadable, empty or too large."""


@dataclass(frozen=True)
class RuleSet:
    text: str
    sha256: str

    @property
    def short_digest(self) -> str:
        return self.sha256[:12]


def load_rules(path: Path, *, max_bytes: int = MAX_RULES_BYTES) -> RuleSet:
    """Read and normalize the rule file. Blocking I/O; call it off the event loop."""
    try:
        if path.stat().st_size > max_bytes:
            raise ModerationRulesError(f"The rule file exceeds {max_bytes} bytes.")
        raw = path.read_bytes()
    except FileNotFoundError:
        raise ModerationRulesError("The rule file does not exist.") from None
    except OSError:
        raise ModerationRulesError("The rule file could not be read.") from None
    if len(raw) > max_bytes:
        raise ModerationRulesError(f"The rule file exceeds {max_bytes} bytes.")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ModerationRulesError("The rule file must be UTF-8 text.") from None
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        raise ModerationRulesError("The rule file is empty.")
    text += "\n"
    return RuleSet(text=text, sha256=hashlib.sha256(text.encode("utf-8")).hexdigest())
