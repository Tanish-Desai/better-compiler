"""A model's prose or an incomplete excerpt must never delete adjacent code."""

import pytest

from ce.patching import PatchFormatError, apply_edits, extract_single_code


def block(search, replacement):
    return f"<<<<<<< SEARCH\n{search}\n=======\n{replacement}\n>>>>>>> REPLACE"


def test_targeted_edit_preserves_neighboring_function_and_partial_boundary():
    original = "  assert(x &&\n         y);\n}\n\nint neighbor() { return 7; }\n"
    updated = apply_edits(original, block("assert(x &&", "assert(z &&"))
    assert updated == original.replace("assert(x &&", "assert(z &&")
    assert "int neighbor() { return 7; }" in updated


def test_prose_and_outer_fences_never_become_cpp():
    reply = "Explanation.\n```cpp\n" + block("return 0;", "return 1;") + "\n```\nMore prose."
    assert apply_edits("int f() { return 0; }", reply) == "int f() { return 1; }"


def test_multiple_edits_resolve_against_original_atomically():
    reply = block("a = 1;", "a = 2;") + "\n" + block("b = 2;", "b = 3;")
    assert apply_edits("a = 1;\nb = 2;", reply) == "a = 2;\nb = 3;"


@pytest.mark.parametrize("original,reply", [
    ("return 0;\nreturn 0;", block("return 0;", "return 1;")),
    ("return 0;", block("return 2;", "return 1;")),
    ("abc", block("abc", "x") + "\n" + block("bc", "y")),
    ("abc", block("abc", "x") + "\n<<<<<<< SEARCH\nmissing delimiter"),
    ("abc", block("", "x")),
    ("abc", "```cpp\nint f() {}\n```"),
    ("abc", block("abc", "```cpp\nx\n```")),
])
def test_unsafe_replies_are_rejected_without_partial_changes(original, reply):
    saved = original
    with pytest.raises(PatchFormatError):
        apply_edits(original, reply)
    assert original == saved


def test_empty_replacement_is_an_explicit_deletion():
    assert apply_edits("a;\nb;\n", block("a;\n", "")) == "b;\n"


def test_legacy_inspector_rejects_multiple_fences_instead_of_greedy_capture():
    with pytest.raises(PatchFormatError):
        extract_single_code("```cpp\nx();\n```\nEnglish\n```cpp\ny();\n```")
    assert extract_single_code("Explanation\n```cpp\nx();\n```\nDone") == "x();"
