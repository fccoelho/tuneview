"""Turn MusicXML into what the musician sees: engraved pages and a chord chart.

Verovio does the engraving (and the transposition, for both views), resvg turns
its SVG into PNG for Flet, and the chord chart is read from Verovio's MEI so its
chord spellings always match the notation.
"""

from __future__ import annotations

import re
import threading
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import resvg_py
import verovio

MEI = "{http://www.music-encoding.org/ns/mei}"

# Written pitch relative to concert pitch, in semitones.
INSTRUMENTS: dict[str, int] = {
    "C": 0,  # concert: piano, guitar, flute, voice
    "B♭": 2,  # trumpet, tenor & soprano sax, clarinet
    "E♭": -3,  # alto & baritone sax
    "F": 7,  # horn
}

# Verovio writes chord-symbol accidentals as SMuFL glyphs in its Leipzig font,
# which resvg can't load (it ships as an embedded WOFF2), so swap in Unicode.
_SMUFL_CSYM = {"\uea64": "♭", "\uea65": "♮", "\uea66": "♯", "\uea67": "𝄪", "\uea68": "𝄫"}
_CSYM_RE = re.compile(r'<tspan font-family="Leipzig" font-size="(\d+)px">(.)</tspan>')

_FONT_CANDIDATES = [
    # Linux
    "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSerif.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/TTF/DejaVuSerif.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSerif-Regular.ttf",
    # macOS
    "/System/Library/Fonts/Supplemental/Times New Roman.ttf",
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    # Windows
    "C:/Windows/Fonts/times.ttf",
    "C:/Windows/Fonts/seguisym.ttf",
]


@lru_cache(maxsize=1)
def _fonts() -> list[str]:
    """Explicit font files: loading every system font costs seconds per render."""
    return [f for f in _FONT_CANDIDATES if Path(f).exists()]


def fix_harmony_offsets(musicxml: str) -> str:
    """Move chord symbols with a negative <offset> in front of the note they belong to.

    Finale writes a chord on beat 4 of a bar of rest as "after the rest, offset -1
    beat"; Verovio ignores negative offsets and stacks the chord on beat 1. Placing
    it before the rest with a positive offset gives the same timing, which Verovio
    honours.
    """
    if "<offset>-" not in musicxml:
        return musicxml
    root = ET.fromstring(musicxml)
    for measure in root.iter("measure"):
        pos = 0
        timeline: list[tuple[int, int, ET.Element]] = []  # (start, end, note)
        moves: list[tuple[ET.Element, ET.Element, int]] = []
        for el in list(measure):
            dur = int(el.findtext("duration", "0") or 0)
            if el.tag == "note":
                if el.find("chord") is not None:
                    continue
                if el.find("grace") is None:
                    timeline.append((pos, pos + dur, el))
                    pos += dur
            elif el.tag == "backup":
                pos -= dur
            elif el.tag == "forward":
                pos += dur
            elif el.tag == "harmony":
                off = el.find("offset")
                if off is None or not (off.text or "").strip().startswith("-"):
                    continue
                target = pos + int(float(off.text))
                host = next((n for n in reversed(timeline) if n[0] <= target < n[1]), None)
                if host is not None:
                    moves.append((el, host[2], target - host[0]))
        for harm, note, offset in moves:
            measure.remove(harm)
            off = harm.find("offset")
            if offset:
                off.text = str(offset)
            else:
                harm.remove(off)
            measure.insert(list(measure).index(note), harm)
    return ET.tostring(root, encoding="unicode")


def transpose_option(semitones: int) -> str:
    return f"{semitones:+d}" if semitones else ""


def normalize_shift(semitones: int) -> int:
    """Fold a transposition into -5..+6 so the sheet stays near its written range."""
    return (semitones + 5) % 12 - 5


# ----------------------------------------------------------------------------
# Chord chart model
# ----------------------------------------------------------------------------
@dataclass
class ChartChord:
    text: str
    beat: float  # 1-based position in the bar


@dataclass
class ChartBar:
    number: str
    chords: list[ChartChord] = field(default_factory=list)
    beats: float = 4
    repeat_start: bool = False
    repeat_end: bool = False
    final: bool = False
    double: bool = False
    ending: str = ""  # "1", "2"… on the first bar of a volta
    rest_bars: int = 0  # >0 for a multi-bar rest
    marks: list[str] = field(default_factory=list)  # segno, coda, rehearsal…
    directions: list[str] = field(default_factory=list)  # "D.S. al Coda", "Fine"…


def _harm_text(el: ET.Element) -> str:
    return " ".join("".join(el.itertext()).split())


def chart_from_mei(mei: str) -> list[ChartBar]:
    root = ET.fromstring(mei)
    beats = 4.0
    bars: list[ChartBar] = []
    seen_endings: set[int] = set()

    def walk(el: ET.Element, ending: ET.Element | None) -> None:
        nonlocal beats
        for child in el:
            tag = child.tag.removeprefix(MEI)
            if tag == "scoreDef":
                count = child.get("meter.count")
                if count is None and (ms := child.find(f".//{MEI}meterSig")) is not None:
                    count = ms.get("count")
                if count:
                    beats = float(sum(float(c) for c in count.split("+")))
            elif tag == "ending":
                walk(child, child)
            elif tag in ("section", "mdiv", "score", "body", "music"):
                walk(child, ending)
            elif tag == "measure":
                bars.append(_bar(child, ending))

    def _bar(m: ET.Element, ending: ET.Element | None) -> ChartBar:
        nonlocal beats
        if (ms := m.find(f".//{MEI}meterSig")) is not None and ms.get("count"):
            beats = float(sum(float(c) for c in ms.get("count").split("+")))
        bar = ChartBar(number=m.get("n", ""), beats=beats)
        left, right = m.get("left", ""), m.get("right", "")
        bar.repeat_start = left in ("rptstart", "rptboth") or right == "rptboth"
        bar.repeat_end = right in ("rptend", "rptboth")
        bar.final = right == "end"
        bar.double = right in ("dbl", "dbldashed", "dbldotted")
        if ending is not None and id(ending) not in seen_endings:
            seen_endings.add(id(ending))
            bar.ending = ending.get("n") or ending.get("label") or ""
        if (mr := m.find(f".//{MEI}multiRest")) is not None:
            bar.rest_bars = int(mr.get("num", "1"))
        harms = [h for h in m.iter(f"{MEI}harm") if h.get("staff", "1").split()[0] == "1"]
        if not harms:  # chords may sit on another staff (e.g. a piano part)
            harms = list(m.iter(f"{MEI}harm"))
        seen = set()
        for h in harms:
            text, beat = _harm_text(h), float(h.get("tstamp", "1"))
            if text and (text, beat) not in seen:
                seen.add((text, beat))
                bar.chords.append(ChartChord(text, beat))
        bar.chords.sort(key=lambda c: c.beat)
        for rm in m.iter(f"{MEI}repeatMark"):
            func = rm.get("func", "")
            if func in ("segno", "coda"):
                bar.marks.append(func)
            elif text := _harm_text(rm):
                bar.directions.append(text)
        for reh in m.iter(f"{MEI}reh"):
            if text := _harm_text(reh):
                bar.marks.append(text)
        for d in m.iter(f"{MEI}dir"):
            if text := _harm_text(d):
                bar.directions.append(text)
        return bar

    for body in root.iter(f"{MEI}body"):
        walk(body, None)
    return bars


# ----------------------------------------------------------------------------
# Engraving
# ----------------------------------------------------------------------------
@dataclass
class Engraving:
    pages: int
    mei: str


class Engraver:
    """One Verovio toolkit, guarded by a lock so renders can run off the UI thread."""

    PAGE_WIDTH = 2100  # MEI units: A4 proportions at Verovio's default

    def __init__(self) -> None:
        self._tk = verovio.toolkit()
        verovio.enableLog(verovio.LOG_OFF)
        self._lock = threading.Lock()

    def load(self, musicxml: str, *, transpose: int = 0, zoom: int = 45,
             page_width: int = PAGE_WIDTH) -> Engraving:
        """Engrave a score. A narrower page_width puts fewer bars on each line."""
        with self._lock:
            self._tk.setOptions(
                {
                    "pageWidth": page_width,
                    "pageHeight": 2970,
                    "adjustPageHeight": True,
                    "pageMarginTop": 40,
                    "pageMarginBottom": 40,
                    "scale": zoom,
                    "header": "none",  # title & composer are shown by the app
                    "footer": "none",
                    "breaks": "auto",
                    "spacingLinear": 0.6,
                    "spacingNonLinear": 0.5,
                    "harmDist": 2.5,
                    "transpose": transpose_option(transpose),
                }
            )
            if not self._tk.loadData(fix_harmony_offsets(musicxml)):
                raise ValueError("Verovio could not read this MusicXML file")
            return Engraving(pages=self._tk.getPageCount(), mei=self._tk.getMEI())

    def render_page(self, page: int, *, width_px: int = 1600, ink: str = "#000000") -> bytes:
        """PNG of one page (1-based) of the loaded score, on a transparent background."""
        with self._lock:
            svg = self._tk.renderToSVG(page)
        svg = _CSYM_RE.sub(_fix_csym, svg)
        # Glyphs and lines use currentColor; text has no fill and would stay black.
        svg = svg.replace('color="black"', f'color="{ink}" fill="{ink}"', 1)
        fonts = _fonts()
        return bytes(
            resvg_py.svg_to_bytes(
                svg_string=svg,
                width=width_px,
                skip_system_fonts=bool(fonts),
                font_files=fonts or None,
                font_family="DejaVu Serif",
                serif_family="DejaVu Serif",
            )
        )


def _fix_csym(m: re.Match) -> str:
    size, glyph = int(m.group(1)), m.group(2)
    if glyph not in _SMUFL_CSYM:
        return m.group(0)
    return f'<tspan font-size="{size * 0.6:.0f}px" baseline-shift="35%">{_SMUFL_CSYM[glyph]}</tspan>'


_ROOT_RE = re.compile(r"^([A-G][♭♯𝄪𝄫#b]?)(.*?)(?:/([A-G][♭♯𝄪𝄫#b]?))?$")


def split_chord(text: str) -> tuple[str, str, str]:
    """'B♭maj7/D' -> ('B♭', 'maj7', 'D'); anything unparseable comes back as the root."""
    m = _ROOT_RE.match(text.replace(" ", ""))
    if not m:
        return text, "", ""
    return m.group(1), m.group(2) or "", m.group(3) or ""
