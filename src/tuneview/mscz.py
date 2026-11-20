"""Convert MuseScore 3/4 .mscz (and .mscx) scores to MusicXML.

MuseScore's native file format is a ZIP holding an .mscx XML document that is
not related to MusicXML.  TuneView engraves with Verovio, so importing a .mscz
means converting the parts of the MuseScore score a leadsheet needs: notes and
rests (with durations, dots and tuplets), chord symbols, lyrics, key and time
signatures, tempo and the common repeat marks.  MuseScore 1/2 scores use an
older format and are rejected with a clear message instead of being guessed at.
"""

from __future__ import annotations

import io
import zipfile
from fractions import Fraction
import xml.etree.ElementTree as ET

# MuseScore tonal pitch classes: tpc -> (letter, alteration).
TPC = {
    7: ("C", -1), 8: ("G", -1), 9: ("D", -1), 10: ("A", -1), 11: ("E", -1),
    12: ("B", -1), 13: ("F", 0), 14: ("C", 0), 15: ("G", 0), 16: ("D", 0),
    17: ("A", 0), 18: ("E", 0), 19: ("B", 0), 20: ("F", 1), 21: ("C", 1),
    22: ("G", 1), 23: ("D", 1), 24: ("A", 1), 25: ("E", 1), 26: ("B", 1),
}
SEMITONE = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}
SHARP_NAMES = [("C", 0), ("C", 1), ("D", 0), ("D", 1), ("E", 0), ("F", 0),
               ("F", 1), ("G", 0), ("G", 1), ("A", 0), ("A", 1), ("B", 0)]
# Quarters per durationType.  Tuplet ratios are applied on top of these.
QUARTERS = {
    "long": Fraction(16), "breve": Fraction(8), "whole": Fraction(4),
    "half": Fraction(2), "quarter": Fraction(1), "eighth": Fraction(1, 2),
    "16th": Fraction(1, 4), "32nd": Fraction(1, 8), "64th": Fraction(1, 16),
    "128th": Fraction(1, 32), "256th": Fraction(1, 64), "512th": Fraction(1, 128),
    "1024th": Fraction(1, 256),
}
# MuseScore chord-symbol quality -> MusicXML kind.
KINDS = {
    "": "major", "maj": "major", "M": "major",
    "m": "minor", "min": "minor",
    "7": "dominant", "dom7": "dominant",
    "maj7": "major-seventh", "M7": "major-seventh", "Δ7": "major-seventh",
    "m7": "minor-seventh", "min7": "minor-seventh",
    "dim": "diminished", "o": "diminished",
    "dim7": "diminished-seventh",
    "aug": "augmented", "+": "augmented",
    "6": "major-sixth", "m6": "minor-sixth",
    "9": "dominant-ninth", "maj9": "major-ninth", "m9": "minor-ninth",
    "11": "dominant-11th", "m11": "minor-11th",
    "13": "dominant-13th",
    "sus2": "suspended-second", "sus4": "suspended-fourth",
    "9sus": "suspended-fourth", "7sus4": "suspended-fourth",
    "m7b5": "half-diminished", "ø7": "half-diminished",
}
CLEFS = {"G": ("G", 2), "F": ("F", 4), "C": ("C", 3), "percussion": ("percussion", 2)}


class _State:
    """Time signature and key carried from measure to measure for one staff."""

    def __init__(self) -> None:
        self.beats, self.beat_type = 4, 4
        self.fifths = 0
        self.clef = ("G", 2)


def _text(el: ET.Element | None, path: str = "") -> str:
    if el is None:
        return ""
    found = el.find(path) if path else el
    return " ".join((found.text or "").split()) if found is not None else ""


def unpack_mscz(data: bytes) -> bytes:
    """The .mscx XML inside a MuseScore .mscz archive."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            names = z.namelist()
            name = next((n for n in names if n.lower().endswith(".mscx")), None)
            if name is None:  # some writers put the score in a container manifest
                try:
                    container = ET.fromstring(z.read("META-INF/container.xml"))
                except (KeyError, ET.ParseError):
                    container = None
                if container is not None:
                    name = next((el.get("full-path") for el in container.iter()
                                 if el.get("full-path", "").lower().endswith(".mscx")), None)
            if name is None:
                raise ValueError("no MuseScore score inside this .mscz file")
            return z.read(name)
    except zipfile.BadZipFile as exc:
        raise ValueError("not a valid .mscz file") from exc


def _note_name(pitch: int, tpc: int | None) -> tuple[str, int, int]:
    """(step, alteration, octave) for a MIDI pitch, spelled by its tpc when present."""
    if tpc in TPC:
        step, alter = TPC[tpc]
    else:
        step, alter = SHARP_NAMES[pitch % 12]
    semis = SEMITONE[step] + alter
    octave = (pitch - semis) // 12 - 1
    return step, alter, octave


def _tpc_note(tpc: int | None) -> tuple[str, int] | None:
    if tpc in TPC:
        return TPC[tpc]
    return None


def _add(parent: ET.Element, tag: str, text: str | None = None, **attrib: str) -> ET.Element:
    el = ET.SubElement(parent, tag, attrib)
    if text is not None:
        el.text = text
    return el


def _fraction(text: str) -> Fraction | None:
    try:
        return Fraction(text.strip())
    except (ValueError, ZeroDivisionError, AttributeError):
        return None


def _voices(measure: ET.Element) -> list[ET.Element]:
    voices = measure.findall("voice")
    return voices or [measure]


def _scan_measure(measure: ET.Element, state: _State) -> None:
    """Pick up a time or key signature placed anywhere in the measure's voices."""
    for voice in _voices(measure):
        for el in voice:
            tag = el.tag
            if tag == "TimeSig":
                try:
                    beats = int(el.findtext("sigN") or 4)
                    beat_type = int(el.findtext("sigD") or 4)
                except ValueError:
                    continue
                if beats > 0 and beat_type > 0:
                    state.beats, state.beat_type = beats, beat_type
            elif tag == "KeySig":
                try:
                    state.fifths = int(el.findtext("concertKey") or 0)
                except ValueError:
                    pass


def _measure_ticks(measure: ET.Element, state: _State, division: int) -> int:
    length = measure.get("len")
    if length:
        parts = _fraction(length)
        if parts is not None:
            return max(1, int(parts * 4 * division))
    return max(1, int(Fraction(state.beats * 4, state.beat_type) * division))


def _duration(chord: ET.Element, division: int,
              tuplet: tuple[int, int] | None) -> tuple[int, str, int] | None:
    """(ticks, MusicXML type, dots) for a timed element, or None for a measure rest."""
    duration_type = _text(chord, "durationType")
    if duration_type == "measure":
        return None
    quarters = QUARTERS.get(duration_type, Fraction(1))
    dots = 0
    try:
        dots = int(chord.findtext("dots") or 0)
    except ValueError:
        pass
    quarters *= Fraction(2 ** (dots + 1) - 1, 2 ** dots)
    if tuplet is not None:
        quarters *= Fraction(*tuplet)
    return max(1, int(quarters * division)), duration_type, dots


def _pitch(note: ET.Element) -> tuple[str, int, int] | None:
    raw = note.findtext("pitch")
    if raw is None:
        return None
    try:
        pitch = int(raw)
    except ValueError:
        return None
    try:
        tpc = int(note.findtext("tpc")) if note.findtext("tpc") else None
    except ValueError:
        tpc = None
    return _note_name(pitch, tpc)


def _tie(note: ET.Element) -> tuple[bool, bool]:
    start = stop = False
    for spanner in note.findall("Spanner"):
        if spanner.get("type") != "Tie":
            continue
        start = start or spanner.find("next") is not None
        stop = stop or spanner.find("prev") is not None
    return start, stop


def _lyrics(chord: ET.Element) -> list[tuple[str, str, str]]:
    out = []
    for lyr in chord.findall("Lyrics"):
        text = _text(lyr, "text")
        if not text:
            continue
        number = _text(lyr, "no") or "0"
        try:
            number = str(int(number) + 1)
        except ValueError:
            number = "1"
        syllabic = _text(lyr, "syllabic") or "single"
        out.append((number, syllabic, text))
    return out


def _add_note(measure: ET.Element, pitch: tuple[str, int, int], ticks: int,
              note_type: str, dots: int, voice: int, *, chord: bool = False,
              grace: bool = False, tie_start: bool = False, tie_stop: bool = False,
              tuplet: tuple[int, int] | None = None,
              lyric: tuple[str, str, str] | None = None) -> None:
    note = _add(measure, "note")
    if grace:
        _add(note, "grace")
    if chord:
        _add(note, "chord")
    step, alter, octave = pitch
    p = _add(note, "pitch")
    _add(p, "step", step)
    if alter:
        _add(p, "alter", str(alter))
    _add(p, "octave", str(octave))
    if not grace:
        _add(note, "duration", str(ticks))
    if tie_start:
        _add(note, "tie", type="start")
    if tie_stop:
        _add(note, "tie", type="stop")
    _add(note, "voice", str(voice))
    if note_type:
        _add(note, "type", note_type)
    for _ in range(dots):
        _add(note, "dot")
    if tuplet is not None:
        actual, normal = tuplet
        tm = _add(note, "time-modification")
        _add(tm, "actual-notes", str(actual))
        _add(tm, "normal-notes", str(normal))
    if tie_start or tie_stop:
        notations = _add(note, "notations")
        if tie_start:
            _add(notations, "tied", type="start")
        if tie_stop:
            _add(notations, "tied", type="stop")
    if lyric is not None:
        number, syllabic, text = lyric
        ly = _add(note, "lyric", number=number)
        _add(ly, "syllabic", syllabic)
        _add(ly, "text", text)


def _add_rest(measure: ET.Element, ticks: int, note_type: str, dots: int, voice: int,
              *, measure_rest: bool = False, tuplet: tuple[int, int] | None = None) -> None:
    note = _add(measure, "note")
    rest = _add(note, "rest")
    if measure_rest:
        rest.set("measure", "yes")
    _add(note, "duration", str(ticks))
    _add(note, "voice", str(voice))
    if note_type:
        _add(note, "type", note_type)
    for _ in range(dots):
        _add(note, "dot")
    if tuplet is not None:
        actual, normal = tuplet
        tm = _add(note, "time-modification")
        _add(tm, "actual-notes", str(actual))
        _add(tm, "normal-notes", str(normal))


def _add_harmony(measure: ET.Element, el: ET.Element) -> None:
    info = el.find("harmonyInfo")
    if info is not None:
        root, name, bass = info.findtext("root"), info.findtext("name") or "", info.findtext("bass")
    else:
        root, name = el.findtext("root"), el.findtext("name") or ""
        bass = el.findtext("base")
    try:
        root_note = _tpc_note(int(root)) if root else None
        bass_note = _tpc_note(int(bass)) if bass else None
    except ValueError:
        return
    if root_note is None:
        return
    harmony = _add(measure, "harmony")
    r = _add(harmony, "root")
    _add(r, "root-step", root_note[0])
    if root_note[1]:
        _add(r, "root-alter", str(root_note[1]))
    kind_text = name if name not in KINDS else None
    kind = _add(harmony, "kind", KINDS.get(name, "major"))
    if kind_text:
        kind.set("text", name)
    if bass_note is not None:
        b = _add(harmony, "bass")
        _add(b, "bass-step", bass_note[0])
        if bass_note[1]:
            _add(b, "bass-alter", str(bass_note[1]))


def _add_direction(measure: ET.Element, words: str, *, tempo: int | None = None) -> None:
    direction = _add(measure, "direction", placement="above")
    dt = _add(direction, "direction-type")
    if tempo is not None:
        metronome = _add(dt, "metronome")
        _add(metronome, "beat-unit", "quarter")
        _add(metronome, "per-minute", str(tempo))
        _add(direction, "sound", tempo=str(tempo))
    else:
        _add(dt, "words", words)


def _barline(measure: ET.Element, location: str, style: str,
             repeat: str | None, times: int | None = None) -> None:
    bar = _add(measure, "barline", location=location)
    _add(bar, "bar-style", style)
    if repeat is not None:
        attrib = {"direction": repeat}
        if times and times > 1:
            attrib["times"] = str(times)
        _add(bar, "repeat", **attrib)


def _measure_number(measure: ET.Element, index: int) -> str:
    return measure.get("number") or str(index + 1)


def _staff_clef(part: ET.Element | None, part_staff: ET.Element | None, index: int) -> tuple[str, int]:
    if part_staff is not None:
        default = _text(part_staff, "defaultClef")
        if default in CLEFS:
            return CLEFS[default]
    if part is not None:
        for clef in part.findall("Instrument/clef"):
            if clef.get("staff") == str(index + 1) and (clef.text or "").strip() in CLEFS:
                return CLEFS[(clef.text or "").strip()]
    return CLEFS["G"]


def _parts(score: ET.Element) -> list[tuple[ET.Element, str, tuple[str, int]]]:
    """(top-level Staff element, part name, clef) for each staff in the score."""
    staves = score.findall("Staff")
    infos: list[tuple[ET.Element, str, tuple[str, int]]] = []
    staff_index = 0
    for part in score.findall("Part"):
        name = _text(part, "trackName") or _text(part, "Instrument/longName") or "Music"
        part_staves = part.findall("Staff")
        for k, _part_staff in enumerate(part_staves):
            if staff_index < len(staves):
                infos.append((staves[staff_index], name,
                              _staff_clef(part, _part_staff, k)))
                staff_index += 1
    while staff_index < len(staves):  # a score without usable <Part> metadata
        infos.append((staves[staff_index], "Music", CLEFS["G"]))
        staff_index += 1
    return infos


def _meta(score: ET.Element) -> dict[str, str]:
    meta = {tag.get("name"): _text(tag) for tag in score.findall("metaTag") if tag.get("name")}
    title = meta.get("workTitle") or meta.get("movementTitle") or ""
    composer = meta.get("composer") or ""
    arranger = meta.get("arranger") or ""
    if not title or not composer:
        for text in score.iter("Text"):
            style = (_text(text, "style") + " " + _text(text, "subtype")).lower()
            value = _text(text, "text")
            if not value:
                continue
            if not title and "title" in style:
                title = value
            elif not composer and "composer" in style:
                composer = value
    return {"title": title, "composer": composer, "arranger": arranger,
            "copyright": meta.get("copyright", "")}


def _measure(part_el: ET.Element, measure: ET.Element | None, state: _State,
             division: int, number: str, first: bool,
             clef: tuple[str, int]) -> None:
    """Fill one MusicXML <measure> (part_el) from one MuseScore measure."""
    """One MusicXML measure from one MuseScore measure."""
    _scan_measure(measure, state) if measure is not None else None
    total = _measure_ticks(measure, state, division) if measure is not None \
        else max(1, int(Fraction(state.beats * 4, state.beat_type) * division))

    attrs: ET.Element | None = None
    if first:
        attrs = _add(part_el, "attributes")
        _add(attrs, "divisions", str(division))
    key_els = [el for v in (_voices(measure) if measure is not None else [])
               for el in v.findall("KeySig")]
    time_els = [el for v in (_voices(measure) if measure is not None else [])
                for el in v.findall("TimeSig")]
    if first or key_els:
        if attrs is None:
            attrs = _add(part_el, "attributes")
        key = _add(attrs, "key")
        _add(key, "fifths", str(state.fifths))
        _add(key, "mode", "major")
    if first or time_els:
        if attrs is None:
            attrs = _add(part_el, "attributes")
        time = _add(attrs, "time")
        _add(time, "beats", str(state.beats))
        _add(time, "beat-type", str(state.beat_type))
    if first:
        assert attrs is not None
        clef_el = _add(attrs, "clef")
        _add(clef_el, "sign", clef[0])
        _add(clef_el, "line", str(clef[1]))

    if measure is None:
        _add_rest(part_el, total, "", 0, 1, measure_rest=True)
        return

    # Tempo and text directions sit outside the voices in MusicXML.
    for v in _voices(measure):
        for el in v:
            if el.tag == "Tempo":
                raw = el.findtext("tempo")
                if raw:
                    try:
                        _add_direction(part_el, "", tempo=max(1, round(float(raw) * 60)))
                    except ValueError:
                        pass
                break
        else:
            continue
        break
    for el in measure:
        if el.tag == "Marker":
            words = _text(el, "text") or _text(el, "label")
            if words:
                _add_direction(part_el, words)
        elif el.tag == "Jump":
            words = _text(el, "text")
            if words:
                _add_direction(part_el, words)

    start_repeat = int(measure.findtext("startRepeat") or 0)
    end_repeat = int(measure.findtext("endRepeat") or 0)
    barline_style = ""
    if start_repeat:
        _barline(part_el, "left", "heavy-light", "forward", start_repeat)
    if end_repeat:
        _barline(part_el, "right", "light-heavy", "backward", end_repeat)

    for voice_index, voice in enumerate(_voices(measure), start=1):
        position = 0
        tuplets: list[tuple[int, int]] = []
        for el in voice:
            tag = el.tag
            if tag == "Tuplet":
                try:
                    normal = int(el.findtext("normalNotes") or 2)
                    actual = int(el.findtext("actualNotes") or 3)
                    tuplets.append((actual, normal))
                except ValueError:
                    tuplets.append((3, 2))
                continue
            if tag == "endTuplet":
                if tuplets:
                    tuplets.pop()
                continue
            if tag == "Harmony":
                _add_harmony(part_el, el)
                continue
            if tag == "BarLine":
                barline_style = _text(el, "subtype") or barline_style
                continue
            if tag == "Chord":
                timing = _duration(el, division, tuplets[-1] if tuplets else None)
                grace = el.find("grace") is not None or el.find("appoggiatura") is not None \
                    or el.find("acciaccatura") is not None
                notes = [n for n in el.findall("Note") if _pitch(n) is not None]
                if not notes:
                    continue
                if grace:
                    for i, note in enumerate(notes):
                        pitch = _pitch(note)
                        assert pitch is not None
                        _add_note(part_el, pitch, 0, "", 0, voice_index,
                                  chord=i > 0, grace=True)
                    continue
                if timing is None:
                    continue
                ticks, note_type, dots = timing
                ties = [_tie(n) for n in notes]
                lyrics = _lyrics(el)
                for i, (note, (tie_start, tie_stop)) in enumerate(zip(notes, ties)):
                    pitch = _pitch(note)
                    assert pitch is not None
                    _add_note(part_el, pitch, ticks, note_type, dots, voice_index,
                              chord=i > 0, tie_start=tie_start, tie_stop=tie_stop,
                              tuplet=tuplets[-1] if tuplets else None,
                              lyric=lyrics[i] if i == 0 and lyrics else None)
                position += ticks
                continue
            if tag == "Rest":
                timing = _duration(el, division, tuplets[-1] if tuplets else None)
                if timing is None:
                    _add_rest(part_el, total, "", 0, voice_index, measure_rest=True)
                    position = total
                else:
                    ticks, note_type, dots = timing
                    _add_rest(part_el, ticks, note_type, dots, voice_index,
                              tuplet=tuplets[-1] if tuplets else None)
                    position += ticks
                continue
            if tag == "location":
                fraction = _fraction(_text(el, "fractions"))
                if fraction is not None:  # MuseScore's own time cursor adjustment
                    position += int(fraction * 8 * division)
                continue
        if voice_index < len(_voices(measure)):  # rewind for the next voice
            if position > 0:
                backup = _add(part_el, "backup")
                _add(backup, "duration", str(position))

    if barline_style:
        style = {"end": "light-heavy", "final": "light-heavy", "double": "light-light",
                 "start-repeat": "heavy-light", "end-repeat": "light-heavy"}.get(
            barline_style, "")
        if style:
            _barline(part_el, "right", style, "backward" if barline_style == "end-repeat"
                     else None)


def mscx_to_musicxml(data: bytes) -> bytes:
    """Convert MuseScore 3/4 .mscx XML into MusicXML 4 partwise."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise ValueError("not valid MuseScore XML") from exc
    version = root.get("version", "")
    try:
        major = int(version.split(".")[0])
    except ValueError:
        major = 0
    if major and major < 3:
        raise ValueError(
            f"MuseScore {version} files are not supported; save with MuseScore 3 or 4, "
            "or export MusicXML from MuseScore")
    score = root.find("Score")
    if score is None:
        raise ValueError("no MuseScore <Score> in this .mscx; save with MuseScore 3 or 4")
    try:
        division = int(_text(score, "Division") or _text(root, "Division") or 480)
    except ValueError:
        division = 480
    division = max(1, division)
    meta = _meta(score)
    parts = _parts(score)
    if not parts:
        raise ValueError("no staves in this MuseScore score")

    musicxml = ET.Element("score-partwise", version="4.0")
    if meta["title"]:
        work = _add(musicxml, "work")
        _add(work, "work-title", meta["title"])
    identification = _add(musicxml, "identification")
    if meta["composer"]:
        _add(identification, "creator", meta["composer"], type="composer")
    if meta["arranger"]:
        _add(identification, "creator", meta["arranger"], type="arranger")
    if meta["copyright"]:
        _add(identification, "rights", meta["copyright"])
    part_list = _add(musicxml, "part-list")
    for i, (_, name, _) in enumerate(parts, start=1):
        score_part = _add(part_list, "score-part", id=f"P{i}")
        _add(score_part, "part-name", name)

    for i, (staff, name, clef) in enumerate(parts, start=1):
        part_el = _add(musicxml, "part", id=f"P{i}")
        state = _State()
        measures = staff.findall("Measure")
        count = max((len(s.findall("Measure")) for s, _, _ in parts), default=0)
        if count == 0:
            raise ValueError("this MuseScore score has no measures")
        for mi in range(count):
            measure = measures[mi] if mi < len(measures) else None
            number = _measure_number(measure, mi) if measure is not None else str(mi + 1)
            out = _add(part_el, "measure", number=number)
            _measure(out, measure, state, division, number, mi == 0, clef)

    body = ET.tostring(musicxml, encoding="unicode")
    return ('<?xml version="1.0" encoding="UTF-8"?>\n' + body + "\n").encode("utf-8")


def mscz_to_musicxml(data: bytes) -> bytes:
    """Convert a MuseScore .mscz archive into MusicXML."""
    return mscx_to_musicxml(unpack_mscz(data))
