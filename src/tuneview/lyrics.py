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


def lyric_words(text: str) -> list[str]:
    """Every word of the lyrics in order, with section headings left out.

    Blank lines mark verses on the Lyrics page, but the words are still sung in
    order, so the chord chart aligns one continuous stream of words to its bars.
    """
    return [word for verse in verses(text) for line in verse.lines for word in line.split()]


def spread_words(n_words: int, n_bars: int) -> list[int]:
    """How many consecutive words land on each bar when they're spread evenly."""
    if n_bars <= 0 or n_words <= 0:
        return [0] * max(n_bars, 0)
    edges = [int(i * n_words / n_bars + 0.5) for i in range(n_bars + 1)]
    return [edges[i + 1] - edges[i] for i in range(n_bars)]


def chunks(words: list[str], counts: list[int]) -> list[list[str]]:
    """Split words into one list per bar; words past the counts go on the last bar."""
    out: list[list[str]] = []
    start = 0
    for count in counts:
        out.append(words[start:start + count])
        start += count
    if start < len(words) and out:
        out[-1].extend(words[start:])
    return out


def split_words(text: str, caret: int) -> tuple[str, str]:
    """Split a bar's words at the caret; the gap around the split is trimmed."""
    caret = max(0, min(caret, len(text)))
    return text[:caret].rstrip(), text[caret:].lstrip()


def join_words(left: str, right: str) -> str:
    """Join two bars' words with a single space."""
    return f"{left.rstrip()} {right.lstrip()}".strip()



def split_bars(bars: list[str], index: int, caret: int) -> list[str]:
    """Split one bar's words at the caret; the later bars shift forward.

    The new tail becomes its own bar, so the last bar takes the overflow of the
    old last two so that no words are lost.
    """
    if not (0 <= index < len(bars)) or index + 1 >= len(bars):
        return bars
    before, after = split_words(bars[index], caret)
    if not after:
        return bars
    tail = [after, *bars[index + 1:]]  # one entry longer than the bars left
    slots = len(bars) - index - 1
    new_tail = [*tail[:slots - 1], join_words(tail[slots - 1], tail[slots])]
    return [*bars[:index], before, *new_tail]


def join_bars(bars: list[str], index: int) -> list[str]:
    """Join bar `index` into the bar before it; the later bars shift back.

    The last bar becomes empty, so a gap opened by the join ends up at the end
    of the chart instead of in the middle of the words.
    """
    if not (0 < index < len(bars)):
        return bars
    lines = list(bars)
    lines[index - 1] = join_words(lines[index - 1], lines[index])
    for j in range(index, len(lines) - 1):
        lines[j] = lines[j + 1]
    lines[-1] = ""
    return lines
