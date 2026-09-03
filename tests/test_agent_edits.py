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
