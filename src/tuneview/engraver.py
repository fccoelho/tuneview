"""Turn MusicXML into what the musician sees: engraved pages and a chord chart.

Verovio does the engraving (and the transposition, for both views), resvg turns
its SVG into PNG for Flet, and the chord chart is read from Verovio's MEI so its
chord spellings always match the notation.
"""

from __future__ import annotations

import re
import threading
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import resvg_py
import verovio

MEI = "{http://www.music-encoding.org/ns/mei}"
SVG = "{http://www.w3.org/2000/svg}"
XML_ID = "{http://www.w3.org/XML/1998/namespace}id"

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


# ----------------------------------------------------------------------------
# Chord-symbol style
# ----------------------------------------------------------------------------
# How chord symbols are spelled: as the source file labels them, or in a compact
# jazz style (Cm7, C△7, Cø7, C°7).
CHORD_STYLES = {"compact": "Compact", "written": "As written"}

# Compact labels for chord kinds the file leaves unlabelled, where Verovio's own
# default (CMaj7, Cm7(♭5), Cdim7…) isn't already compact.
_COMPACT_KIND = {
    "major-seventh": "△7",
    "major-ninth": "△9",
    "major-13th": "△13",
    "major-minor": "m(△7)",
    "minor-major": "m(△7)",
    "half-diminished": "ø7",
    "diminished": "°",
    "diminished-seventh": "°7",
}


def compact_quality(quality: str) -> str:
    """'min7' -> 'm7', 'maj7' -> '△7', 'min7(b5)' -> 'ø7', 'dim7' -> '°7'…"""
    q = quality.strip()
    q = re.sub(r"^(?:min|mi|-)", "m", q)
    q = re.sub(r"^m7\(?[b♭]5\)?", "ø7", q)
    q = re.sub(r"^(?:dim|o)(?=7|$)", "°", q)
    if q in ("maj", "ma", "Maj"):  # a plain major triad
        return ""
    q = re.sub(r"^maj6", "6", q)
    return re.sub(r"(?:maj|Maj|ma|Ma|M)(?=[79]|1[13])", "△", q)


def compact_chords(musicxml: str) -> str:
    """Relabel every chord symbol in the compact style, before Verovio reads it.

    A half-diminished chord written as min7 with a flattened 5th becomes ø7.
    """
    if "<harmony" not in musicxml:
        return musicxml
    root = ET.fromstring(musicxml)
    for harmony in root.iter("harmony"):
        kind = harmony.find("kind")
        if kind is None:
            continue
        text = kind.get("text")
        text = compact_quality(text) if text is not None else _COMPACT_KIND.get((kind.text or "").strip())
        is_m7 = text == "m7" or (text is None and (kind.text or "").strip() == "minor-seventh")
        if is_m7:
            for degree in harmony.findall("degree"):
                if (degree.findtext("degree-value", "").strip() == "5"
                        and degree.findtext("degree-alter", "").strip() == "-1"):
                    harmony.remove(degree)
                    text = "ø7"
                    break
        if text is not None:
            kind.set("text", text)
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
    id: str = ""  # MEI xml:id of the <harm>, shared with the SVG


@dataclass
class ChartBar:
    number: str
    id: str = ""  # MEI xml:id, which links the bar to playback and the page
    chords: list[ChartChord] = field(default_factory=list)
    beats: float = 4  # meter count
    unit: int = 4  # meter unit: 4 = quarter-note beats, 8 = eighth-note beats
    repeat_start: bool = False
    repeat_end: bool = False
    final: bool = False
    double: bool = False
    ending: str = ""  # "1", "2"… on the first bar of a volta
    rest_bars: int = 0  # >0 for a multi-bar rest
    marks: list[str] = field(default_factory=list)  # segno, coda, rehearsal…
    directions: list[str] = field(default_factory=list)  # "D.S. al Coda", "Fine"…
    clef: str = "G2"  # first staff: shape + line ("G2", "F4"), "" for others
    key_sig: int = 0  # sharps (+) or flats (-) in the key signature


def _harm_text(el: ET.Element) -> str:
    return " ".join("".join(el.itertext()).split())


def _clef(c: ET.Element) -> str:
    shape, line = c.get("shape", ""), c.get("line", "")
    return shape + line if shape in ("G", "F") else ""


def _key_sig(sig: str) -> int:
    """MEI '3f' -> -3, '2s' -> 2, '0' -> 0."""
    m = re.fullmatch(r"(\d+)([sf]?)", sig.strip())
    if not m:
        return 0
    return -int(m.group(1)) if m.group(2) == "f" else int(m.group(1))


def chart_from_mei(mei: str) -> list[ChartBar]:
    root = ET.fromstring(mei)
    beats = 4.0
    bars: list[ChartBar] = []
    seen_endings: set[int] = set()

    unit = 4
    clef, key_sig = "G2", 0

    def set_staff(staff_def: ET.Element | None) -> None:
        """Clef and key of the first staff, from a staffDef (or a mid-piece change)."""
        nonlocal clef, key_sig
        if staff_def is None:
            return
        if (c := staff_def.find(f"{MEI}clef")) is not None:
            clef = _clef(c)
        if (k := staff_def.find(f"{MEI}keySig")) is not None:
            key_sig = _key_sig(k.get("sig", "0"))

    def first_staff_def(el: ET.Element) -> ET.Element | None:
        defs = list(el.iter(f"{MEI}staffDef"))
        return next((d for d in defs if d.get("n") == "1"), defs[0] if defs else None)

    def set_meter(count: str | None, unit_: str | None) -> None:
        nonlocal beats, unit
        if count:
            beats = float(sum(float(c) for c in count.split("+")))
        if unit_:
            unit = int(unit_)

    def walk(el: ET.Element, ending: ET.Element | None) -> None:
        nonlocal key_sig
        for child in el:
            tag = child.tag.removeprefix(MEI)
            if tag == "scoreDef":
                if (ms := child.find(f".//{MEI}meterSig")) is not None:
                    set_meter(ms.get("count"), ms.get("unit"))
                set_meter(child.get("meter.count"), child.get("meter.unit"))
                set_staff(first_staff_def(child))
                if (k := child.find(f"{MEI}keySig")) is not None:  # a key change for all staves
                    key_sig = _key_sig(k.get("sig", "0"))
            elif tag == "ending":
                walk(child, child)
            elif tag in ("section", "mdiv", "score", "body", "music"):
                walk(child, ending)
            elif tag == "measure":
                bars.append(_bar(child, ending))

    def _bar(m: ET.Element, ending: ET.Element | None) -> ChartBar:
        if (ms := m.find(f".//{MEI}meterSig")) is not None:
            set_meter(ms.get("count"), ms.get("unit"))
        nonlocal clef
        for staff in m.iter(f"{MEI}staff"):  # a clef change inside the first staff
            if staff.get("n") == "1":
                for c in staff.iter(f"{MEI}clef"):
                    clef = _clef(c)
        bar = ChartBar(number=m.get("n", ""), id=m.get(XML_ID, ""), beats=beats, unit=unit,
                       clef=clef, key_sig=key_sig)
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
                bar.chords.append(ChartChord(text, beat, h.get(XML_ID, "")))
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
    # Bars in playing order (repeats played out): (bar id, start in quarter notes)
    timeline: list[tuple[str, float]] = field(default_factory=list)
    # The first staff's notes in playing order: (start, end, MIDI pitch), in
    # quarter notes, tied notes merged; written pitch (after transposition)
    melody: list[tuple[float, float, int]] = field(default_factory=list)
    # Tempo changes: (position in quarter notes, beats per minute)
    tempos: list[tuple[float, float]] = field(default_factory=list)


@dataclass
class Page:
    png: bytes
    aspect: float  # height / width
    # Where each bar sits on the page, as fractions of its width and height:
    # bar id -> (left, top, right, bottom) of the bar's staves
    boxes: dict[str, tuple[float, float, float, float]] = field(default_factory=dict)
    # bar id -> (top line, bottom line) of the bar's first staff
    staves: dict[str, tuple[float, float]] = field(default_factory=dict)
    # chord symbol (MEI harm id) -> its left edge, where its note is
    chords_x: dict[str, float] = field(default_factory=dict)


def _base_id(xml_id: str) -> str:
    """Verovio names the copies of repeated bars and notes '<id>-rend2'."""
    return re.sub(r"-rend\d+$", "", xml_id)


def _timeline(timemap: list[dict]) -> list[tuple[str, float]]:
    """Bar starts from Verovio's timemap, in playing order."""
    return [(_base_id(e["measureOn"]), float(e.get("qstamp", 0)))
            for e in timemap if e.get("measureOn") is not None]


def _melody(timemap: list[dict], mei_root: ET.Element,
            pitch_of: Callable[[str], int]) -> list[tuple[float, float, int]]:
    """The first staff's notes from the timemap.

    Built from the timemap rather than Verovio's MIDI export, which leaves
    slash/beat-repeat bars silent (the export voices them) and can't run out of
    memory on some files (the export can).
    """
    staff1 = {
        n.get(XML_ID)
        for st in mei_root.iter(f"{MEI}staff") if st.get("n") == "1"
        for n in st.iter(f"{MEI}note")
    }
    tie_ends = {t.get("endid", "").lstrip("#") for t in mei_root.iter(f"{MEI}tie")}
    started: dict[str, float] = {}
    notes: list[tuple[float, float, int, str]] = []
    for entry in timemap:
        q = float(entry.get("qstamp", 0))
        for nid in entry.get("off", []):
            if nid in started:
                notes.append((started.pop(nid), q, pitch_of(_base_id(nid)), _base_id(nid)))
        for nid in entry.get("on", []):
            if _base_id(nid) in staff1:
                started[nid] = q
    notes.sort()
    merged: list[tuple[float, float, int]] = []
    for start, end, pitch, nid in notes:
        if nid in tie_ends:  # a tied continuation: lengthen the note it hangs from
            for i in range(len(merged) - 1, -1, -1):
                s, e, p = merged[i]
                if p == pitch and abs(e - start) < 1e-6:
                    merged[i] = (s, end, p)
                    break
            else:
                merged.append((start, end, pitch))
        else:
            merged.append((start, end, pitch))
    return merged


_LINE_RE = re.compile(r"M\s*(-?[\d.]+)\s+(-?[\d.]+)\s*L\s*(-?[\d.]+)\s+(-?[\d.]+)")


Box = tuple[float, float, float, float]


def page_layout(svg: str) -> tuple[dict[str, Box], dict[str, tuple[float, float]], dict[str, float]]:
    """Where things are on a Verovio SVG page, as fractions of its width and height:
    each bar's staff area (left, top, right, bottom), each bar's first staff (top
    line, bottom line), and the left edge of each chord symbol."""
    root = ET.fromstring(svg)
    inner = next((e for e in root.iter(f"{SVG}svg") if "definition-scale" in e.get("class", "")), None)
    if inner is None:
        return {}, {}, {}
    _, _, vw, vh = (float(v) for v in inner.get("viewBox", "0 0 1 1").split())
    tx = ty = 0.0
    margin = next((e for e in root.iter(f"{SVG}g") if e.get("class") == "page-margin"), None)
    if margin is not None and (m := re.search(r"translate\(([-\d.]+),\s*([-\d.]+)\)",
                                              margin.get("transform", ""))):
        tx, ty = float(m.group(1)), float(m.group(2))
    boxes: dict[str, Box] = {}
    staves: dict[str, tuple[float, float]] = {}
    chords_x: dict[str, float] = {}
    for g in root.iter(f"{SVG}g"):
        if "measure" not in g.get("class", "").split():
            continue
        xs, ys = [], []
        for n, staff in enumerate(e for e in g.iter(f"{SVG}g") if "staff" in e.get("class", "").split()):
            lines = []
            for path in staff.findall(f"{SVG}path"):
                if m := _LINE_RE.match(path.get("d", "")):
                    x1, y1, x2, _ = (float(v) for v in m.groups())
                    xs += [x1, x2]
                    lines.append(y1)
            ys += lines
            if n == 0 and lines:
                staves[g.get("id", "")] = ((min(lines) + ty) / vh, (max(lines) + ty) / vh)
        if xs:
            boxes[g.get("id", "")] = ((min(xs) + tx) / vw, (min(ys) + ty) / vh,
                                      (max(xs) + tx) / vw, (max(ys) + ty) / vh)
        for harm in g.iter(f"{SVG}g"):
            if "harm" in harm.get("class", "").split() and (text := harm.find(f"{SVG}text")) is not None:
                chords_x[harm.get("id", "")] = (float(text.get("x", 0)) + tx) / vw
    return boxes, staves, chords_x


class Engraver:
    """One Verovio toolkit, guarded by a lock so renders can run off the UI thread."""

    PAGE_WIDTH = 2100  # MEI units: A4 proportions at Verovio's default

    def __init__(self) -> None:
        self._tk = verovio.toolkit()
        verovio.enableLog(verovio.LOG_OFF)
        self._lock = threading.Lock()

    def load(self, musicxml: str, *, transpose: int = 0, zoom: int = 45,
             page_width: int = PAGE_WIDTH, chord_style: str = "written") -> Engraving:
        """Engrave a score. A narrower page_width puts fewer bars on each line;
        chord_style is a CHORD_STYLES key."""
        musicxml = fix_harmony_offsets(musicxml)
        if chord_style == "compact":
            musicxml = compact_chords(musicxml)
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
            if not self._tk.loadData(musicxml):
                raise ValueError("Verovio could not read this MusicXML file")
            mei = self._tk.getMEI()
            timemap = self._tk.renderToTimemap({"includeMeasures": True})
            tempos = [(float(e.get("qstamp", 0)), float(e["tempo"])) for e in timemap if "tempo" in e]
            melody = _melody(timemap, ET.fromstring(mei),
                             lambda nid: int(self._tk.getMIDIValuesForElement(nid)["pitch"]))
            return Engraving(
                pages=self._tk.getPageCount(),
                mei=mei,
                timeline=_timeline(timemap),
                melody=melody,
                tempos=tempos,
            )

    def render_page(self, page: int, *, width_px: int = 1600, ink: str = "#000000") -> Page:
        """One page (1-based) of the loaded score: PNG on a transparent background,
        plus where each bar, its first staff and each chord symbol are on it."""
        with self._lock:
            svg = self._tk.renderToSVG(page)
        boxes, staves, chords_x = page_layout(svg)
        svg = _CSYM_RE.sub(_fix_csym, svg)
        # Glyphs and lines use currentColor; text has no fill and would stay black.
        svg = svg.replace('color="black"', f'color="{ink}" fill="{ink}"', 1)
        fonts = _fonts()
        png = bytes(
            resvg_py.svg_to_bytes(
                svg_string=svg,
                width=width_px,
                skip_system_fonts=bool(fonts),
                font_files=fonts or None,
                font_family="DejaVu Serif",
                serif_family="DejaVu Serif",
            )
        )
        # PNG header: width and height are big-endian u32s at bytes 16-24
        w, h = int.from_bytes(png[16:20], "big"), int.from_bytes(png[20:24], "big")
        return Page(png, h / w if w else 1.4, boxes, staves, chords_x)


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
