from itertools import pairwise

import pytest

from tuneview.engraver import ChartBar, ChartChord, Engraver, chart_from_mei
from tuneview.scales import (
    choose_scale,
    scale_guide,
    scale_guides,
    scale_marks,
    spell_scale,
    staff_positions,
)


@pytest.mark.parametrize(
    "chord, key_sig, next_chord, scale",
    [
        ("Dm7", 0, None, "Dorian"),
        ("Dmin7", 0, None, "Dorian"),
        ("Em7", 0, None, "Phrygian"),  # iii of C
        ("Am7", 0, None, "Aeolian"),  # vi of C
        ("Am7", -1, None, "Phrygian"),  # iii of F
        ("Am7", 1, None, "Dorian"),  # ii of G
        ("Cm6", 0, None, "Dorian"),
        ("Cm(△7)", 0, None, "Melodic minor"),
        ("C△7", 0, None, "Ionian"),
        ("Cmaj7", 0, None, "Ionian"),
        ("F△7", 0, None, "Lydian"),  # IV of C
        ("C△7(♯11)", 0, None, "Lydian"),
        ("C6", 0, None, "Ionian"),
        ("C", 0, None, "Ionian"),
        ("G7", 0, "C△7", "Mixolydian"),
        ("G7", 0, "Cm7", "Phrygian dominant"),  # V7 into a minor chord
        ("G7", 0, "Cø7", "Phrygian dominant"),
        ("G7(♭9)", 0, None, "Half-whole diminished"),
        ("G7(♯9add♭13)", 0, None, "Altered"),
        ("G7alt", 0, None, "Altered"),
        ("G7(♯5)", 0, None, "Whole tone"),
        ("G+7", 0, None, "Whole tone"),
        ("G7(add♯11)", 0, None, "Lydian dominant"),
        ("G7sus", 0, None, "Mixolydian"),
        ("G13", 0, None, "Mixolydian"),
        ("Bø7", 0, None, "Locrian"),
        ("Bmin7(♭5)", 0, None, "Locrian"),
        ("B°7", 0, None, "Whole-half diminished"),
        ("Bdim", 0, None, "Whole-half diminished"),
        ("N.C.", 0, None, None),
        ("C5", 0, None, None),
    ],
)
def test_choose_scale(chord, key_sig, next_chord, scale):
    assert choose_scale(chord, key_sig, next_chord) == scale


def names(notes):
    return [("CDEFGAB"[letter] + {-2: "𝄫", -1: "♭", 0: "", 1: "♯", 2: "𝄪"}[acc]) for letter, acc in notes]


def test_spell_scale():
    assert names(spell_scale("D", "Dorian")) == ["D", "E", "F", "G", "A", "B", "C"]
    assert names(spell_scale("B♭", "Mixolydian")) == ["B♭", "C", "D", "E♭", "F", "G", "A♭"]
    assert names(spell_scale("G", "Altered")) == ["G", "A♭", "A♯", "B", "C♯", "E♭", "F"]
    assert names(spell_scale("G", "Half-whole diminished")) == ["G", "A♭", "A♯", "B", "C♯", "D", "E", "F"]
    assert names(spell_scale("C", "Whole tone")) == ["C", "D", "E", "F♯", "G♯", "B♭"]
    # G♭ Locrian would need five double flats: spelled as F♯ Locrian instead
    assert names(spell_scale("G♭", "Locrian")) == ["F♯", "G", "A", "B", "C", "D", "E"]


def test_staff_positions_treble_and_bass():
    d_dorian = spell_scale("D", "Dorian")
    # Treble clef: D4 sits just under the bottom line (E4 = step 0)
    assert staff_positions(d_dorian, "G2") == [(s, "") for s in range(-1, 6)]
    # Bass clef: bottom line G2, so D3 is step 4
    assert staff_positions(d_dorian, "F4")[0][0] == 4
    assert staff_positions(d_dorian, "") == []


def test_staff_positions_follow_the_key_signature():
    b_flat = spell_scale("B♭", "Mixolydian")  # B♭ C D E♭ F G A♭
    assert [a for _, a in staff_positions(b_flat, "G2", 0)] == ["♭", "", "", "♭", "", "", "♭"]
    # In B♭ major (2 flats) only the A♭ needs an accidental
    assert [a for _, a in staff_positions(b_flat, "G2", -2)] == ["", "", "", "", "", "", "♭"]
    # In D major (2 sharps) the naturals need showing
    c_ionian = spell_scale("C", "Ionian")
    assert [a for _, a in staff_positions(c_ionian, "G2", 2)] == ["♮", "", "", "♮", "", "", ""]


def test_eight_note_scale_repeats_a_step():
    steps = [s for s, _ in staff_positions(spell_scale("G", "Half-whole diminished"), "G2")]
    assert steps == [2, 3, 3, 4, 5, 6, 7, 8]  # A♭ and A♯ share a line


def test_scale_guide_names_the_spelled_root():
    guide = scale_guide("G♭ø7", "G2")
    assert guide.name == "F♯ Locrian"
    assert scale_guide("N.C.", "G2") is None


def test_scale_guides_look_ahead_across_bars():
    bars = [
        ChartBar("1", "b1", [ChartChord("Dø7", 1, "h1"), ChartChord("G7", 3, "h2")]),
        ChartBar("2", "b2", [ChartChord("Cm7", 1, "h3")], key_sig=-3),
    ]
    guides = scale_guides(bars)
    assert guides["h1"].scale == "Locrian"
    assert guides["h2"].scale == "Phrygian dominant"
    assert guides["h3"].scale == "Aeolian"  # i of C minor: vi of E♭ major


def test_scale_marks_climb_from_each_chord_symbol():
    bar = ChartBar("1", "b1", [ChartChord("Dm7", 1, "h1"), ChartChord("G7", 3, "h2")])
    guides = scale_guides([bar])
    # A 1000 x 1000 page: staff lines 10 px apart from y=400 to y=440
    marks = scale_marks(bar, guides, (0.4, 0.44), 0.6, {"h1": 0.1, "h2": 0.35}, 1000, 1000)
    heads = [m for m in marks if m.kind in ("note", "root")]
    assert len(heads) == 14 and [m.kind for m in heads].count("root") == 2
    first = heads[:7]
    assert first[0].x == pytest.approx(100) and all(a.x < b.x for a, b in pairwise(first))
    # D4 is centred half a space under the bottom line; each note climbs a step
    assert first[0].y + first[0].height / 2 == pytest.approx(445)
    assert first[1].y == pytest.approx(first[0].y - 5)
    assert all(m.x < 350 for m in first)  # Dorian stays clear of the G7's scale
    assert heads[7].x == pytest.approx(350)
    assert scale_marks(bar, guides, (0.4, 0.44), 0.6, {}, 1000, 1000) == []


def test_ledger_lines_below_the_staff():
    bar = ChartBar("1", "b1", [ChartChord("C△7", 1, "h1")])
    marks = scale_marks(bar, scale_guides([bar]), (0.4, 0.44), 0.6, {"h1": 0.1}, 1000, 1000)
    ledgers = [m for m in marks if m.kind == "ledger"]
    assert len(ledgers) == 1 and ledgers[0].y + 0.75 == pytest.approx(450)  # middle C


def test_chart_reads_clef_and_key(mini_xml):
    engraver = Engraver()
    bars = chart_from_mei(engraver.load(mini_xml).mei)
    assert all(b.clef == "G2" and b.key_sig == -1 for b in bars)  # F major
    assert chart_from_mei(engraver.load(mini_xml, transpose=2).mei)[0].key_sig == 1  # G major
