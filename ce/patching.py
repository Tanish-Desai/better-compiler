"""Apply explicit, uniquely anchored edits without replacing a source excerpt.

The displayed excerpt may start or end inside a C++ construct. Treating a
generated function as its replacement can delete neighboring definitions.
Edits here are matched against the original excerpt and applied atomically.
"""

from __future__ import annotations

from dataclasses import dataclass
import re


class PatchFormatError(ValueError):
    """A reply cannot be applied safely; no source has been changed."""


@dataclass(frozen=True)
class Edit:
    search: str
    replacement: str


_BLOCK = re.compile(
    r"^<<<<<<< SEARCH\n(.*?)^=======\n(.*?)^>>>>>>> REPLACE[ \t]*(?:\n|$)",
    re.MULTILINE | re.DOTALL,
)


def parse_edits(reply: str) -> list[Edit]:
    reply = reply.replace("\r\n", "\n")
    matches = list(_BLOCK.finditer(reply))
    if not matches:
        raise PatchFormatError("No complete SEARCH/REPLACE block was returned.")
    # Marker counts catch nested, incomplete and otherwise unparsed edits.
    for marker in ("<<<<<<< SEARCH", ">>>>>>> REPLACE"):
        if len(re.findall(r"^" + re.escape(marker) + r"[ \t]*$", reply, re.MULTILINE)) != len(matches):
            raise PatchFormatError("A SEARCH/REPLACE block is incomplete or nested.")
    edits = []
    for match in matches:
        search, replacement = match.groups()
        # The newline that separates content from its marker is framing, not
        # an instruction to insert/delete a newline at the source boundary.
        search = search.removesuffix("\n")
        replacement = replacement.removesuffix("\n")
        if not search.strip():
            raise PatchFormatError("SEARCH must contain existing source, not an empty insertion anchor.")
        if "```" in search or "```" in replacement:
            raise PatchFormatError("Place code fences outside the edit blocks, not inside the source.")
        edits.append(Edit(search, replacement))
    return edits


def apply_edits(original: str, reply: str) -> str:
    edits = parse_edits(reply)
    spans = []
    for edit in edits:
        start = original.find(edit.search)
        if start < 0:
            raise PatchFormatError("SEARCH was not found exactly in the original source excerpt.")
        if original.find(edit.search, start + 1) >= 0:
            raise PatchFormatError("SEARCH occurs more than once; include more unchanged context.")
        spans.append((start, start + len(edit.search), edit.replacement))
    spans.sort(key=lambda span: span[0])
    for previous, current in zip(spans, spans[1:]):
        if previous[1] > current[0]:
            raise PatchFormatError("SEARCH regions overlap; combine them into one edit.")
    result = original
    for start, end, replacement in reversed(spans):
        result = result[:start] + replacement + result[end:]
    return result


def extract_single_code(reply: str) -> str:
    """Legacy inspection only: never join prose between multiple code fences."""
    fences = re.findall(r"^```[^\n]*\n(.*?)^```[ \t]*$", reply.strip(), re.MULTILINE | re.DOTALL)
    if len(fences) > 1:
        raise PatchFormatError("Multiple code blocks are ambiguous; return explicit edits.")
    if len(fences) == 1:
        return fences[0].rstrip("\n")
    if "```" in reply:
        raise PatchFormatError("An unclosed code fence was returned.")
    return reply.strip()
