"""Scale suggestions for improvising over chord symbols, and where their notes sit
on the staff.

The choice follows common jazz chord-scale practice: the quality of the chord
first (m7 -> Dorian, 7 -> Mixolydian, ø7 -> Locrian…), refined by the key
signature (a m7 on the 3rd of the key is Phrygian, a △7 on the 4th is Lydian) and
by where a dominant resolves (V7 into a minor chord -> Phrygian dominant).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .engraver import ChartBar, split_chord

LETTERS = "CDEFGAB"
NATURAL = [0, 2, 4, 5, 7, 9, 11]
ACCIDENTALS = {-2: "𝄫", -1: "♭", 0: "♮", 1: "♯", 2: "𝄪"}
_ACC_IN_NAME = {"♭": -1, "b": -1, "♯": 1, "#": 1, "𝄪": 2, "𝄫": -2}

# name -> (semitones above the root, letter steps above the root's letter)
HEPTATONIC = list(range(7))
SCALES: dict[str, tuple[list[int], list[int]]] = {
    "Ionian": ([0, 2, 4, 5, 7, 9, 11], HEPTATONIC),
    "Dorian": ([0, 2, 3, 5, 7, 9, 10], HEPTATONIC),
    "Phrygian": ([0, 1, 3, 5, 7, 8, 10], HEPTATONIC),
    "Lydian": ([0, 2, 4, 6, 7, 9, 11], HEPTATONIC),
    "Mixolydian": ([0, 2, 4, 5, 7, 9, 10], HEPTATONIC),
    "Aeolian": ([0, 2, 3, 5, 7, 8, 10], HEPTATONIC),
    "Locrian": ([0, 1, 3, 5, 6, 8, 10], HEPTATONIC),
    "Locrian ♮2": ([0, 2, 3, 5, 6, 8, 10], HEPTATONIC),
    "Melodic minor": ([0, 2, 3, 5, 7, 9, 11], HEPTATONIC),
    "Lydian dominant": ([0, 2, 4, 6, 7, 9, 10], HEPTATONIC),
    "Mixolydian ♭6": ([0, 2, 4, 5, 7, 8, 10], HEPTATONIC),
    "Phrygian dominant": ([0, 1, 4, 5, 7, 8, 10], HEPTATONIC),
    "Lydian augmented": ([0, 2, 4, 6, 8, 9, 11], HEPTATONIC),
    # Spelled as alterations of a dominant: 1 ♭9 ♯9 3 ♯11 ♭13 ♭7
    "Altered": ([0, 1, 3, 4, 6, 8, 10], [0, 1, 1, 2, 3, 5, 6]),
    "Whole tone": ([0, 2, 4, 6, 8, 10], [0, 1, 2, 3, 4, 6]),
    "Half-whole diminished": ([0, 1, 3, 4, 6, 7, 9, 10], [0, 1, 1, 2, 3, 4, 5, 6]),
    "Whole-half diminished": ([0, 2, 3, 5, 6, 8, 9, 11], [0, 1, 2, 3, 4, 5, 5, 6]),
}

# The staff's bottom line for each clef, as a diatonic index (letter + 7 * octave)
CLEF_BOTTOM_LINE = {"G2": 2 + 7 * 4, "F4": 4 + 7 * 2}  # E4, G2


def parse_note(name: str) -> tuple[int, int] | None:
    """'B♭' -> (6, -1): letter index and accidental."""
    if not name or name[0] not in LETTERS:
        return None
    return LETTERS.index(name[0]), sum(_ACC_IN_NAME.get(c, 0) for c in name[1:])


def note_pc(letter: int, acc: int) -> int:
    return (NATURAL[letter] + acc) % 12


# ----------------------------------------------------------------------------
# Choosing a scale
# ----------------------------------------------------------------------------
@dataclass
class _Quality:
    minor: bool
    dim: bool
    half_dim: bool
    aug: bool
    sus: bool
    major7: bool
    dominant: bool
    q: str
    nums: list[int]


def _quality(quality: str) -> _Quality:
    q = quality.replace(" ", "").replace("(", "").replace(")", "").replace(",", "")
    q = q.replace("#", "♯").replace("b5", "♭5").replace("b9", "♭9").replace("b13", "♭13").replace("b6", "♭6")
    lower = q.lower()
    minor = bool(re.match(r"(min|mi|m(?!a)|-)", q)) and not lower.startswith("maj")
    dim = lower.startswith("dim") or q.startswith(("°", "o"))
    half_dim = "ø" in q or (minor and "7♭5" in q)
    aug = lower.startswith(("aug", "+"))
    sus = "sus" in lower
    major7 = bool(re.search(r"maj|ma7|ma9|ma13|Δ|△|^M(7|9|13)", q))
    nums = [int(n) for n in re.findall(r"(?<![♭♯\d])(\d+)", q)]
    dominant = not (minor or dim or half_dim or major7) and any(n in (7, 9, 11, 13) for n in nums)
    return _Quality(minor, dim, half_dim, aug, sus, major7, dominant, q, nums)


def choose_scale(chord: str, key_sig: int = 0, next_chord: str | None = None) -> str | None:
    """The scale (a SCALES key) to suggest over a chord symbol such as 'Dm7' or
    'G7(♭9)'. key_sig counts sharps (+) or flats (-); next_chord is the chord that
    follows, which decides how a dominant resolves. None for N.C. and power chords."""
    root_name, quality, _ = split_chord(chord)
    root = parse_note(root_name)
    if root is None or quality.strip() == "5":
        return None
    c = _quality(quality)
    q = c.q
    # Scale degree of the root in the major key of the key signature
    degree = (note_pc(*root) - key_sig * 7) % 12

    if c.half_dim:
        return "Locrian ♮2" if 9 in c.nums else "Locrian"
    if c.dim:
        return "Whole-half diminished"
    if c.minor and c.major7:
        return "Melodic minor"
    if c.minor:
        if "♭6" in q or "♭13" in q:
            return "Aeolian"
        if "♭9" in q:
            return "Phrygian"
        if 6 in c.nums:
            return "Dorian"
        return {4: "Phrygian", 9: "Aeolian"}.get(degree, "Dorian")
    if c.aug:
        return "Lydian augmented" if c.major7 else "Whole tone"
    if c.major7:
        return "Lydian" if "♯11" in q or degree == 5 else "Ionian"  # △7 on IV: Lydian
    if c.sus:
        return "Phrygian" if "♭9" in q else "Mixolydian"
    if c.dominant:
        flat9, sharp9 = "♭9" in q, "♯9" in q
        if "alt" in q.lower() or ((flat9 or sharp9) and ("♭13" in q or "♯5" in q)):
            return "Altered"
        if flat9 or sharp9:
            return "Half-whole diminished"
        if "♯5" in q:
            return "Whole tone"
        if "♭13" in q:
            return "Mixolydian ♭6"
        if "♯11" in q or "♭5" in q:
            return "Lydian dominant"
        if next_chord and _resolves_to_minor(note_pc(*root), next_chord):
            return "Phrygian dominant"
        return "Mixolydian"
    # Major triads and sixth chords
    return "Lydian" if "♯11" in q or degree == 5 else "Ionian"


def _resolves_to_minor(root_pc: int, next_chord: str) -> bool:
    """A dominant going down a fifth into a minor or half-diminished chord."""
    root_name, quality, _ = split_chord(next_chord)
    target = parse_note(root_name)
    if target is None or (root_pc - note_pc(*target)) % 12 != 7:
        return False
    c = _quality(quality)
    return c.minor or c.half_dim


# ----------------------------------------------------------------------------
# Spelling and placing a scale
# ----------------------------------------------------------------------------
def spell_scale(root_name: str, scale: str) -> list[tuple[int, int]]:
    """The scale's notes as (letter index, accidental), ascending from the root.

    A root that would need double sharps or flats (G♭ Locrian) is respelled
    enharmonically (F♯ Locrian)."""
    root = parse_note(root_name)
    if root is None:
        return []
    intervals, steps = SCALES[scale]
    pc = note_pc(*root)

    def spell(letter: int) -> list[tuple[int, int]]:
        out = []
        for iv, st in zip(intervals, steps, strict=True):
            lt = (letter + st) % 7
            out.append((lt, (pc + iv - NATURAL[lt] + 6) % 12 - 6))
        return out if all(abs(a) <= 2 for _, a in out) else []

    # The written root's letter, or a neighbour letter one sharp or flat away
    candidates = [root[0]] + [lt for lt in range(7)
                              if lt != root[0] and abs((pc - NATURAL[lt] + 6) % 12 - 6) <= 1]
    spellings = [s for s in (spell(lt) for lt in candidates) if s]
    if not spellings:
        return []
    # Fewest double accidentals, then fewest accidentals; the written root wins ties.
    return min(spellings, key=lambda s: (sum(abs(a) == 2 for _, a in s), sum(abs(a) for _, a in s)))


def key_accidental(letter: int, key_sig: int) -> int:
    if key_sig > 0:
        return 1 if LETTERS[letter] in "FCGDAEB"[:key_sig] else 0
    if key_sig < 0:
        return -1 if LETTERS[letter] in "BEADGCF"[:-key_sig] else 0
    return 0


def staff_positions(notes: list[tuple[int, int]], clef: str, key_sig: int = 0) -> list[tuple[int, str]]:
    """Where each note goes on the staff: (diatonic steps above the bottom line,
    accidental to print or ''). The root lands between the ledger line below the
    staff and the middle line, so the scale climbs through the staff."""
    if clef not in CLEF_BOTTOM_LINE or not notes:
        return []
    base = CLEF_BOTTOM_LINE[clef]
    root_step = (notes[0][0] - base + 2) % 7 - 2
    root_letter = notes[0][0]
    # 8-note scales use one letter twice (D♭ and D♯): both sit on the same step.
    return [(root_step + (letter - root_letter) % 7,
             "" if acc == key_accidental(letter, key_sig) else ACCIDENTALS[acc])
            for letter, acc in notes]


@dataclass
class ScaleGuide:
    root: str
    scale: str
    positions: list[tuple[int, str]]  # see staff_positions

    @property
    def name(self) -> str:
        return f"{self.root} {self.scale}"


def scale_guide(chord: str, clef: str, key_sig: int = 0, next_chord: str | None = None) -> ScaleGuide | None:
    """Everything needed to draw the suggested scale for a chord, or None."""
    scale = choose_scale(chord, key_sig, next_chord)
    if scale is None:
        return None
    notes = spell_scale(split_chord(chord)[0], scale)
    if not notes:
        return None
    letter, acc = notes[0]  # the root as spelled, which may be respelled (G♭ -> F♯)
    root = LETTERS[letter] + (ACCIDENTALS[acc] if acc else "")
    return ScaleGuide(root, scale, staff_positions(notes, clef, key_sig))


def scale_guides(bars: list[ChartBar]) -> dict[str, ScaleGuide]:
    """A guide for every chord in the chart, keyed by the chord's id."""
    chords = [(c, b) for b in bars for c in b.chords]
    out = {}
    for i, (chord, bar) in enumerate(chords):
        nxt = chords[i + 1][0].text if i + 1 < len(chords) else None
        if chord.id and (g := scale_guide(chord.text, bar.clef, bar.key_sig, nxt)):
            out[chord.id] = g
    return out


# ----------------------------------------------------------------------------
# Drawing a bar's scales on the page
# ----------------------------------------------------------------------------
@dataclass
class Mark:
    """One thing to draw, in pixels on the page image."""

    kind: str  # "note", "root" (the scale's root), "accidental" or "ledger"
    x: float
    y: float  # top edge
    width: float
    height: float
    text: str = ""


def scale_marks(bar: ChartBar, guides: dict[str, ScaleGuide], staff: tuple[float, float],
                right: float, chords_x: dict[str, float], width: float, height: float) -> list[Mark]:
    """The bar's suggested scales as noteheads climbing from each chord symbol
    to the next one. staff is the first staff's (top, bottom) line, right the
    bar's right edge and chords_x each chord's left edge, all as fractions of
    the page; width and height are the page's size in pixels."""
    top, bottom = staff[0] * height, staff[1] * height
    space = (bottom - top) / 4  # between two staff lines
    if space <= 0:
        return []
    placed = [(chords_x[c.id] * width, guides[c.id]) for c in bar.chords
              if c.id in guides and c.id in chords_x]
    marks: list[Mark] = []
    for i, (x0, guide) in enumerate(placed):
        x1 = placed[i + 1][0] if i + 1 < len(placed) else right * width
        n = len(guide.positions)
        step_x = min(space * 2.4, max(space * 0.8, (x1 - x0 - space * 1.6) / max(n - 1, 1)))
        for j, (step, accidental) in enumerate(guide.positions):
            x = x0 + j * step_x
            y = bottom - step * space / 2  # centre of the notehead
            for ledger in [*range(-2, step - 1, -2), *range(10, step + 1, 2)]:
                ly = bottom - ledger * space / 2
                marks.append(Mark("ledger", x - space * 0.35, ly - 0.75, space * 2, 1.5))
            marks.append(Mark("root" if j == 0 else "note", x, y - space * 0.5, space * 1.3, space))
            if accidental:
                # Printed small above the notehead, as editorial accidentals are: there is
                # no room beside it. A text glyph: height is its font size, and its body
                # sits ~0.97 em below its top.
                marks.append(Mark("accidental", x + space * 0.3, y - space * 2.65, space * 0.8,
                                  space * 1.6, accidental))
    return marks
