"""Lyrics typed in TuneView: plain text, laid out as a large-print lyrics page.

Verses are separated by blank lines. A line like "[Chorus]" or "Verse 2:" at
the top of a verse is shown as its heading.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# "[Anything]" is a heading; "Name:" only for usual section names, so a verse
# whose first line ends in a colon ("And she said:") stays a lyric.
_SECTIONS = (
    "verse|chorus|refrain|pre-chorus|prechorus|bridge|intro|outro|coda|tag|vamp|interlude"
    "|ending|solo|head|estrofe|refrão|refrao|ponte|introdução|introducao|final|coro"
)
_HEADING_RE = re.compile(
    rf"^\[(.+)\]$|^((?:{_SECTIONS})(?:\s+[\w'.]+){{0,2}}|[A-D]\d?):$", re.IGNORECASE
)


@dataclass
class Verse:
    heading: str = ""
    lines: list[str] = field(default_factory=list)


def heading_of(line: str) -> str | None:
    """'[Chorus]' -> 'Chorus', 'Verse 2:' -> 'Verse 2'; None for an ordinary line."""
    m = _HEADING_RE.match(line.strip())
    if not m:
        return None
    return (m.group(1) or m.group(2)).strip() or None


def verses(text: str) -> list[Verse]:
    """Split lyrics into verses at blank lines, picking out their headings."""
    out: list[Verse] = []
    for block in re.split(r"\n\s*\n", text.replace("\r\n", "\n").strip()):
        lines = [ln.rstrip() for ln in block.split("\n")]
        lines = [ln for ln in lines if ln.strip()]
        if not lines:
            continue
        verse = Verse()
        if (h := heading_of(lines[0])) is not None:
            verse.heading, lines = h, lines[1:]
        verse.lines = [ln.strip() for ln in lines]
        out.append(verse)
    return out
