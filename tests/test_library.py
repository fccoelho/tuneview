import sqlite3

import pytest

from tuneview.library import SCHEMA, Library, SongInfo, fold, search


def song(id_, title, composer=""):
    return SongInfo(id_, title, composer, "", "", "", "C", "4/4", None, 8, 8, True, False, 1)


SONGS = [
    song(1, "Along Came Betty", "Benny Golson"),
    song(2, "I Don't Want No One But You", "Eddie Harris"),
    song(3, "Is You Is or Is You Ain't My Baby", "Louis Jordan"),
    song(4, "Café", "Egberto Gismonti"),
    song(5, "Blues for Betty", "Someone"),
]


def test_fold_strips_case_and_accents():
    assert fold("Café São") == "cafe sao"


def test_search_matches_all_words_in_title_or_composer():
    assert [s.id for s in search(SONGS, "golson betty")] == [1]
    assert [s.id for s in search(SONGS, "CAFE")] == [4]
    assert search(SONGS, "   ") == SONGS


def test_search_ranks_title_matches_first():
    # "is" also occurs in "Harris"; the title match must come first.
    assert [s.id for s in search(SONGS, "is you is")][0] == 3
    # Prefix match beats a match later in the title.
    assert [s.id for s in search(SONGS, "betty")] == [1, 5]
    assert [s.id for s in search(SONGS, "blues for")] == [5]


def test_create_replaces_legacy_schema(tmp_path):
    path = tmp_path / "old.sqlite"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE harmony (id INTEGER, chords JSON)")
    con.commit()
    con.close()

    lib = Library(path)
    assert not lib.is_ready()
    lib.create()
    assert lib.is_ready()
    tables = {r[0] for r in lib.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert tables == {"song", "lyrics", "lyrics_alignment"}


def test_lyrics_are_saved_and_survive_a_reimport(tmp_path, mini_path):
    from tuneview.importer import import_folder

    (tmp_path / "scores").mkdir()
    (tmp_path / "scores" / "mini.musicxml").write_bytes(mini_path.read_bytes())
    lib = Library(tmp_path / "lib.sqlite")
    import_folder(tmp_path / "scores", lib, log=lambda *a: None)
    song_id = lib.songs()[0].id
    assert lib.lyrics(song_id) == ""
    lib.set_lyrics(song_id, "  [Chorus]\nHello\n\n")
    assert lib.lyrics(song_id) == "[Chorus]\nHello"
    lib.set_lyrics(song_id, "Hello again")
    assert lib.lyrics(song_id) == "Hello again"
    import_folder(tmp_path / "scores", lib, log=lambda *a: None)  # running the import again
    assert lib.lyrics(lib.songs()[0].id) == "Hello again"
    lib.set_lyrics(song_id, "   ")  # blank deletes
    assert lib.lyrics(song_id) == ""
    assert lib.conn.execute("SELECT count(*) FROM lyrics").fetchone()[0] == 0


def test_a_v2_library_gains_the_lyrics_table(tmp_path):
    path = tmp_path / "v2.sqlite"
    con = sqlite3.connect(path)
    con.executescript(SCHEMA.split("-- Lyrics")[0])  # the song table alone
    con.execute("INSERT INTO song (title, sha1, musicxml) VALUES ('Tune', 'abc', x'00')")
    con.execute("PRAGMA user_version = 2")
    con.commit()
    con.close()
    lib = Library(path)
    assert lib.is_ready()
    assert [s.title for s in lib.songs()] == ["Tune"]  # nothing lost
    lib.set_lyrics(lib.songs()[0].id, "La la")
    assert lib.lyrics(lib.songs()[0].id) == "La la"


def test_a_v3_library_gains_the_alignment_table(tmp_path):
    path = tmp_path / "v3.sqlite"
    con = sqlite3.connect(path)
    con.executescript(SCHEMA.split("-- Where the lyrics go")[0])  # v3: song + lyrics
    con.execute("INSERT INTO song (title, sha1, musicxml) VALUES ('Tune', 'abc', x'00')")
    con.execute("INSERT INTO lyrics (sha1, text) VALUES ('abc', 'La la')")
    con.execute("PRAGMA user_version = 3")
    con.commit()
    con.close()

    lib = Library(path)
    assert lib.is_ready()
    song_id = lib.songs()[0].id
    assert lib.lyrics(song_id) == "La la"  # nothing lost
    assert lib.lyrics_alignment(song_id) is None
    lib.set_lyrics_alignment(song_id, ["m1", "m2"], [1, 1])
    assert lib.lyrics_alignment(song_id) == (["m1", "m2"], [1, 1])


def test_chart_lyric_alignment_is_saved_and_cleared_with_the_words(tmp_path, mini_path):
    from tuneview.importer import import_folder

    (tmp_path / "scores").mkdir()
    (tmp_path / "scores" / "mini.musicxml").write_bytes(mini_path.read_bytes())
    lib = Library(tmp_path / "lib.sqlite")
    import_folder(tmp_path / "scores", lib, log=lambda *a: None)
    song_id = lib.songs()[0].id

    lib.set_lyrics(song_id, "One two three four")
    lib.set_lyrics_alignment(song_id, ["m1", "m2"], [3, 1])
    assert lib.lyrics_alignment(song_id) == (["m1", "m2"], [3, 1])

    lib.set_lyrics(song_id, "One two three four")  # unchanged: keep the alignment
    assert lib.lyrics_alignment(song_id) == (["m1", "m2"], [3, 1])

    lib.set_lyrics(song_id, "One two three five")  # new words: the split no longer fits
    assert lib.lyrics_alignment(song_id) is None

    with pytest.raises(ValueError):
        lib.set_lyrics_alignment(song_id, ["m1"], [1, 2])
    with pytest.raises(ValueError):
        lib.set_lyrics_alignment(song_id, ["m1", "m2"], [-1, 1])
    with pytest.raises(KeyError):
        lib.set_lyrics_alignment(9999, [], [])
