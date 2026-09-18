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

#: Past this many lines an anchor is a rewrite rather than an edit, and is
#: warned about by name when it misses. Set from real sessions: anchors that
#: applied ran to a median of 24 lines and a maximum of 167; the ones that
#: failed had a median of 139.
_LONG_ANCHOR_LINES = 60

#: Past this many characters a file cannot be re-emitted whole, so write_file
#: must not be suggested as the way out of a failed edit. Derived from the
#: output budget - ``client.DEFAULT_MAX_TOKENS`` of 8192, at roughly 3.5
#: characters per token, is about 28 kB of JSON-escaped source, and the reply
#: also has to carry the model's prose. Deliberately well under that: the
#: failure mode is silent truncation mid-string, and being wrong in this
#: direction only costs a suggestion, while being wrong in the other costs the
#: whole edit. Kept here rather than imported so this module stays independent
#: of the gateway; if that cap changes by much, change this with it.
_REWRITABLE_CHARS = 20_000


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
    """Last resort for a near miss that cannot be located line by line.

    Only reached when :func:`_divergence_hint` could not line the anchor up
    against the file at all - the model reflowed the block, so no single line
    of it matches even stripped. Says the difference is whitespace without
    saying where, which is all that can honestly be said in that case.

    It must not pre-empt the line-level report. This hint on its own is what
    produced the loop it was written to prevent: told only "almost certainly
    indentation. Re-read the file", the model re-read a 2400-line template
    five times and re-sent the same 72-line anchor each time, because a
    four-space error on line 1901 is not something a re-read reveals.
    """

    def squash(text: str) -> str:
        return re.sub(r"\s+", " ", text).strip()

    squashed_old = squash(old_string)
    if not squashed_old:
        return None
    if squash(content).count(squashed_old) > 0:
        return (
            " A block matching this text apart from whitespace does exist, but it "
            "could not be lined up against the file to say where - the line breaks "
            "differ too. Re-read the file and copy a few of its lines verbatim."
        )
    return None


def _leading(line: str) -> str:
    return line[: len(line) - len(line.lstrip())]


def _describe_indent(whitespace: str) -> str:
    """Indentation named in a way that survives being printed.

    The difference *is* the whitespace, so it cannot be shown by showing the
    line: two lines differing only in indentation print as the same line, and
    a whitespace-only line prints as nothing at all. Both happened in real
    sessions - one miss reported "the file has:" followed by an empty string.
    """
    if not whitespace:
        return "no indentation"
    if whitespace == " " * len(whitespace):
        return f"{len(whitespace)} space{'' if len(whitespace) == 1 else 's'}"
    if whitespace == "\t" * len(whitespace):
        return f"{len(whitespace)} tab{'' if len(whitespace) == 1 else 's'}"
    return f"the whitespace {whitespace!r}"


def _shown(line: str) -> str:
    """One line of code for the report, or a name for the fact that it is bare."""
    text = line.strip()[:100]
    return text or "(a blank line)"


def _divergence_hint(content: str, old_string: str) -> str | None:
    """Say where the anchor stops matching the file.

    "Not found" is true and nearly useless: for a long anchor it could be a
    stray space on line 2 or a wholly invented block, and those need opposite
    corrections. Measured on real sessions, the anchors that fail are ones the
    model reconstructed from memory - median 139 lines against 24 for the ones
    that apply - and they are usually right for most of their length. Naming
    the first line that differs turns a re-read of the whole file into a
    one-line correction.
    """
    wanted = old_string.split("\n")
    lines = content.split("\n")
    offset = next((index for index, line in enumerate(wanted) if line.strip()), -1)
    if offset < 0:
        return None
    anchor = wanted[offset]

    # Exact first, then on stripped text. Without the fallback an anchor whose
    # *first* line is already mis-indented cannot be located at all, and gets
    # reported as never having been in the file - the opposite correction to
    # the one it needs.
    starts = [index for index, line in enumerate(lines) if line == anchor]
    if not starts:
        starts = [index for index, line in enumerate(lines) if line.strip() == anchor.strip()]
    if not starts:
        # Nothing to line up against, so this cannot say where. Whether that
        # means "reflowed" or "invented" is not knowable from here; the hints
        # after this one in the chain answer that.
        return None

    best_start, best_matched = starts[0], -1
    for start in starts:
        matched = 0
        while (
            matched + offset < len(wanted)
            and start + matched < len(lines)
            and lines[start + matched] == wanted[offset + matched]
        ):
            matched += 1
        if matched > best_matched:
            best_start, best_matched = start, matched

    sent_index = offset + best_matched
    file_index = best_start + best_matched
    if sent_index >= len(wanted):
        return None  # ran off the end of the anchor; nothing useful to say
    sent = wanted[sent_index]
    at_end = file_index >= len(lines)
    actual = "" if at_end else lines[file_index]
    where = (
        # Zero matched lines means the block was found on its stripped text
        # alone: it begins where the file says, but its first line is already
        # wrong. "matches for 0 lines" is a confusing way to say that.
        f" It lines up with line {best_start + 1} of the file, but differs"
        if best_matched == 0
        else (
            f" It matches the file from line {best_start + 1} for {best_matched} line"
            f"{'' if best_matched == 1 else 's'}, then differs"
        )
    )

    if at_end:
        return (
            f"{where}: the file ends there, while your old_string carries on for "
            f"{len(wanted) - sent_index} more line"
            f"{'' if len(wanted) - sent_index == 1 else 's'}.\n"
            "  Anchor on a shorter run of lines around the change."
        )

    # Reported by name, and with the line number, because this is the miss that
    # a re-read does not fix: the two lines are the same code, so nothing in the
    # file looks wrong. Only the indentation and where to find it are useful.
    if sent.strip() == actual.strip():
        return (
            f"{where} only in indentation, on line {file_index + 1}:\n"
            f"    you sent:      {_describe_indent(_leading(sent))}\n"
            f"    the file has:  {_describe_indent(_leading(actual))}\n"
            f"    on this line:  {_shown(actual)}\n"
            "  Re-indent from that line on to match the file, or anchor on a shorter "
            "run of lines around the change."
        )

    return (
        f"{where} on line {file_index + 1}:\n"
        f"    you sent:      {_shown(sent)}\n"
        f"    the file has:  {_shown(actual)}\n"
        f"  Fix that line, or anchor on a shorter run of lines around the change."
    )


def _absent_anchor_hint(content: str, old_string: str) -> str | None:
    """The anchor was not copied from the file at all.

    Last of the three, because it prescribes the opposite correction to the
    other two: go and find the code, rather than fix a detail of it. Saying
    that about a block which is in the file - reflowed, or mis-indented - sends
    the model looking for something it is already looking at.
    """
    lines = content.split("\n")
    anchor = next((line for line in old_string.split("\n") if line.strip()), "")
    if not anchor or any(line.strip() == anchor.strip() for line in lines):
        return None
    return (
        f" Its first non-blank line is not in the file at all:\n    {_shown(anchor)}\n"
        "  So this anchor was not copied from the file. Call read_file and copy "
        "the few lines you mean to change."
    )


def _size_hint(old_string: str, content: str) -> str:
    """Long anchors do not fail by bad luck; they fail because they are long.

    The way out depends on the file. Offering a whole-file ``write_file`` for a
    file that cannot fit in one reply is advice that is guaranteed to fail, and
    the failure looks like a formatting problem rather than a size one, so the
    model does not learn that the route was closed. From a real session: told
    "or use write_file to replace the file whole" about a 108 kB template, it
    answered "let me just replace the entire file", the call was cut off by the
    output cap mid-string, and it abandoned the edit.
    """
    count = old_string.count("\n") + 1
    if count < _LONG_ANCHOR_LINES:
        return ""
    if len(content) > _REWRITABLE_CHARS:
        way_out = (
            f"Anchor on the few lines that actually change. This file is "
            f"{len(content) // 1000} kB, which is more than one reply can carry, so "
            "write_file is not an option here either - several small edits are."
        )
    else:
        way_out = (
            "Anchor on the few lines that actually change, or use write_file to "
            "replace the file whole."
        )
    return (
        f"\n  old_string is {count} lines. edit_file needs a byte-exact copy, and that "
        f"much text reproduced from memory almost never is one. {way_out}"
    )


def apply_edit(
    content: str,
    old_string: str,
    new_string: str,
    *,
    replace_all: bool = False,
    written_since_read: bool = False,
) -> str:
    """Return ``content`` with ``old_string`` replaced by ``new_string``.

    ``written_since_read`` says the caller has changed this file since it last
    read it, which makes a stale anchor the likeliest explanation for a miss.

    Raises:
        EditError: whenever the edit is not unambiguously safe to apply.
    """
    if not old_string:
        raise EditError(
            "old_string is empty. Use write_file to create a file or replace it wholesale."
        )

    if old_string == new_string:
        # Common, and it means one of two opposite things, so the file has to
        # be consulted before saying anything. Measured over a real session:
        # 14 of these, and in 12 the text was already in the file. The model
        # had made the change several turns earlier, lost track, and proposed
        # it again from its plan rather than from what it had just read.
        #
        # Telling it to "send a new_string that differs" - which is what this
        # said before the transcripts were checked - is the wrong instruction
        # for that case. It invites the model to invent a difference in code
        # that is already correct.
        if old_string in content:
            # Says what is true - this call changes nothing - without deciding
            # for the model whether its *task* is done. It cannot know that
            # from here: the anchor being present means the text is there, not
            # that the change the user asked for has landed. An earlier version
            # ended "move on to the next thing", which is the wrong thing to
            # say to a model in the middle of a feature that does not work yet.
            raise EditError(
                "old_string and new_string are identical, so this call asks for no "
                "change, and the file already contains that text. Nothing was written. "
                "If you meant to change something, send the text as it is now in "
                "old_string and the text you want in new_string. If you meant to add "
                "something, anchor on the line you want to add it next to. Call "
                "git_diff to see what has actually landed so far."
            )
        raise EditError(
            "old_string and new_string are identical, and neither appears in the file. "
            "Both fields hold what you want the file to become; old_string has to hold "
            "what it contains now. Call read_file and copy the current text into it."
        )

    occurrences = content.count(old_string)

    if occurrences == 0:
        stale = (
            "\n  You have written this file since you last read it, so it no longer "
            "says what you remember. Call read_file before editing it again."
            if written_since_read
            else ""
        )
        # Locating the miss comes first. The whitespace hint cannot say *where*,
        # and when it answered first it hid the one report that could: a model
        # told "it is indentation somewhere in this 2400-line file" re-reads the
        # file and sends the identical anchor again, which is the loop the repeat
        # detector then has to break.
        # Most specific first. Each of the three prescribes a different
        # correction, so the order is the whole point: fix this line at this
        # line number, then copy the file's line breaks, then go and find the
        # code. Answering with a vaguer one than the evidence supports is what
        # turns a miss into a retry loop.
        hint = (
            _divergence_hint(content, old_string)
            or _whitespace_insensitive_hint(content, old_string)
            or _absent_anchor_hint(content, old_string)
            or " Re-read the file: the text may have changed, or it may never have been there."
        )
        size = _size_hint(old_string, content)
        raise EditError(f"old_string was not found in the file.{hint}{stale}{size}")

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
