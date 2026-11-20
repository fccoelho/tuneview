import io
import zipfile
from pathlib import Path

import pytest

from tuneview.engraver import Engraver, chart_from_mei
from tuneview.importer import find_scores, import_files, parse_metadata
from tuneview.library import Library
from tuneview.mscz import mscx_to_musicxml, mscz_to_musicxml, unpack_mscz

MSCX = '''<?xml version="1.0" encoding="UTF-8"?>
<museScore version="4.20">
  <programVersion>4.2.1</programVersion>
  <Score>
    <Division>480</Division>
    <metaTag name="workTitle">Test Tune</metaTag>
    <metaTag name="composer">Test Composer</metaTag>
    <metaTag name="arranger">Test Arranger</metaTag>
    <metaTag name="copyright">Test Copyright</metaTag>
    <Part>
      <Staff><defaultClef>G</defaultClef></Staff>
      <trackName>Piano</trackName>
    </Part>
    <Staff id="1">
      <Measure number="1">
        <voice>
          <KeySig><concertKey>-1</concertKey></KeySig>
          <TimeSig><sigN>4</sigN><sigD>4</sigD></TimeSig>
          <Tempo><tempo>2</tempo><text>♩ = 120</text></Tempo>
          <Harmony><harmonyInfo><name>maj7</name><root>14</root></harmonyInfo></Harmony>
          <Chord><durationType>quarter</durationType>
            <Note><pitch>60</pitch><tpc>14</tpc></Note>
            <Lyrics><text>Hello</text></Lyrics></Chord>
          <Chord><durationType>quarter</durationType>
            <Note><pitch>62</pitch><tpc>16</tpc></Note></Chord>
          <Chord><durationType>half</durationType>
            <Note><pitch>64</pitch><tpc>18</tpc></Note></Chord>
        </voice>
      </Measure>
      <Measure number="2">
        <startRepeat/>
        <voice>
          <Harmony><harmonyInfo><name>7</name><root>15</root><bass>19</bass></harmonyInfo></Harmony>
          <Chord><durationType>whole</durationType>
            <Note><pitch>65</pitch><tpc>13</tpc></Note></Chord>
        </voice>
        <endRepeat>2</endRepeat>
      </Measure>
    </Staff>
  </Score>
</museScore>'''


def mscz(mscx: str = MSCX, name: str = "test.mscx") -> bytes:
    """A MuseScore .mscz archive holding the given .mscx document."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("META-INF/container.xml",
                   f'<container><rootfiles><rootfile full-path="{name}"/>'
                   "</rootfiles></container>")
        z.writestr(name, mscx)
    return buf.getvalue()


def test_unpack_mscz():
    assert unpack_mscz(mscz()).decode() == MSCX
    with pytest.raises(ValueError, match="no MuseScore score"):
        unpack_mscz(mscz("<museScore/>", name="readme.txt"))
    with pytest.raises(ValueError, match="not a valid .mscz"):
        unpack_mscz(b"not a zip")


def test_mscz_to_musicxml_metadata():
    data = mscz_to_musicxml(mscz())
    assert data.startswith(b"<?xml") and b"<score-partwise" in data
    assert parse_metadata(data, Path("Test Tune.mscz")) == {
        "title": "Test Tune",
        "composer": "Test Composer",
        "arranger": "Test Arranger",
        "copyright": "Test Copyright",
        "key": "F",
        "time": "4/4",
        "tempo": 120,
        "measures": 2,
        "chords": 2,
        "has_melody": True,
        "has_lyrics": True,
        "parts": 1,
    }


def test_mscx_to_musicxml_is_engravable():
    data = mscx_to_musicxml(MSCX.encode())
    eng = Engraver().load(data.decode())
    bars = chart_from_mei(eng.mei)
    assert len(bars) == 2
    assert [c.text for c in bars[0].chords] == ["CMaj7"]
    assert [c.text for c in bars[1].chords] == ["G7/B"]


def test_mscz_imports_into_the_library(tmp_path):
    lib = Library(tmp_path / "lib.sqlite")
    result = import_files([("Test Tune.mscz", mscz())], lib)
    assert result.added and not result.errors
    [song] = lib.songs()
    assert song.title == "Test Tune" and song.composer == "Test Composer"
    stored = lib.musicxml(song.id)
    assert stored.startswith('<?xml') and "<score-partwise" in stored
    assert parse_metadata(stored.encode(), Path("Test Tune.mscz"))["chords"] == 2
    assert lib.lyrics(song.id) == "Hello"  # adopted from the score on import
    assert lib.lyrics_alignment(song.id) == (["1", "2"], [1, 0])


def test_old_musescore_files_are_rejected_with_a_hint(tmp_path):
    old = '<?xml version="1.0"?><museScore version="2.06"><Score/></museScore>'
    with pytest.raises(ValueError, match="MuseScore 2.06 files are not supported"):
        mscx_to_musicxml(old.encode())
    lib = Library(tmp_path / "lib.sqlite")
    result = import_files([("Old.mscz", mscz(old))], lib)
    assert not result.added
    assert "Old.mscz: MuseScore 2.06 files are not supported" in result.errors[0]


def test_find_scores_finds_musescore_files(tmp_path):
    (tmp_path / "one.mscz").write_bytes(mscz())
    (tmp_path / "two.mscx").write_text(MSCX)
    (tmp_path / "three.musicxml").write_text("<score-partwise/>")
    (tmp_path / "notes.txt").write_text("not a score")
    assert [p.name for p in find_scores(tmp_path)] == ["one.mscz", "three.musicxml", "two.mscx"]


def test_mscz_placeholder_metadata_uses_the_file_name(tmp_path):
    placeholder = (MSCX.replace("Test Tune", "Partitura sem título")
                   .replace("Test Composer", "Compositor / arranjador")
                   .replace("Test Arranger", ""))
    lib = Library(tmp_path / "lib.sqlite")
    result = import_files([("Copacabana.mscz", mscz(placeholder))], lib)
    assert result.added and not result.errors
    [song] = lib.songs()
    assert song.title == "Copacabana"
    assert song.composer == ""
    assert song.source == "Copacabana.mscz"
