from __future__ import annotations

import logging
from typing import Iterable, Mapping


def target_alias(index: int) -> str:
    """Return the public alias for a target at zero-based ``index``.

    Aliases are ``好友01``, ``好友02``, ..., ``好友10`` and so on, following
    the order of targets in the task configuration.
    """
    return f"好友{index + 1:02d}"


def build_target_aliases(targets: Iterable[object]) -> dict[str, str]:
    """Map every configured identity candidate to its public target alias.

    Search and browser errors can echo a remark, nickname, or stable account
    identifier rather than the display name. Keep all candidates tied to the
    same alias so logs, result files, and notifications do not expose them.
    """
    aliases: dict[str, str] = {}
    for index, target in enumerate(targets):
        if isinstance(target, str):
            candidates = (target,)
        else:
            candidates = tuple(
                getattr(target, field, None)
                for field in (
                    "name",
                    "display_name",
                    "remark_name",
                    "nickname",
                    "unique_id",
                    "short_id",
                    "sec_uid",
                )
            )
        alias = target_alias(index)
        for candidate in candidates:
            if isinstance(candidate, str) and candidate.strip():
                # If two targets share an identity value, preserve the first
                # mapping rather than silently changing an already redacted
                # value to another target's alias.
                aliases.setdefault(candidate.strip(), alias)
    return aliases


def redact_text(text: str, aliases: Mapping[str, str]) -> str:
    """Replace every real name in ``text`` with its alias.

    Longer names are replaced first so that a name like ``小明同学`` is fully
    redacted before its prefix ``小明`` is considered.
    """
    for name in sorted((name for name in aliases if name), key=len, reverse=True):
        text = text.replace(name, aliases[name])
    return text


class RedactingFormatter(logging.Formatter):
    """Formatter that redacts real friend names as a final safety net.

    It redacts the complete formatted string (message plus any traceback) so
    that unexpected text such as Playwright locator messages containing a real
    nickname never reaches public logs.
    """

    def __init__(
        self,
        fmt: str | None = None,
        datefmt: str | None = None,
        aliases: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(fmt, datefmt)
        self.aliases = dict(aliases or {})

    def format(self, record: logging.LogRecord) -> str:
        return redact_text(super().format(record), self.aliases)
