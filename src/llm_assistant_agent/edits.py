"""Applying model-proposed edits to source files.

This is the part of an agent that most often goes quietly wrong, so the rules
here are deliberately unforgiving:

* the anchor must match **exactly** - no whitespace normalisation, no fuzzy
  fallback, no "closest match"
* the anchor must be **unique** in the file, unless the caller asked for a
  replace-all
* a failure returns a message written *for the model*, telling it what to do
  differently, because the recovery path is the model re-reading and retrying

A fuzzy applier trades a loud failure for a silent wrong edit, and a silent
wrong edit in a 30-file refactor is much more expensive than a retry. Qwen3
re-reads and corrects itself reliably when the error says what was wrong.
"""

from __future__ import annotations

import difflib
import re

__all__ = ["EditError", "apply_edit", "unified_diff"]

#: How many occurrences to name before giving up on listing line numbers.
_MAX_REPORTED_LINES = 5


class EditError(Exception):
    """An edit that could not be applied safely.

    The message is part of the interface: it is handed back to the model as a
    tool result, so it must say what to do next rather than merely what broke.
    """


def _line_numbers_of(content: str, needle: str) -> list[int]:
    lines: list[int] = []
    start = 0
    while (index := content.find(needle, start)) != -1:
        lines.append(content.count("\n", 0, index) + 1)
        start = index + 1
    return lines


def _whitespace_insensitive_hint(content: str, old_string: str) -> str | None:
    """Explain a near miss, which is nearly always indentation.

    A model working from a prompt that reflowed a snippet will produce an
    anchor that is right in every respect except leading spaces. Saying so
    turns a retry loop into a single corrected call.
    """

    def squash(text: str) -> str:
        return re.sub(r"\s+", " ", text).strip()

    squashed_old = squash(old_string)
    if not squashed_old:
        return None
    if squash(content).count(squashed_old) > 0:
        return (
            " A block matching this text apart from whitespace does exist - the "
            "difference is almost certainly indentation. Re-read the file and copy "
            "the exact leading whitespace."
        )
    return None


def apply_edit(
    content: str,
    old_string: str,
    new_string: str,
    *,
    replace_all: bool = False,
) -> str:
    """Return ``content`` with ``old_string`` replaced by ``new_string``.

    Raises:
        EditError: whenever the edit is not unambiguously safe to apply.
    """
    if old_string == new_string:
        raise EditError("old_string and new_string are identical, so this edit is a no-op.")

    if not old_string:
        raise EditError(
            "old_string is empty. Use write_file to create a file or replace it wholesale."
        )

    occurrences = content.count(old_string)

    if occurrences == 0:
        hint = _whitespace_insensitive_hint(content, old_string) or (
            " Re-read the file: the text may have changed, or it may never have been there."
        )
        raise EditError(f"old_string was not found in the file.{hint}")

    if occurrences > 1 and not replace_all:
        lines = _line_numbers_of(content, old_string)
        shown = ", ".join(str(line) for line in lines[:_MAX_REPORTED_LINES])
        more = "" if len(lines) <= _MAX_REPORTED_LINES else ", ..."
        raise EditError(
            f"old_string appears {occurrences} times (lines {shown}{more}), so this "
            f"edit is ambiguous. Include more surrounding context to make it unique, "
            f"or pass replace_all=true if every occurrence should change."
        )

    if replace_all:
        return content.replace(old_string, new_string)

    return content.replace(old_string, new_string, 1)


def unified_diff(before: str, after: str, path: str, *, context: int = 3) -> str:
    """A diff of what actually changed on disk.

    Computed here rather than taken from the model's description of its own
    edit, so that what the user reviews is ground truth.
    """
    lines = difflib.unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile=f"a/{path}",
        tofile=f"b/{path}",
        n=context,
    )
    return "".join(lines)
