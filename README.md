# Chordview

A leadsheet viewer for the music stand. Pick a tune and Chordview shows it the
way you need to play it:

- **Sheet**: the engraved leadsheet, with melody, chord symbols and lyrics (when
  the source has them), repeats, endings and D.S./coda marks.
- **Chords**: a large-print chord chart, four bars per line, with chords placed
  on their beats. It's easy to read from a distance.
- **Transposition** for B♭, E♭ and F instruments, plus any extra shift in
  semitones. The notation, the chord chart and the displayed key all follow it.
- Instant search across title and composer, light and dark themes, and keyboard
  control that works with page-turner pedals.

Built with [Flet](https://flet.dev) 1.x, with [Verovio](https://www.verovio.org)
for engraving.

## Setup

Dependencies are managed with [uv](https://docs.astral.sh/uv/):

```bash
uv sync
```

### Build the library

Chordview reads MusicXML leadsheets (`.xml` / `.musicxml`) into a single SQLite
file, `leadsheets.sqlite` at the project root (override with `CHORDVIEW_DB`):

```bash
uv run chordview-import /path/to/musicxml   # searched recursively
```

Identical files are imported once, and each sub-folder name becomes the tune's
volume (e.g. `I`, `II`). Running the import again adds any new files.

## Run

```bash
uv run chordview                      # desktop window
CHORDVIEW_WEB=1 uv run chordview      # in the browser instead
uv run pytest                         # tests
```

## Keyboard

| Key | Action |
| --- | --- |
| `/` | search (`Enter` opens the best match, `Esc` clears) |
| `↓` `↑`, `PgDn` `PgUp`, `Space` | scroll (page-turner pedals send these) |
| `Home` | back to the top |
| `]` `[` | transpose up / down a semitone |
| `+` `-` | larger / smaller notation |
| `S` / `C` | sheet / chord chart |
| `L` | show / hide the library |
| `D` | dark / light |
| `F11` | full screen (desktop) |
