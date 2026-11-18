import pytest

from chordview.app import transpose_key
from chordview.engraver import (
    Engraver,
    chart_from_mei,
    fix_harmony_offsets,
    normalize_shift,
    split_chord,
    transpose_option,
)


@pytest.fixture(scope="module")
def engraver():
    return Engraver()


def chart(engraver, xml, transpose=0):
    return chart_from_mei(engraver.load(xml, transpose=transpose).mei)


def test_chart_reads_chords_beats_and_structure(engraver, mini_xml):
    bars = chart(engraver, mini_xml)
    assert [b.number for b in bars] == ["1", "2", "3", "4"]
    assert [(c.text, c.beat) for c in bars[0].chords] == [("Fmaj7", 1.0), ("Dmin7", 3.0)]
    # The C7/E written "after the rest, offset -1 beat" belongs on beat 4.
    assert [(c.text, c.beat) for c in bars[1].chords] == [("B♭7", 1.0), ("C7/E", 4.0)]
    assert bars[0].repeat_start
    assert bars[2].repeat_end and bars[2].ending == "1"
    assert bars[3].ending == "2"


def test_transposition_applies_to_chords(engraver, mini_xml):
    bars = chart(engraver, mini_xml, transpose=2)  # F major -> G major
    assert [c.text for c in bars[0].chords] == ["Gmaj7", "Emin7"]
    assert [c.text for c in bars[1].chords] == ["C7", "D7/F♯"]


def test_render_page_returns_png(engraver, mini_xml):
    engraver.load(mini_xml)
    png = engraver.render_page(1, width_px=400, ink="#ffffff")
    assert png.startswith(b"\x89PNG")


def test_load_rejects_garbage(engraver):
    with pytest.raises(ValueError):
        engraver.load("this is not music")


def test_fix_harmony_offsets_moves_chord_before_its_note(mini_xml):
    fixed = fix_harmony_offsets(mini_xml)
    assert "<offset>-" not in fixed
    assert "<offset>72</offset>" in fixed
    assert fix_harmony_offsets("<score-partwise/>") == "<score-partwise/>"


@pytest.mark.parametrize(
    "text, parts",
    [("B♭maj7/D", ("B♭", "maj7", "D")), ("F♯min11", ("F♯", "min11", "")), ("C", ("C", "", "")),
     ("N.C.", ("N.C.", "", ""))],
)
def test_split_chord(text, parts):
    assert split_chord(text) == parts


@pytest.mark.parametrize("given, folded", [(0, 0), (2, 2), (7, -5), (-3, -3), (9, -3), (6, 6), (-6, 6)])
def test_normalize_shift(given, folded):
    assert normalize_shift(given) == folded


def test_transpose_option():
    assert transpose_option(0) == ""
    assert transpose_option(3) == "+3"
    assert transpose_option(-2) == "-2"


@pytest.mark.parametrize(
    "key, shift, expected",
    [("G", 2, "A"), ("B♭", 2, "C"), ("Em", 3, "Gm"), ("E♭", -3, "C"), ("F♯m", 1, "Gm"), ("", 2, "")],
)
def test_transpose_key(key, shift, expected):
    assert transpose_key(key, shift) == expected
