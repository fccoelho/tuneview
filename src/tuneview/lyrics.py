"""Lyrics typed in TuneView: plain text, laid out as a large-print lyrics page.

Verses are separated by blank lines. A line like "[Chorus]" or "Verse 2:" at
the top of a verse is shown as its heading.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
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


def first_verse_words(text: str) -> list[str]:
    """The words of the first verse, which is what the chord chart aligns."""
    vs = verses(text)
    return [word for line in vs[0].lines for word in line.split()] if vs else []


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


def _join_verse(entries: list[tuple[int, str, str]]) -> tuple[str, list[tuple[int, str]]]:
    """Join one verse's lyric syllables into words, keeping each word's measure.

    entries are (measure index, syllabic, text); returns (verse text, words
    paired with the measure where the word starts).
    """
    words: list[tuple[int, str]] = []
    current = ""
    current_measure = 0
    for measure, syllabic, raw in entries:
        text = " ".join(raw.split())
        if not text:
            continue
        kind = syllabic.strip().lower()
        if kind == "begin":
            if current:
                words.append((current_measure, current))
            current, current_measure = text, measure
        elif kind == "middle":
            if not current:
                current_measure = measure
            current += text
        elif kind == "end":
            if not current:
                current_measure = measure
            current += text
            words.append((current_measure, current))
            current = ""
        else:  # single, or no <syllabic>
            if current:
                words.append((current_measure, current))
                current = ""
            words.extend((measure, token) for token in text.split())
    if current:
        words.append((current_measure, current))
    return " ".join(word for _, word in words), words


def extract_lyrics_with_alignment(musicxml: str | bytes) -> tuple[str, list[str], list[int]]:
    """Lyrics embedded in a MusicXML score, with the bars each word belongs to.

    Returns (text, bar numbers, words per bar).  The text is ready for the
    Lyrics page; the bar numbers/counts are a default chord-chart alignment for
    the first verse, so the words appear under the bars where they are actually
    sung.  Further verses are kept in the text and can be edited as usual.
    """
    try:
        root = ET.fromstring(musicxml)
    except ET.ParseError:
        return "", [], []
    parts = root.findall("part") or [root]
    part = max(parts, key=lambda p: len(p.findall(".//lyric")), default=None)
    if part is None or not part.findall(".//lyric"):
        return "", [], []

    measures = part.findall("measure")
    verses: dict[str, list[tuple[int, str, str]]] = {}
    for index, measure in enumerate(measures):
        for lyric in measure.iter("lyric"):
            text = lyric.findtext("text") or ""
            if not text.strip():
                continue
            number = lyric.get("number") or "1"
            verses.setdefault(number, []).append(
                (index, lyric.findtext("syllabic") or "single", text))

    blocks = []
    verse_words = []
    for entries in verses.values():
        text, words = _join_verse(entries)
        blocks.append(text)
        verse_words.append(words)
    text = "\n\n".join(block for block in blocks if block)
    if not text:
        return "", [], []

    bars: list[str] = []
    counts: list[int] = []
    if measures and verse_words:
        bars = [m.get("number") or str(i + 1) for i, m in enumerate(measures)]
        counts = [0] * len(measures)
        for measure, word in verse_words[0]:
            counts[measure] += len(word.split())
        if sum(counts) != len(first_verse_words(text)):
            bars, counts = [], []
    return text, bars, counts


def extract_lyrics(musicxml: str | bytes) -> str:
    """Lyrics embedded in a MusicXML score, with hyphenated syllables joined."""
    return extract_lyrics_with_alignment(musicxml)[0]
