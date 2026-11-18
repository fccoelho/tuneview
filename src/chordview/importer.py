"""Import a folder of MusicXML leadsheets into the Chordview library.

Usage: chordview-import /path/to/musicxml [--db leadsheets.sqlite]
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

from .library import Library, default_db_path

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
        if creator.get("type") in (None, "composer"):
            composer, arranger = split_composer(creator.text or "")
            break
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


def find_scores(folder: Path) -> list[Path]:
    return sorted(
        p for p in folder.rglob("*") if p.suffix.lower() in (".xml", ".musicxml") and p.is_file()
    )


def import_folder(folder: Path, library: Library, log=print) -> tuple[int, int, list[str]]:
    """Import every MusicXML file under folder. Returns (added, duplicates, errors)."""
    library.create()
    added = duplicates = 0
    errors: list[str] = []
    for path in find_scores(folder):
        data = path.read_bytes()
        try:
            meta = parse_metadata(data, path)
        except (ET.ParseError, ValueError) as exc:
            errors.append(f"{path}: {exc}")
            continue
        meta["collection"] = path.parent.name if path.parent != folder else ""
        meta["source"] = str(path.relative_to(folder))
        if library.add(meta, data, hashlib.sha1(data).hexdigest()):
            added += 1
        else:
            duplicates += 1
    library.commit()
    log(f"Imported {added} leadsheets ({duplicates} duplicates skipped, {len(errors)} errors).")
    for e in errors:
        log(f"  error: {e}")
    return added, duplicates, errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("folder", type=Path, help="folder searched recursively for .xml/.musicxml")
    parser.add_argument("--db", type=Path, default=None, help=f"library file (default {default_db_path()})")
    args = parser.parse_args(argv)
    if not args.folder.is_dir():
        parser.error(f"{args.folder} is not a folder")
    _, _, errors = import_folder(args.folder, Library(args.db))
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
