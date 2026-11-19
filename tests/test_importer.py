import pytest

from tuneview.importer import (
    ImportResult,
    import_files,
    import_folder,
    key_name,
    parse_metadata,
    split_composer,
    unpack_mxl,
)
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


def mxl(score: bytes, container: bool = True, namespaced: bool = False) -> bytes:
    """A compressed MusicXML (.mxl) archive holding score."""
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        if container:
            ns = ' xmlns="urn:oasis:names:tc:opendocument:xmlns:container"' if namespaced else ""
            z.writestr("META-INF/container.xml",
                       f'<container{ns}><rootfiles><rootfile full-path="score/tune.musicxml"/>'
                       "</rootfiles></container>")
        z.writestr("score/tune.musicxml", score)
    return buf.getvalue()


def test_unpack_mxl(mini_path):
    data = mini_path.read_bytes()
    assert unpack_mxl(mxl(data)) == data
    assert unpack_mxl(mxl(data, namespaced=True)) == data
    assert unpack_mxl(mxl(data, container=False)) == data
    with pytest.raises(ValueError):
        unpack_mxl(b"not a zip")


def test_import_files_from_the_app(tmp_path, mini_path):
    data = mini_path.read_bytes()
    lib = Library(tmp_path / "new.sqlite")  # a brand-new file
    assert lib.is_new()
    result = import_files([("Tune.mxl", mxl(data)), ("again.musicxml", data),
                           ("notes.txt.xml", b"<html/>"), ("bad.mxl", b"zip?")], lib)
    assert lib.is_ready() and not lib.is_new()
    [song] = lib.songs()
    assert result.added == [song.id] and result.duplicates == 1  # the .mxl's score, once
    assert [e.split(":")[0] for e in result.errors] == ["notes.txt.xml", "bad.mxl"]
    assert lib.musicxml(song.id) == data.decode()
    assert result.summary() == "Added 1 tune (1 already in the library, 2 couldn't be read)."
    assert ImportResult(added=[1, 2]).summary() == "Added 2 tunes."
    assert ImportResult().summary() == "Added 0 tunes."
