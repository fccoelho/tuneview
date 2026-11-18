import pytest

from tuneview.importer import import_folder, key_name, parse_metadata, split_composer
from tuneview.library import Library


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Benny Golson\nArr. 6EQUJ5", ("Benny Golson", "6EQUJ5")),
        ("Eddie Harris, Arr 6EQUJ5", ("Eddie Harris", "6EQUJ5")),
        ("Arranged by Gil Evans", ("", "Gil Evans")),
        ("Duke Ellington, arr: Billy Strayhorn", ("Duke Ellington", "Billy Strayhorn")),
        ("Arrow Smith", ("Arrow Smith", "")),  # a name, not an arranger credit
        ("", ("", "")),
    ],
)
def test_split_composer(raw, expected):
    assert split_composer(raw) == expected


@pytest.mark.parametrize(
    "fifths, mode, expected",
    [(0, "major", "C"), (-1, None, "F"), (3, "major", "A"), (-3, "minor", "Cm"), (1, "minor", "Em"),
     (9, "major", "C♯")],  # out-of-range fifths are clamped
)
def test_key_name(fifths, mode, expected):
    assert key_name(fifths, mode) == expected


def test_parse_metadata(mini_path):
    meta = parse_metadata(mini_path.read_bytes(), mini_path)
    assert meta == {
        "title": "Test Tune",
        "composer": "Test Composer",
        "arranger": "Someone Else",
        "copyright": "Written for the TuneView test suite",
        "key": "F",
        "time": "4/4",
        "tempo": 120,
        "measures": 4,
        "chords": 6,
        "has_melody": True,
        "has_lyrics": True,
        "parts": 1,
    }


def test_parse_metadata_rejects_timewise(tmp_path):
    path = tmp_path / "t.xml"
    with pytest.raises(ValueError):
        parse_metadata(b"<score-timewise/>", path)


def test_import_folder_dedupes_and_reports_errors(tmp_path, mini_path):
    src = tmp_path / "scores"
    (src / "I").mkdir(parents=True)
    (src / "II").mkdir()
    data = mini_path.read_bytes()
    (src / "I" / "Test Tune.xml").write_bytes(data)
    (src / "II" / "Test Tune.musicxml").write_bytes(data)  # same tune in another volume
    (src / "II" / "broken.xml").write_bytes(b"<score-partwise>")

    lib = Library(tmp_path / "lib.sqlite")
    added, duplicates, errors = import_folder(src, lib, log=lambda *_: None)

    assert (added, duplicates, len(errors)) == (1, 1, 1)
    [song] = lib.songs()
    assert song.collection == "I"
    assert lib.musicxml(song.id) == data.decode()
