"""The strict edit applier.

These are the tests that matter most in the whole agent: a fuzzy applier turns
a loud, recoverable failure into a silent wrong edit, and a silent wrong edit
in the middle of a multi-file change is expensive to find.
"""

from __future__ import annotations

import pytest

from llm_assistant_agent.edits import EditError, apply_edit, unified_diff

SOURCE = """\
def alpha():
    return 1


def beta():
    return 1
"""


def test_replaces_a_unique_anchor() -> None:
    result = apply_edit(SOURCE, "def alpha():\n    return 1", "def alpha():\n    return 2")

    assert "def alpha():\n    return 2" in result
    assert "def beta():\n    return 1" in result


def test_refuses_an_anchor_that_is_not_present() -> None:
    with pytest.raises(EditError, match="not found"):
        apply_edit(SOURCE, "def gamma():", "def delta():")


def test_refuses_an_ambiguous_anchor_and_names_the_lines() -> None:
    with pytest.raises(EditError) as excinfo:
        apply_edit(SOURCE, "    return 1", "    return 2")

    message = str(excinfo.value)
    assert "appears 2 times" in message
    assert "lines 2, 6" in message
    assert "replace_all" in message


def test_replace_all_is_opt_in() -> None:
    result = apply_edit(SOURCE, "    return 1", "    return 2", replace_all=True)

    assert result.count("return 2") == 2


def test_refuses_a_no_op_edit() -> None:
    with pytest.raises(EditError, match="identical"):
        apply_edit(SOURCE, "def alpha():", "def alpha():")


def test_refuses_an_empty_anchor() -> None:
    """An empty old_string would insert at position zero of every file."""
    with pytest.raises(EditError, match="write_file"):
        apply_edit(SOURCE, "", "anything")


def test_indentation_mismatch_gets_an_actionable_hint() -> None:
    """The most common near miss: the model reflowed the snippet."""
    with pytest.raises(EditError) as excinfo:
        apply_edit(SOURCE, "def alpha():\nreturn 1", "def alpha():\nreturn 2")

    assert "indentation" in str(excinfo.value)


def test_no_whitespace_hint_when_the_text_is_simply_absent() -> None:
    with pytest.raises(EditError) as excinfo:
        apply_edit(SOURCE, "completely unrelated text", "x")

    assert "indentation" not in str(excinfo.value)


def test_whitespace_is_never_normalised_away() -> None:
    """Exact means exact - the applier must not 'helpfully' match anyway."""
    with pytest.raises(EditError):
        apply_edit("x = 1\n", "x  =  1", "x = 2")


def test_many_occurrences_are_truncated_in_the_message() -> None:
    content = "a\n" * 20

    with pytest.raises(EditError) as excinfo:
        apply_edit(content, "a", "b")

    assert "..." in str(excinfo.value)


def test_unified_diff_describes_the_actual_change() -> None:
    after = apply_edit(SOURCE, "    return 1\n\n\ndef beta", "    return 9\n\n\ndef beta")

    diff = unified_diff(SOURCE, after, "sample.py")

    assert "--- a/sample.py" in diff
    assert "-    return 1" in diff
    assert "+    return 9" in diff


def test_unified_diff_of_a_new_file() -> None:
    diff = unified_diff("", "hello\n", "new.py")

    assert "+hello" in diff


# --- explaining a miss -----------------------------------------------------
#
# "old_string was not found" is true and nearly useless. Measured over real
# sessions, anchors that failed ran to a median of 139 lines against 24 for
# those that applied: the model reconstructs a long block from memory and gets
# most of it right. These assertions are about saying which part it got wrong.


def test_a_near_miss_names_the_line_that_differs() -> None:
    content = "def main():\n    total = 0\n    return total\n"
    anchor = "def main():\n    total = 1\n    return total"

    with pytest.raises(EditError) as excinfo:
        apply_edit(content, anchor, "x")

    message = str(excinfo.value)
    assert "matches the file from line 1 for 1 line" in message
    assert "you sent:" in message and "total = 1" in message
    assert "the file has:" in message and "total = 0" in message


def test_an_indentation_miss_names_the_line_and_both_indents() -> None:
    """The loop this whole module exists to break.

    From a real session: a 72-line anchor that was byte-correct for 61 lines
    and then over-indented by four spaces. The old message said only "almost
    certainly indentation - re-read the file", so the model re-read a
    2400-line template and re-sent the identical anchor, five times. Nothing
    in the file looks wrong on a re-read: the code is the same code. The line
    number and the two indent widths are the only useful things to say.
    """
    content = "def main():\n    if x:\n        run()\n    return 1\n"
    anchor = "def main():\n    if x:\n            run()"

    with pytest.raises(EditError) as excinfo:
        apply_edit(content, anchor, "x")

    message = str(excinfo.value)
    assert "only in indentation, on line 3" in message
    assert "you sent:      12 spaces" in message
    assert "the file has:  8 spaces" in message
    assert "run()" in message
    # The un-locatable fallback must not be what answers a locatable miss.
    assert "could not be lined up" not in message


def test_a_block_whose_first_line_is_mis_indented_is_still_located() -> None:
    """Previously reported as never having been in the file at all.

    The exact-match search for the block's first line found nothing, so this
    took the "not copied from the file" branch - which tells the model to go
    and find the code, when the code is right there and only its indentation
    is wrong.
    """
    content = "body:\n    <!-- toggle -->\n    <button/>\n"
    anchor = "        <!-- toggle -->\n        <button/>"

    with pytest.raises(EditError) as excinfo:
        apply_edit(content, anchor, "x")

    message = str(excinfo.value)
    assert "lines up with line 2 of the file" in message
    assert "not in the file at all" not in message
    assert "you sent:      8 spaces" in message
    assert "the file has:  4 spaces" in message


def test_a_whitespace_only_line_is_named_rather_than_printed_blank() -> None:
    """A stripped report of a blank line prints nothing, and read as a bug."""
    content = "a = 1\n\nb = 2\n"
    anchor = "a = 1\n</body>"

    with pytest.raises(EditError) as excinfo:
        apply_edit(content, anchor, "x")

    assert "the file has:  (a blank line)" in str(excinfo.value)


def test_an_anchor_running_past_the_end_of_the_file_says_so() -> None:
    content = "a = 1\nb = 2"
    anchor = "a = 1\nb = 2\nc = 3\nd = 4"

    with pytest.raises(EditError) as excinfo:
        apply_edit(content, anchor, "x")

    assert "the file ends there" in str(excinfo.value)
    assert "2 more lines" in str(excinfo.value)


def test_a_reflowed_block_falls_back_to_the_unlocatable_hint() -> None:
    """No line of it matches, so there is no line number to give.

    The anchor is real code, wrapped across lines the file does not wrap. That
    is the one case where "it is whitespace, somewhere" is the honest answer -
    and it must not be mistaken for an anchor that was never in the file.
    """
    content = "if (a && b) {\n    run();\n}\n"
    anchor = "if (a &&\n    b) {"

    with pytest.raises(EditError) as excinfo:
        apply_edit(content, anchor, "x")

    message = str(excinfo.value)
    assert "could not be lined up" in message
    assert "not in the file at all" not in message


def test_an_invented_anchor_is_called_out_as_one() -> None:
    """Nothing to correct line by line - it was never copied from the file."""
    with pytest.raises(EditError) as excinfo:
        apply_edit("a = 1\nb = 2\n", "# parse the cardholder\nreturn rows", "x")

    message = str(excinfo.value)
    assert "first non-blank line is not in the file at all" in message
    assert "# parse the cardholder" in message


def test_a_long_anchor_is_told_why_it_will_never_match() -> None:
    content = "".join(f"line {i}\n" for i in range(200))
    anchor = "".join(f"wrong {i}\n" for i in range(100))

    with pytest.raises(EditError) as excinfo:
        apply_edit(content, anchor, "x")

    message = str(excinfo.value)
    assert "old_string is 101 lines" in message
    assert "write_file" in message


def test_a_file_too_large_to_rewrite_is_not_offered_write_file() -> None:
    """Advice that cannot be followed is worse than no advice.

    A whole-file write_file has to fit in one reply, and the output cap is
    8192 tokens. Told to rewrite a 108 kB template, a real session answered
    "let me just replace the entire file", was cut off mid-string, and gave up
    on the edit - the failure arriving as "Expecting ',' delimiter", which
    reads as a formatting slip rather than a size limit.
    """
    content = "".join(f"line {i}\n" for i in range(4000))
    anchor = "".join(f"wrong {i}\n" for i in range(100))

    with pytest.raises(EditError) as excinfo:
        apply_edit(content, anchor, "x")

    message = str(excinfo.value)
    assert "write_file is not an option here" in message
    assert "more than one reply can carry" in message


def test_a_small_file_may_still_be_offered_write_file() -> None:
    content = "".join(f"line {i}\n" for i in range(200))
    anchor = "".join(f"wrong {i}\n" for i in range(100))

    with pytest.raises(EditError) as excinfo:
        apply_edit(content, anchor, "x")

    message = str(excinfo.value)
    assert "use write_file to replace the file whole" in message
    assert "not an option" not in message


def test_a_short_anchor_is_not_lectured_about_length() -> None:
    with pytest.raises(EditError) as excinfo:
        apply_edit("a = 1\n", "b = 2", "c = 3")

    assert "lines. edit_file needs a byte-exact copy" not in str(excinfo.value)


def test_editing_from_memory_of_your_own_write_says_so() -> None:
    with pytest.raises(EditError) as excinfo:
        apply_edit("a = 1\n", "b = 2", "c = 3", written_since_read=True)

    assert "written this file since you last read it" in str(excinfo.value)


def test_a_successful_edit_is_unaffected_by_the_new_hints() -> None:
    assert apply_edit("a = 1\n", "a = 1", "a = 2", written_since_read=True) == "a = 2\n"


def test_an_identical_pair_whose_text_is_present_says_nothing_was_written() -> None:
    """The dominant case, by a long way.

    Over a real session, 16 identical old/new pairs: in 14 the text was
    already in the file. The model had made the change several turns earlier,
    lost track of it, and proposed it again from its plan rather than from the
    file it had just read. Telling it to "send something different" is the
    wrong instruction - it invites inventing a change to correct code.

    What the message must not do is decide that the *task* is finished. The
    anchor being present means the text is there, not that the change the user
    asked for has landed, and this fires in the middle of features that do not
    work yet.
    """
    with pytest.raises(EditError) as excinfo:
        apply_edit("a = 1\nb = 2\n", "a = 1", "a = 1")

    message = str(excinfo.value)
    assert "asks for no change" in message
    assert "Nothing was written" in message
    assert "already contains that text" in message
    assert "move on" not in message


def test_identical_text_that_is_not_in_the_file_is_a_different_problem() -> None:
    """Both fields hold the wanted result; neither holds what the file says."""
    with pytest.raises(EditError) as excinfo:
        apply_edit("a = 1\n", "z = 9", "z = 9")

    message = str(excinfo.value)
    assert "neither appears in the file" in message
    assert "already contains that text" not in message
