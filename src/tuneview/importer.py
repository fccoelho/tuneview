"""Import leadsheets into the TuneView library.

MusicXML (.musicxml, .xml or compressed .mxl) is used as written; MuseScore 3/4
scores (.mscz and .mscx) are converted to MusicXML first.

Usage: tuneview-import /path/to/scores [--db leadsheets.sqlite]
"""

from __future__ import annotations

import argparse
import hashlib
import io
import re
import sys
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from .library import Library, default_db_path
from .mscz import mscx_to_musicxml, mscz_to_musicxml

MAJOR_KEYS = ["C♭", "G♭", "D♭", "A♭", "E♭", "B♭", "F", "C", "G", "D", "A", "E", "B", "F♯", "C♯"]
MINOR_KEYS = ["A♭", "E♭", "B♭", "F", "C", "G", "D", "A", "E", "B", "F♯", "C♯", "G♯", "D♯", "A♯"]
# "Arr. X", "arranged by X", "Arr: X" — but not words that merely start with "arr".
ARRANGER_RE = re.compile(r"\barr(?:anged\b|\.|:|\b)\s*(?:by\b)?\s*:?\s*", re.I)


def key_name(fifths: int, mode: str | None) -> str:
    """'G', 'Em' … from a MusicXML <key>; fifths is clamped to the 15 real keys."""
    i = max(-7, min(7, fifths)) + 7
    return f"{MINOR_KEYS[i]}m" if mode == "minor" else MAJOR_KEYS[i]


def _text(el: ET.Element | None) -> str:
    return " ".join((el.text or "").split()) if el is not None else ""


def split_composer(raw: str) -> tuple[str, str]:
    """'Benny Golson\\nArr. 6EQUJ5' -> ('Benny Golson', '6EQUJ5')."""
    composer, arranger = [], []
    for line in raw.splitlines():
        m = ARRANGER_RE.search(line)
        head, tail = (line[: m.start()], line[m.end():]) if m else (line, "")
        if head := head.strip(" ,;-"):
            composer.append(head)
        if tail.strip():
            arranger.append(tail.strip())
    return ", ".join(composer), ", ".join(arranger)


def parse_metadata(data: bytes, path: Path) -> dict:
    root = ET.fromstring(data)
    if root.tag != "score-partwise":
        raise ValueError(f"unsupported MusicXML root <{root.tag}>")

    title = _text(root.find("movement-title")) or _text(root.find("work/work-title"))
    title = title or path.stem
    composer, arranger = "", ""
    for creator in root.iterfind("identification/creator"):
        kind = creator.get("type")
        if kind == "arranger":
            if not arranger:
                arranger = _text(creator)
        elif kind in (None, "composer") and not composer:
            composer, from_line = split_composer(creator.text or "")
            arranger = arranger or from_line
    copyright_ = _text(root.find("identification/rights"))

    parts = root.findall("part")
    first = parts[0] if parts else root
    measures = first.findall("measure")

    key_el = first.find(".//attributes/key")
    key = ""
    if key_el is not None and key_el.find("fifths") is not None:
        key = key_name(int(key_el.findtext("fifths", "0")), key_el.findtext("mode"))

    time_el = first.find(".//attributes/time")
    time = ""
    if time_el is not None and time_el.findtext("beats"):
        time = f"{time_el.findtext('beats')}/{time_el.findtext('beat-type')}"

    tempo = None
    sound = root.find(".//sound[@tempo]")
    if sound is not None:
        try:
            tempo = round(float(sound.get("tempo")))
        except ValueError:
            pass

    has_lyrics = any(_text(t) for t in root.iterfind(".//lyric/text"))
    has_melody = first.find(".//note/pitch") is not None

    return {
        "title": title,
        "composer": composer,
        "arranger": arranger,
        "copyright": copyright_,
        "key": key,
        "time": time,
        "tempo": tempo,
        "measures": len(measures),
        "chords": sum(1 for _ in root.iter("harmony")),
        "has_melody": has_melody,
        "has_lyrics": has_lyrics,
        "parts": len(parts),
    }


SCORE_EXTENSIONS = (".musicxml", ".xml", ".mxl", ".mscz", ".mscx")


def find_scores(folder: Path) -> list[Path]:
    """Every supported score file under folder."""
    return sorted(
        p for p in folder.rglob("*") if p.suffix.lower() in SCORE_EXTENSIONS and p.is_file()
    )


def unpack_mxl(data: bytes) -> bytes:
    """The MusicXML inside a compressed .mxl file (a zip named by META-INF/container.xml)."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            names = z.namelist()
            rootfile = None
            if "META-INF/container.xml" in names:
                container = ET.fromstring(z.read("META-INF/container.xml"))
                # (an Element with no children is falsy: compare with None, never use `or`)
                el = next((e for e in container.iter() if e.tag.split("}")[-1] == "rootfile"), None)
                rootfile = el.get("full-path") if el is not None else None
            if rootfile is None:  # no container: the first score in the archive
                rootfile = next((n for n in names if not n.startswith("META-INF/")
                                 and n.lower().endswith((".xml", ".musicxml"))), None)
            if rootfile is None:
                raise ValueError("no MusicXML score inside this .mxl file")
            return z.read(rootfile)
    except zipfile.BadZipFile as exc:
        raise ValueError("not a valid .mxl file") from exc


@dataclass
class ImportResult:
    added: list[int] = field(default_factory=list)  # ids of the new songs
    duplicates: int = 0
    errors: list[str] = field(default_factory=list)  # "name: reason"

    def summary(self) -> str:
        """'Added 2 tunes (1 already in the library, 1 couldn't be read).'"""
        n = len(self.added)
        text = f"Added {n} tune{'' if n == 1 else 's'}"
        notes = []
        if self.duplicates:
            notes.append(f"{self.duplicates} already in the library")
        if self.errors:
            notes.append(f"{len(self.errors)} couldn't be read")
        return text + (f" ({', '.join(notes)})" if notes else "") + "."


def import_score(library: Library, data: bytes, name: str, result: ImportResult, *,
                 collection: str = "") -> None:
    """Add one score's file contents to the library, recording the outcome in result.
    name is the file name, used for the format and as a fallback title."""
    path = Path(name)
    suffix = path.suffix.lower()
    try:
        if suffix == ".mxl":
            data = unpack_mxl(data)
        elif suffix == ".mscz":
            data = mscz_to_musicxml(data)
        elif suffix == ".mscx":
            data = mscx_to_musicxml(data)
        meta = parse_metadata(data, path)
    except (ET.ParseError, ValueError) as exc:
        result.errors.append(f"{name}: {exc}")
        return
    meta["collection"] = collection
    meta["source"] = name
    sha1 = hashlib.sha1(data).hexdigest()
    if library.add(meta, data, sha1):
        result.added.append(library.song_id(sha1))
    else:
        result.duplicates += 1


def import_folder(folder: Path, library: Library, log=print) -> tuple[int, int, list[str]]:
    """Import every score under folder. Returns (added, duplicates, errors)."""
    result = import_paths(find_scores(folder), library, root=folder)
    log(f"Imported {len(result.added)} leadsheets ({result.duplicates} duplicates skipped,"
        f" {len(result.errors)} errors).")
    for e in result.errors:
        log(f"  error: {e}")
    return len(result.added), result.duplicates, result.errors


def import_paths(paths: list[Path], library: Library, root: Path | None = None) -> ImportResult:
    """Import score files. Under a root folder, each sub-folder names the tune's volume."""
    library.create()
    result = ImportResult()
    for path in paths:
        rel = path.relative_to(root) if root else Path(path.name)
        collection = path.parent.name if root and path.parent != root else ""
        try:
            data = path.read_bytes()
        except OSError as exc:
            result.errors.append(f"{rel}: {exc.strerror or exc}")
            continue
        import_score(library, data, str(rel), result, collection=collection)
    library.commit()
    return result


def import_files(files: list[tuple[str, bytes]], library: Library) -> ImportResult:
    """Import scores given as (file name, contents), e.g. picked in the app."""
    library.create()
    result = ImportResult()
    for name, data in files:
        import_score(library, data, name, result)
    library.commit()
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("folder", type=Path,
                        help="folder searched recursively for MusicXML or MuseScore scores")
    parser.add_argument("--db", type=Path, default=None, help=f"library file (default {default_db_path()})")
    args = parser.parse_args(argv)
    if not args.folder.is_dir():
        parser.error(f"{args.folder} is not a folder")
    _, _, errors = import_folder(args.folder, Library(args.db))
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
