"""The leadsheet library: a SQLite file holding song metadata and compressed MusicXML."""

from __future__ import annotations

import functools
import os
import sqlite3
import threading
import unicodedata
import zlib
from dataclasses import dataclass
from pathlib import Path

SCHEMA_VERSION = 3  # v3 added the lyrics table to v2

SCHEMA = """
CREATE TABLE IF NOT EXISTS song (
    id          INTEGER PRIMARY KEY,
    title       TEXT NOT NULL,
    composer    TEXT NOT NULL DEFAULT '',
    arranger    TEXT NOT NULL DEFAULT '',
    copyright   TEXT NOT NULL DEFAULT '',
    collection  TEXT NOT NULL DEFAULT '',
    key         TEXT NOT NULL DEFAULT '',
    time        TEXT NOT NULL DEFAULT '',
    tempo       INTEGER,
    measures    INTEGER NOT NULL DEFAULT 0,
    chords      INTEGER NOT NULL DEFAULT 0,
    has_melody  INTEGER NOT NULL DEFAULT 0,
    has_lyrics  INTEGER NOT NULL DEFAULT 0,
    parts       INTEGER NOT NULL DEFAULT 1,
    source      TEXT NOT NULL DEFAULT '',
    sha1        TEXT NOT NULL UNIQUE,
    musicxml    BLOB NOT NULL
);
-- Lyrics typed in TuneView: the user's own work, so they are kept apart from the
-- imported scores and keyed by the score's content, which survives a re-import.
CREATE TABLE IF NOT EXISTS lyrics (
    sha1        TEXT PRIMARY KEY,
    text        TEXT NOT NULL,
    updated     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


def default_db_path() -> Path:
    """The library file: $TUNEVIEW_DB, else leadsheets.sqlite at the project root."""
    env = os.environ.get("TUNEVIEW_DB")
    if env:
        return Path(env).expanduser()
    return Path(__file__).resolve().parents[2] / "leadsheets.sqlite"


def fold(text: str) -> str:
    """Lowercase and strip accents, for accent-insensitive search."""
    norm = unicodedata.normalize("NFKD", text.casefold())
    return "".join(c for c in norm if not unicodedata.combining(c))


@dataclass(frozen=True)
class SongInfo:
    id: int
    title: str
    composer: str
    arranger: str
    copyright: str
    collection: str
    key: str
    time: str
    tempo: int | None
    measures: int
    chords: int
    has_melody: bool
    has_lyrics: bool
    parts: int

    @property
    def search_text(self) -> str:
        return fold(f"{self.title} {self.composer}")


_INFO_COLUMNS = (
    "id, title, composer, arranger, copyright, collection, key, time, tempo, "
    "measures, chords, has_melody, has_lyrics, parts"
)


def _locked(method):
    """One thread at a time on the connection (the app imports and renders off its UI thread)."""
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return wrapper


class Library:
    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path else default_db_path()
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self._lock = threading.RLock()
        if self._version() == 2:  # add the lyrics table; nothing else changed
            self.conn.executescript(SCHEMA)
            self.conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            self.conn.commit()

    def _version(self) -> int:
        return self.conn.execute("PRAGMA user_version").fetchone()[0]

    # -- writing -----------------------------------------------------------
    @_locked
    def create(self) -> None:
        version = self._version()
        if version not in (0, SCHEMA_VERSION):
            raise RuntimeError(f"{self.path} has schema v{version}; expected v{SCHEMA_VERSION}")
        if version == 0:
            # A pre-v2 database (the old harmony/melody tables) is replaced wholesale.
            for (name,) in self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall():
                self.conn.execute(f'DROP TABLE "{name}"')
        self.conn.executescript(SCHEMA)
        self.conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        self.conn.commit()

    @_locked
    def add(self, meta: dict, musicxml: bytes, sha1: str) -> bool:
        """Insert a song; returns False if identical MusicXML is already present."""
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO song (title, composer, arranger, copyright, collection,"
            " key, time, tempo, measures, chords, has_melody, has_lyrics, parts, source,"
            " sha1, musicxml) VALUES (:title, :composer, :arranger, :copyright, :collection,"
            " :key, :time, :tempo, :measures, :chords, :has_melody, :has_lyrics, :parts,"
            " :source, :sha1, :musicxml)",
            {**meta, "sha1": sha1, "musicxml": zlib.compress(musicxml, 9)},
        )
        return cur.rowcount == 1

    @_locked
    def commit(self) -> None:
        self.conn.commit()

    # -- reading -----------------------------------------------------------
    @_locked
    def is_ready(self) -> bool:
        return self._version() == SCHEMA_VERSION

    @_locked
    def is_new(self) -> bool:
        """No tables at all: a library file that was just created (or left empty)."""
        return self.conn.execute("SELECT count(*) FROM sqlite_master").fetchone()[0] == 0

    @_locked
    def songs(self) -> list[SongInfo]:
        rows = self.conn.execute(
            f"SELECT {_INFO_COLUMNS} FROM song ORDER BY title COLLATE NOCASE"
        ).fetchall()
        return [
            SongInfo(*r[:11], has_melody=bool(r[11]), has_lyrics=bool(r[12]), parts=r[13])
            for r in rows
        ]

    @_locked
    def song_id(self, sha1: str) -> int:
        return self.conn.execute("SELECT id FROM song WHERE sha1 = ?", (sha1,)).fetchone()[0]

    @_locked
    def musicxml(self, song_id: int) -> str:
        row = self.conn.execute("SELECT musicxml FROM song WHERE id = ?", (song_id,)).fetchone()
        if row is None:
            raise KeyError(song_id)
        return zlib.decompress(row[0]).decode("utf-8")

    # -- lyrics ------------------------------------------------------------
    @_locked
    def lyrics(self, song_id: int) -> str:
        """The song's lyrics as typed in TuneView, or ''."""
        row = self.conn.execute(
            "SELECT l.text FROM lyrics l JOIN song s ON s.sha1 = l.sha1 WHERE s.id = ?", (song_id,)
        ).fetchone()
        return row[0] if row else ""

    @_locked
    def set_lyrics(self, song_id: int, text: str) -> None:
        """Save the song's lyrics; blank text deletes them."""
        row = self.conn.execute("SELECT sha1 FROM song WHERE id = ?", (song_id,)).fetchone()
        if row is None:
            raise KeyError(song_id)
        text = text.strip()
        if text:
            self.conn.execute(
                "INSERT INTO lyrics (sha1, text) VALUES (?, ?) ON CONFLICT (sha1)"
                " DO UPDATE SET text = excluded.text, updated = CURRENT_TIMESTAMP",
                (row[0], text),
            )
        else:
            self.conn.execute("DELETE FROM lyrics WHERE sha1 = ?", (row[0],))
        self.conn.commit()


def search(songs: list[SongInfo], query: str) -> list[SongInfo]:
    """Songs whose title or composer contain every word of the query, best first.

    Titles that start with the query rank first, then titles containing it, then
    titles containing all its words, then composer matches; ties keep library order.
    """
    q = " ".join(fold(query).split())
    words = q.split()
    if not words:
        return songs

    def rank(s: SongInfo) -> int:
        title = " ".join(fold(s.title).split())
        if title.startswith(q):
            return 0
        if q in title:
            return 1
        if all(w in title for w in words):
            return 2
        return 3

    hits = [s for s in songs if all(w in s.search_text for w in words)]
    return sorted(hits, key=rank)
