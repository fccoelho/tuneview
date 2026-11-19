# TuneView

A leadsheet viewer for the music stand. Pick a tune and TuneView shows it the
way you need to play it:

![TuneView playing "After You've Gone": the engraved leadsheet, with the bar being played highlighted](docs/screenshot-sheet.png)

![The same tune as a large-print chord chart](docs/screenshot-chords.png)

- **Sheet**: the engraved leadsheet, with melody, chord symbols and lyrics (when
  the source has them), repeats, endings and D.S./coda marks.
- **Chords**: a large-print chord chart, four bars per line, with chords placed
  on their beats. It's easy to read from a distance.
- **Transposition** for B♭, E♭ and F instruments, plus any extra shift in
  semitones. The notation, the chord chart and the displayed key all follow it.
- **Chord symbols**: Compact (the default) spells them the jazz way: `Cm7`,
  `C△7`, `Cø7`, `C°7`. As written uses the labels from the source file instead
  (`Cmin7`, `Cmaj7`…).
- **Playback**: the melody, a simple band (bass and piano comping from the chord
  symbols) or both. It plays at concert pitch plus your own transposition, at
  50–150% of the written tempo. Swing is optional: Off (the default), Light,
  Medium, Hard, or Auto, which guesses per tune. The bar being played is
  highlighted and kept in view in both the Sheet and Chords views.
- **Scales for improvising**: turn on Scales (or press `G`) and, while the tune
  plays, the bar being played shows a scale for each chord as faint noteheads on
  the staff, climbing from the chord symbol, with the scale names in the
  transport bar. The choice follows chord-scale practice (m7 → Dorian, 7 →
  Mixolydian, ø7 → Locrian, °7 → whole-half diminished, 7♭9 → half-whole…),
  refined by the key (a m7 on the 3rd of the key is Phrygian, a △7 on the 4th is
  Lydian) and by where a dominant resolves (into a minor chord → Phrygian
  dominant). Accidentals outside the key signature are printed above their
  notes.
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

TuneView reads MusicXML leadsheets (`.xml` / `.musicxml`) into a single SQLite
file, `leadsheets.sqlite` at the project root (override with `TUNEVIEW_DB`):

```bash
uv run tuneview-import /path/to/musicxml    # searched recursively
```

Identical files are imported once, and each sub-folder name becomes the tune's
volume (e.g. `I`, `II`). Running the import again adds any new files.

## Run

```bash
uv run tuneview                   # desktop window
TUNEVIEW_WEB=1 uv run tuneview    # in the browser instead
uv run pytest                     # tests
```

## Keyboard

| Key | Action |
| --- | --- |
| `/` | search (`Enter` opens the best match, `Esc` clears) |
| `P` | play / stop |
| `Space` | pause / resume while playing (scrolls otherwise) |
| `G` | show / hide the improvisation scales |
| `↓` `↑`, `PgDn` `PgUp`, `Space` | scroll (page-turner pedals send these; Space only when not playing) |
| `Home` | back to the top |
| `]` `[` | transpose up / down a semitone |
| `+` `-` | larger / smaller notation |
| `S` / `C` | sheet / chord chart |
| `L` | show / hide the library |
| `D` | dark / light |
| `F11` | full screen (desktop) |
