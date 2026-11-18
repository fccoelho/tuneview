import sqlite3

from tuneview.library import Library, SongInfo, fold, search


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
    assert tables == {"song"}
