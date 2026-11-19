"""TuneView's Flet interface: a library on the left, the leadsheet on the music stand."""

from __future__ import annotations

import asyncio
import atexit
import bisect
import hashlib
import os
import shutil
import socket
import tempfile
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import flet as ft
import flet_audio as fta

from .engraver import (
    CHORD_STYLES,
    INSTRUMENTS,
    ChartBar,
    Engraver,
    Page,
    chart_from_mei,
    normalize_shift,
    split_chord,
)
from .importer import (
    SCORE_EXTENSIONS,
    ImportResult,
    find_scores,
    import_files,
    import_paths,
)
from .library import Library, SongInfo, search
from .lyrics import (
    chunks, join_bars, lyric_words, spread_words, split_bars, split_words, verses,
)
from .player import (
    PLAY_MODES,
    SAMPLE_RATE,
    SWING,
    Player,
    accompaniment,
    bar_times,
    melody,
    perform,
    should_swing,
    synthesize,
    tempo_map,
    wav_bytes,
)
from .scales import Mark, ScaleGuide, scale_guides, scale_marks

SHEET, CHART, LYRICS = "sheet", "chart", "lyrics"
ZOOM_STEPS = [30, 35, 40, 45, 50, 55, 60, 70, 80]
SPEEDS = [0.5, 0.6, 0.75, 0.9, 1.0, 1.1, 1.25, 1.5]
SIDEBAR_WIDTH = 330
SHEET_PAD = 12  # vertical padding around each engraved page
NARROW = 900  # below this width the library and the music take turns
SEED_COLOR = "#3f51b5"

# The web server serves these files to the browser; audio is written here on play.
_WEB_ASSETS: Path | None = None


def _web_assets() -> Path:
    """A temporary asset directory, served by the web version to the browser."""
    global _WEB_ASSETS
    if _WEB_ASSETS is None:
        _WEB_ASSETS = Path(tempfile.mkdtemp(prefix="tuneview-web-"))
        (_WEB_ASSETS / "audio").mkdir(parents=True, exist_ok=True)
        atexit.register(shutil.rmtree, _WEB_ASSETS, ignore_errors=True)
    return _WEB_ASSETS


def _audio_asset(pcm) -> str:
    """Write PCM as a WAV asset and return the URL path the browser should fetch."""
    data = wav_bytes(pcm)
    name = hashlib.sha1(data).hexdigest()[:20] + ".wav"
    path = _web_assets() / "audio" / name
    if not path.exists():  # content-addressed, so the same tune reuses the file
        fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".wav")
        try:
            with os.fdopen(fd, "wb") as file:
                file.write(data)
            os.replace(tmp, path)  # browsers/clients may race to write the same tune
        finally:
            Path(tmp).unlink(missing_ok=True)
    return f"audio/{name}"


MAJOR = ["C", "D♭", "D", "E♭", "E", "F", "G♭", "G", "A♭", "A", "B♭", "B"]
MINOR = ["C", "C♯", "D", "E♭", "E", "F", "F♯", "G", "G♯", "A", "B♭", "B"]
PITCH = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}


def transpose_key(key: str, semitones: int) -> str:
    """'B♭' +2 -> 'C'; 'Em' +3 -> 'Gm'. Unknown keys come back unchanged."""
    if not key or key[0] not in PITCH:
        return key
    minor = key.endswith("m")
    name = key[:-1] if minor else key
    pc = PITCH[name[0]] + name[1:].count("♯") - name[1:].count("♭")
    pc = (pc + semitones) % 12
    return f"{MINOR[pc]}m" if minor else MAJOR[pc]


@dataclass
class Rendered:
    song: SongInfo
    pages: list[Page]
    bars: list[ChartBar]
    shift: int
    page_width: int = 0
    timeline: list[tuple[str, float]] = field(default_factory=list)
    melody: list[tuple[float, float, int]] = field(default_factory=list)
    tempos: list[tuple[float, float]] = field(default_factory=list)
    guides: dict[str, ScaleGuide] = field(default_factory=dict)  # chord id -> scale
    error: str = ""

    @property
    def playable(self) -> bool:
        return bool(self.timeline) and (bool(self.melody) or any(b.chords for b in self.bars))


@dataclass
class State:
    songs: list[SongInfo] = field(default_factory=list)
    current: SongInfo | None = None
    rendered: Rendered | None = None
    mode: str = SHEET
    instrument: str = "C"
    shift: int = 0  # the player's own transposition, on top of the instrument's
    zoom: int = 45
    library_open: bool = True
    search_focused: bool = False
    editing: bool = False  # the lyrics editor is open: keys are typing, not shortcuts
    render_token: int = 0
    speed: float = 1.0
    play_mode: str = "melody"  # see player.PLAY_MODES
    swing: str = "Off"  # see player.SWING; swing is opt-in
    chord_style: str = "compact"  # see engraver.CHORD_STYLES
    show_scales: bool = False  # draw improvisation scales on the bar being played
    chart_lyrics: bool = True  # print the lyric words under the chords in the chord chart

    @property
    def total_shift(self) -> int:
        return normalize_shift(INSTRUMENTS[self.instrument] + self.shift)


class BrowserAudio:
    """Plays the synthesized tune in the browser, with a follow-along clock.

    The web version runs on one computer but may be opened from others, so the
    PCM is written as a WAV asset and fetched and played by each browser: every
    listener hears the tune locally instead of on the machine running TuneView.
    Position events from the client keep the bar highlight in step with the
    sound that is actually playing.
    """

    def __init__(self, page: ft.Page, on_finished: Callable[[], None]):
        self.page = page
        self._on_finished = on_finished
        self._audio: fta.Audio | None = None
        self._length = 0.0
        self._anchor = 0.0
        self._anchor_time = time.monotonic()
        self._playing = False
        self._paused = False
        self._closing = False

    @property
    def playing(self) -> bool:
        return self._playing

    @property
    def paused(self) -> bool:
        return self._paused

    @property
    def length(self) -> float:
        return self._length

    @property
    def position(self) -> float:
        if not self._playing:
            return 0.0
        if self._paused:
            return self._anchor
        return min(self._anchor + time.monotonic() - self._anchor_time, self._length)

    def _sync(self, seconds: float) -> None:
        self._anchor = max(0.0, seconds)
        self._anchor_time = time.monotonic()

    def _on_position(self, e) -> None:
        self._sync(e.position / 1000)  # the client reports milliseconds

    def _on_state(self, e) -> None:
        if e.state == fta.AudioState.COMPLETED and not self._closing:
            self._playing = False
            self._paused = False
            self._on_finished()

    async def play(self, url: str, length: float) -> None:
        self._length = length
        self._playing = True
        self._paused = False
        self._closing = False
        self._sync(0.0)
        if self._audio is None:
            self._audio = fta.Audio(src=url, on_position_change=self._on_position,
                                    on_state_change=self._on_state)
            self.page.services.append(self._audio)
        else:
            self._audio.src = url
        self.page.update()
        await self._audio.play()

    async def pause(self) -> None:
        if not self._playing or self._paused:
            return
        self._sync(self.position)
        self._paused = True
        if self._audio is not None:
            await self._audio.pause()
        self.page.update()

    async def resume(self) -> None:
        if not self._playing or not self._paused:
            return
        self._paused = False
        self._anchor_time = time.monotonic()
        if self._audio is not None:
            await self._audio.resume()
        self.page.update()

    async def close(self) -> None:
        self._closing = True
        self._playing = False
        self._paused = False
        audio, self._audio = self._audio, None
        if audio is not None:
            try:
                await audio.pause()  # stop the sound before releasing the player
            except Exception:
                pass
            try:
                await audio.release()
            except Exception:
                pass
            if audio in self.page.services:
                self.page.services.remove(audio)
        try:
            self.page.update()
        except Exception:  # the page/session went away
            pass

    def abandon(self) -> None:
        """The browser session is gone: it already stopped, so just forget it."""
        self._closing = True
        self._playing = False
        self._paused = False
        self._audio = None


class TuneView:
    def __init__(self, page: ft.Page, library: Library):
        self.page = page
        self.library = library
        self.engraver = Engraver()
        self.player = Player()
        self._browser: BrowserAudio | None = None
        self.state = State(songs=library.songs())
        self._alignment: dict | None = None  # state of the open lyrics-alignment editor
        self._build()

    # ------------------------------------------------------------------ layout
    def _build(self) -> None:
        p = self.page
        p.title = "TuneView"
        p.theme = ft.Theme(color_scheme_seed=SEED_COLOR)
        p.dark_theme = ft.Theme(color_scheme_seed=SEED_COLOR)
        p.theme_mode = ft.ThemeMode.SYSTEM
        p.padding = 0
        p.spacing = 0
        p.on_keyboard_event = self.on_key
        p.on_platform_brightness_change = self.on_theme_changed
        p.on_resize = self.on_resize
        # The sound is made in this process: stop it when the window or tab goes away.
        p.on_disconnect = lambda e: self._shutdown_playback()
        p.on_close = lambda e: self._shutdown_playback()

        # Library -----------------------------------------------------------
        self.search_field = ft.TextField(
            hint_text="Search title or composer",
            prefix_icon=ft.Icons.SEARCH,
            border_radius=28,
            filled=True,
            dense=True,
            content_padding=ft.Padding.symmetric(horizontal=16, vertical=10),
            on_change=self.on_search,
            on_focus=lambda e: setattr(self.state, "search_focused", True),
            on_blur=lambda e: setattr(self.state, "search_focused", False),
            on_submit=self.on_search_submit,
        )
        self.count_text = ft.Text(size=12, color=ft.Colors.ON_SURFACE_VARIANT)
        self.file_picker = ft.FilePicker()  # a service: registers itself with the page
        add_tip = "Add MusicXML files (.musicxml, .xml, .mxl) to the library"
        if p.web:  # browsers can't pick folders
            self.add_button = ft.IconButton(ft.Icons.LIBRARY_ADD_OUTLINED, tooltip=add_tip,
                                            on_click=self.add_files)
        else:
            self.add_button = ft.PopupMenuButton(
                icon=ft.Icons.LIBRARY_ADD_OUTLINED, tooltip=add_tip,
                items=[
                    ft.PopupMenuItem(content=ft.Text("Add files…"), icon=ft.Icons.NOTE_ADD_OUTLINED,
                                     on_click=self.add_files),
                    ft.PopupMenuItem(content=ft.Text("Add a folder…"),
                                     icon=ft.Icons.CREATE_NEW_FOLDER_OUTLINED, on_click=self.add_folder),
                ],
            )
        self.song_list = ft.ListView(expand=True, spacing=0, padding=ft.Padding.only(bottom=12))
        self.sidebar = ft.Container(
            width=SIDEBAR_WIDTH,
            bgcolor=ft.Colors.SURFACE_CONTAINER_LOW,
            padding=ft.Padding.only(left=12, right=12, top=16),
            content=ft.Column(
                spacing=10,
                horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
                controls=[
                    ft.Row(
                        [
                            ft.Icon(ft.Icons.QUEUE_MUSIC, color=ft.Colors.PRIMARY, size=28),
                            ft.Text("TuneView", size=22, weight=ft.FontWeight.W_600, expand=True),
                            self.add_button,
                        ],
                        spacing=10,
                    ),
                    self.search_field,
                    ft.Container(self.count_text, padding=ft.Padding.only(left=8)),
                    self.song_list,
                ],
            ),
        )

        # Viewer toolbar ------------------------------------------------------
        self.library_button = ft.IconButton(
            ft.Icons.MENU_OPEN, tooltip="Hide library (L)", on_click=self.toggle_library
        )
        self.title_text = ft.Text(size=22, weight=ft.FontWeight.W_600, max_lines=1,
                                  overflow=ft.TextOverflow.ELLIPSIS)
        self.subtitle_text = ft.Text(size=13, color=ft.Colors.ON_SURFACE_VARIANT, max_lines=1,
                                     overflow=ft.TextOverflow.ELLIPSIS)
        self.info_row = ft.Row(spacing=6, wrap=True)
        self.mode_button = ft.SegmentedButton(
            selected=[SHEET],
            segments=[
                ft.Segment(value=SHEET, label=ft.Text("Sheet"), icon=ft.Icon(ft.Icons.MUSIC_NOTE)),
                ft.Segment(value=CHART, label=ft.Text("Chords"), icon=ft.Icon(ft.Icons.GRID_VIEW)),
                ft.Segment(value=LYRICS, label=ft.Text("Lyrics"), icon=ft.Icon(ft.Icons.LYRICS_OUTLINED)),
            ],
            show_selected_icon=False,
            on_change=self.on_mode,
        )
        self.instrument_dd = ft.Dropdown(
            value="C",
            width=185,
            dense=True,
            label="Instrument",
            options=[
                ft.DropdownOption(key="C", text="C (concert)"),
                ft.DropdownOption(key="B♭", text="B♭ (tpt, tenor)"),
                ft.DropdownOption(key="E♭", text="E♭ (alto, bari)"),
                ft.DropdownOption(key="F", text="F (horn)"),
            ],
            on_select=self.on_instrument,
        )
        self.chord_style_dd = ft.Dropdown(
            value=self.state.chord_style,
            width=150,
            dense=True,
            label="Chord symbols",
            tooltip="Compact: Cm7, C△7, Cø7, C°7. As written: the file's own labels.",
            options=[ft.DropdownOption(key=k, text=v) for k, v in CHORD_STYLES.items()],
            on_select=self.on_chord_style,
        )
        self.shift_text = ft.Text("0", width=30, text_align=ft.TextAlign.CENTER,
                                  weight=ft.FontWeight.W_600)
        transpose_box = self._group(
            "Transpose",
            [
                ft.IconButton(ft.Icons.REMOVE, tooltip="Down a semitone ([)",
                              on_click=lambda e: self.change_shift(-1)),
                self.shift_text,
                ft.IconButton(ft.Icons.ADD, tooltip="Up a semitone (])",
                              on_click=lambda e: self.change_shift(+1)),
            ],
        )
        zoom_box = self._group(
            "Size",
            [
                ft.IconButton(ft.Icons.ZOOM_OUT, tooltip="Smaller (-)", on_click=lambda e: self.change_zoom(-1)),
                ft.IconButton(ft.Icons.ZOOM_IN, tooltip="Larger (+)", on_click=lambda e: self.change_zoom(+1)),
            ],
        )
        self.theme_button = ft.IconButton(ft.Icons.DARK_MODE_OUTLINED, tooltip="Dark / light (D)",
                                          on_click=self.toggle_theme)
        self.fullscreen_button = ft.IconButton(ft.Icons.FULLSCREEN, tooltip="Full screen (F11)",
                                               on_click=self.toggle_fullscreen, visible=not p.web)
        # Transport bar (bottom of the viewer) --------------------------------
        self.play_button = ft.IconButton(
            ft.Icons.PLAY_ARROW_ROUNDED, tooltip="Play (P)", disabled=True,
            icon_size=30, style=ft.ButtonStyle(bgcolor=ft.Colors.PRIMARY_CONTAINER),
            on_click=self.toggle_playback,
        )
        self.time_text = ft.Text("0:00", size=12, color=ft.Colors.ON_SURFACE_VARIANT, width=80)
        self.play_progress = ft.ProgressBar(value=0, expand=True, bar_height=4, border_radius=2)
        self.play_mode_dd = ft.Dropdown(
            value=self.state.play_mode, width=212, dense=True, label="Play",
            options=[ft.DropdownOption(key=k, text=v) for k, v in PLAY_MODES.items()],
            on_select=self.on_playback_setting,
        )
        self.swing_dd = ft.Dropdown(
            value=self.state.swing, width=136, dense=True, label="Swing",
            options=[ft.DropdownOption(key=k, text=k) for k in SWING],
            on_select=self.on_playback_setting,
        )
        self.speed_dd = ft.Dropdown(
            value="1.0", width=112, dense=True, label="Tempo",
            options=[ft.DropdownOption(key=str(s), text=f"{round(s * 100)}%") for s in SPEEDS],
            on_select=self.on_playback_setting,
        )
        self.scales_switch = ft.Switch(
            label="Scales", value=self.state.show_scales, on_change=self.on_scales,
            tooltip="While playing, draw a scale to improvise with over each chord (G)",
        )
        self.scale_text = ft.Text("", size=13, color=ft.Colors.TERTIARY, italic=True)
        self.transport = ft.Container(
            visible=False,
            padding=ft.Padding.symmetric(horizontal=12, vertical=8),
            bgcolor=ft.Colors.SURFACE_CONTAINER_LOW,
            border=ft.Border.only(top=ft.BorderSide(1, ft.Colors.OUTLINE_VARIANT)),
            content=ft.Row(
                wrap=True, spacing=12, run_spacing=8,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                controls=[
                    ft.Row([self.play_button, self.time_text,
                            ft.Container(self.play_progress, width=220)],
                           spacing=10, tight=True, vertical_alignment=ft.CrossAxisAlignment.CENTER),
                    ft.Row([self.play_mode_dd, self.swing_dd, self.speed_dd], spacing=8, tight=True),
                    ft.Row([self.scales_switch, self.scale_text], spacing=8, tight=True,
                           vertical_alignment=ft.CrossAxisAlignment.CENTER),
                ],
            ),
        )
        self.toolbar = ft.Container(
            padding=ft.Padding.only(left=8, right=12, top=8, bottom=8),
            border=ft.Border.only(bottom=ft.BorderSide(1, ft.Colors.OUTLINE_VARIANT)),
            content=ft.Row(
                wrap=True,
                run_spacing=8,
                alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                controls=[
                    ft.Row(
                        tight=True,
                        controls=[
                            self.library_button,
                            ft.Column([self.title_text, self.subtitle_text, self.info_row],
                                      spacing=2, tight=True),
                        ],
                    ),
                    ft.Row(
                        tight=True,
                        wrap=True,
                        spacing=12,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        controls=[self.mode_button, self.instrument_dd, self.chord_style_dd, transpose_box,
                                  zoom_box, self.theme_button, self.fullscreen_button],
                    ),
                ],
            ),
        )

        # A Column rather than a ListView: scroll_to(scroll_key=…) needs every item built.
        self.content = ft.Column(expand=True, spacing=16, scroll=ft.ScrollMode.AUTO,
                                 horizontal_alignment=ft.CrossAxisAlignment.STRETCH)
        self.progress = ft.ProgressBar(visible=False)
        self.viewer = ft.Column(
            expand=True, spacing=0, horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
            controls=[self.toolbar, self.progress,
                      ft.Container(self.content, expand=True, padding=ft.Padding.all(16)),
                      self.transport],
        )
        # Follow-along: what the highlight is drawn on in the current view
        self._sheet_pages: list[dict] = []  # per page: highlight, size, boxes, offset
        self._chart_cells: dict[str, tuple[ft.Container, str, object]] = {}  # bar id -> (cell, row key, bg)
        self._content_heights: dict[int, float] = {}
        self._highlighted: str | None = None
        self._scrolled_to: tuple | None = None

        self.divider = ft.VerticalDivider(width=1)
        p.add(ft.Row(expand=True, spacing=0, vertical_alignment=ft.CrossAxisAlignment.STRETCH,
                     controls=[self.sidebar, self.divider, self.viewer]))
        self.populate_list(self.state.songs)
        self.show_welcome()
        self.apply_layout()

    def _group(self, label: str, controls: list[ft.Control]) -> ft.Control:
        return ft.Container(
            border=ft.Border.all(1, ft.Colors.OUTLINE_VARIANT),
            border_radius=24,
            padding=ft.Padding.symmetric(horizontal=4),
            tooltip=label,
            content=ft.Row(controls, spacing=0, tight=True),
        )

    # ---------------------------------------------------------------- library
    def populate_list(self, songs: list[SongInfo]) -> None:
        titles = Counter(s.title.casefold() for s in self.state.songs)
        current = self.state.current.id if self.state.current else None
        tiles = []
        for s in songs:
            details = [d for d in (s.composer, s.key, s.time) if d]
            if titles[s.title.casefold()] > 1 and s.collection:
                details.append(f"Vol. {s.collection}")
            tiles.append(
                ft.ListTile(
                    title=ft.Text(s.title, weight=ft.FontWeight.W_500, max_lines=1,
                                  overflow=ft.TextOverflow.ELLIPSIS),
                    subtitle=ft.Text(" · ".join(details), size=12, max_lines=1,
                                     overflow=ft.TextOverflow.ELLIPSIS),
                    trailing=ft.Icon(ft.Icons.LYRICS_OUTLINED, size=18, tooltip="Has lyrics")
                    if s.has_lyrics else None,
                    selected=s.id == current,
                    dense=True,
                    shape=ft.RoundedRectangleBorder(radius=12),
                    data=s,
                    on_click=self.on_song_click,
                )
            )
        self.song_list.controls = tiles
        n = len(songs)
        self.count_text.value = f"{n} tune{'s' if n != 1 else ''}"

    # ------------------------------------------------------- adding to library
    async def add_files(self, e=None) -> None:
        files = await self.file_picker.pick_files(
            dialog_title="Add MusicXML files to the library",
            file_type=ft.FilePickerFileType.CUSTOM,
            allowed_extensions=[x.lstrip(".") for x in SCORE_EXTENSIONS],
            allow_multiple=True, with_data=True,
        )
        if not files:
            return

        def job() -> ImportResult:
            picked = []
            for f in files:
                data = f.bytes
                if data is None and f.path:  # desktop may hand over the path only
                    data = Path(f.path).read_bytes()
                if data is not None:
                    picked.append((f.name, data))
            return import_files(picked, self.library)

        await self._import(job)

    async def add_folder(self, e=None) -> None:
        folder = await self.file_picker.get_directory_path(
            dialog_title="Add a folder of MusicXML files (sub-folders included)")
        if not folder:
            return
        root = Path(folder)
        await self._import(lambda: import_paths(find_scores(root), self.library, root=root))

    async def _import(self, job) -> None:
        """Run an import off the UI thread, then refresh the library and report."""
        self.progress.visible = True
        self.page.update()
        try:
            result = await asyncio.to_thread(job)
        except Exception as exc:  # e.g. an unreadable folder: report, don't crash
            result = ImportResult(errors=[str(exc) or type(exc).__name__])
        self.progress.visible = False
        self.state.songs = self.library.songs()
        self.populate_list(search(self.state.songs, self.search_field.value or ""))
        message = result.summary()
        if result.errors:
            message += f" {result.errors[0]}" + (" …" if len(result.errors) > 1 else "")
        self.page.show_dialog(ft.SnackBar(ft.Text(message), duration=6000))
        added = [s for s in self.state.songs if s.id in result.added]
        if len(added) == 1:
            await self.open_song(added[0])  # one new tune: show it
        elif self.state.current is None:
            self.show_welcome()
        self.page.update()

    def on_search(self, e: ft.Event[ft.TextField]) -> None:
        self.populate_list(search(self.state.songs, self.search_field.value or ""))
        self.page.update()

    async def on_search_submit(self, e) -> None:
        matches = search(self.state.songs, self.search_field.value or "")
        if matches:
            await self.open_song(matches[0])

    async def on_song_click(self, e: ft.Event[ft.ListTile]) -> None:
        await self.open_song(e.control.data)

    async def open_song(self, song: SongInfo) -> None:
        self.state.current = song
        for tile in self.song_list.controls:
            tile.selected = tile.data.id == song.id
        if self.narrow:
            self.set_library(False)  # narrow screen: give the music the room
        await self.render()

    # -------------------------------------------------------------- rendering
    @property
    def dark(self) -> bool:
        mode = self.page.theme_mode
        if mode == ft.ThemeMode.SYSTEM:
            return self.page.platform_brightness == ft.Brightness.DARK
        return mode == ft.ThemeMode.DARK

    async def render(self) -> None:
        """Engrave the current song with the current settings, off the UI thread."""
        song = self.state.current
        if song is None:
            return
        self.state.render_token += 1
        token = self.state.render_token
        self.stop_playback()  # the music on screen is about to change
        self.progress.visible = True
        self.update_header()
        self.page.update()

        shift, zoom, page_width = self.state.total_shift, self.state.zoom, self._page_width()
        chord_style = self.state.chord_style
        ink = "#e6e1e5" if self.dark else "#000000"

        def work() -> Rendered:
            try:
                xml = self.library.musicxml(song.id)
                eng = self.engraver.load(xml, transpose=shift, zoom=zoom, page_width=page_width,
                                         chord_style=chord_style)
                bars = chart_from_mei(eng.mei)
                pages = [self.engraver.render_page(i, ink=ink) for i in range(1, eng.pages + 1)]
                return Rendered(song, pages, bars, shift, page_width, timeline=eng.timeline,
                                melody=eng.melody, tempos=eng.tempos, guides=scale_guides(bars))
            except Exception as exc:  # a bad file must not take the app down
                return Rendered(song, [], [], shift, page_width, error=str(exc) or type(exc).__name__)

        result = await asyncio.to_thread(work)
        if token != self.state.render_token:
            return  # a newer request superseded this one
        self.state.rendered = result
        self.progress.visible = False
        self.play_button.disabled = not result.playable
        self.show_content()
        self.page.update()

    def update_header(self) -> None:
        s = self.state.current
        if s is None:
            return
        self.title_text.value = s.title
        credits = s.composer or "Unknown composer"
        if s.arranger:
            credits += f"  ·  arr. {s.arranger}"
        self.subtitle_text.value = credits
        chips = []
        if s.key:
            key = transpose_key(s.key, self.state.total_shift)
            label = f"Key {key}" if key == s.key else f"Key {key}  (concert {s.key})"
            chips.append(self._chip(ft.Icons.KEY, label))
        if s.time:
            chips.append(self._chip(ft.Icons.TIMER_OUTLINED, s.time))
        if s.tempo:
            chips.append(self._chip(ft.Icons.SPEED, f"♩ = {s.tempo}"))
        if s.has_lyrics:
            chips.append(self._chip(ft.Icons.LYRICS_OUTLINED, "Lyrics"))
        if s.collection:
            chips.append(self._chip(ft.Icons.LIBRARY_BOOKS_OUTLINED, f"Vol. {s.collection}"))
        self.info_row.controls = chips
        self.shift_text.value = f"{self.state.shift:+d}" if self.state.shift else "0"

    def _chip(self, icon: ft.IconData, text: str) -> ft.Control:
        return ft.Container(
            bgcolor=ft.Colors.SECONDARY_CONTAINER,
            border_radius=12,
            padding=ft.Padding.symmetric(horizontal=8, vertical=2),
            content=ft.Row(
                [ft.Icon(icon, size=14, color=ft.Colors.ON_SECONDARY_CONTAINER),
                 ft.Text(text, size=12, color=ft.Colors.ON_SECONDARY_CONTAINER)],
                spacing=4,
                tight=True,
            ),
        )

    def show_welcome(self) -> None:
        self.content.controls = [
            ft.Container(
                padding=ft.Padding.only(top=120),
                alignment=ft.Alignment.CENTER,
                content=ft.Column(
                    horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                    spacing=12,
                    controls=[
                        ft.Icon(ft.Icons.PIANO, size=72, color=ft.Colors.PRIMARY),
                        ft.Text("Pick a tune from the library" if self.state.songs else
                                "Your library is empty", size=24, weight=ft.FontWeight.W_500),
                        ft.Text("Add MusicXML leadsheets with the add button above the "
                                "library (.musicxml, .xml or .mxl).",
                                color=ft.Colors.ON_SURFACE_VARIANT, visible=not self.state.songs),
                        ft.Text(
                            f"{len(self.state.songs)} leadsheets · melody, chords and lyrics, "
                            "transposed for your instrument",
                            color=ft.Colors.ON_SURFACE_VARIANT,
                            visible=bool(self.state.songs),
                        ),
                        ft.Text(
                            "Keys: / search · P play · Space pause · ↓ ↑ PgDn PgUp scroll · [ ] transpose · "
                            "S sheet · C chords · W lyrics · G scales · L library · D dark",
                            size=12,
                            color=ft.Colors.ON_SURFACE_VARIANT,
                        ),
                    ],
                ),
            )
        ]
        self.title_text.value = "TuneView"
        self.subtitle_text.value = "Leadsheets on your music stand"

    def show_content(self) -> None:
        r = self.state.rendered
        if r is None:
            return
        self._sheet_pages, self._chart_cells, self._content_heights = [], {}, {}
        self._highlighted, self._scrolled_to = None, None
        self.transport.visible = True
        if r.error:
            self.content.controls = [self._notice(ft.Icons.ERROR_OUTLINE,
                                                  f"Couldn't engrave this leadsheet: {r.error}")]
        elif self.state.mode == SHEET:
            self.content.controls = self._sheet(r)
        elif self.state.mode == LYRICS:
            self.content.controls = self._lyrics(r)
        else:
            self.content.controls = self._chart(r)
        for i, control in enumerate(self.content.controls):  # heights, for scrolling
            control.on_size_change = lambda e, i=i: self._content_heights.__setitem__(i, e.height)

    def _notice(self, icon: ft.IconData, text: str) -> ft.Control:
        return ft.Container(
            padding=ft.Padding.all(24),
            content=ft.Row([ft.Icon(icon, color=ft.Colors.ON_SURFACE_VARIANT),
                            ft.Text(text, color=ft.Colors.ON_SURFACE_VARIANT)], spacing=12),
        )

    # ------------------------------------------------------------- sheet view
    def _sheet(self, r: Rendered) -> list[ft.Control]:
        paper = ft.Colors.SURFACE_CONTAINER if self.dark else ft.Colors.WHITE
        out: list[ft.Control] = []
        if not r.song.has_melody:
            out.append(self._notice(ft.Icons.INFO_OUTLINE, "This leadsheet has no written melody."))
        for page in r.pages:
            highlight = ft.Container(
                visible=False, left=0, top=0, width=0, height=0, border_radius=4,
                bgcolor=ft.Colors.with_opacity(0.16, ft.Colors.PRIMARY),
                border=ft.Border.all(1.5, ft.Colors.with_opacity(0.6, ft.Colors.PRIMARY)),
            )
            scales = ft.Stack([], visible=False, left=0, top=0, width=0, height=0)
            info = {"highlight": highlight, "scales": scales, "boxes": page.boxes,
                    "staves": page.staves, "chords_x": page.chords_x, "aspect": page.aspect,
                    "width": 0.0, "index": len(out)}
            stack = ft.Stack(
                [ft.Image(src=page.png, fit=ft.BoxFit.FIT_WIDTH, gapless_playback=True), highlight,
                 scales],
                on_size_change=lambda e, info=info: info.__setitem__("width", e.width),
            )
            self._sheet_pages.append(info)
            out.append(
                ft.Container(
                    bgcolor=paper,
                    border_radius=6,
                    padding=ft.Padding.symmetric(horizontal=8, vertical=SHEET_PAD),
                    shadow=ft.BoxShadow(blur_radius=8, color=ft.Colors.with_opacity(0.18, ft.Colors.BLACK)),
                    content=stack,
                )
            )
        return out

    # ------------------------------------------------------------ lyrics view
    def _lyrics(self, r: Rendered) -> list[ft.Control]:
        text = self.library.lyrics(r.song.id)
        edit = ft.FilledTonalButton("Edit lyrics" if text else "Add lyrics",
                                    icon=ft.Icons.EDIT_OUTLINED if text else ft.Icons.ADD,
                                    on_click=self.open_lyrics_editor)
        out: list[ft.Control] = [ft.Row([edit], alignment=ft.MainAxisAlignment.END)]
        if not text:
            hint = "No lyrics yet. Add them to read them here in large print."
            if r.song.has_lyrics:
                hint += " (This leadsheet has lyrics under its melody: see Sheet.)"
            out.append(self._notice(ft.Icons.LYRICS_OUTLINED, hint))
            return out
        size = self.state.zoom * 0.55  # lyrics follow the zoom setting, like the chord chart
        blocks: list[ft.Control] = []
        for verse in verses(text):
            lines: list[ft.Control] = []
            if verse.heading:
                lines.append(ft.Text(verse.heading.upper(), size=size * 0.62, weight=ft.FontWeight.W_700,
                                     color=ft.Colors.PRIMARY, style=ft.TextStyle(letter_spacing=1.5)))
            lines += [ft.Text(line, size=size, selectable=True) for line in verse.lines]
            blocks.append(ft.Column(lines, spacing=size * 0.18, tight=True))
        out.append(ft.Row([ft.Container(ft.Column(blocks, spacing=size * 1.1, tight=True),
                                        width=min(900.0, self._chart_width()),
                                        padding=ft.Padding.symmetric(horizontal=8))],
                          alignment=ft.MainAxisAlignment.CENTER))
        return out

    def open_lyrics_editor(self, e=None) -> None:
        song = self.state.current
        if song is None:
            return
        field = ft.TextField(
            value=self.library.lyrics(song.id), multiline=True, min_lines=14, max_lines=22,
            autofocus=True, text_size=15,
            hint_text="Paste or type the lyrics. Leave a blank line between verses; "
                      "a line like [Chorus] or Verse 2: on top of a verse becomes its heading.",
        )

        def close(save: bool):
            def handler(e=None) -> None:
                if save:
                    self.library.set_lyrics(song.id, field.value or "")
                self.state.editing = False
                self.page.pop_dialog()
                if save and self.state.current is song and self.state.mode == LYRICS:
                    self.show_content()
                self.page.update()
            return handler

        dialog = ft.AlertDialog(
            title=ft.Text(f"Lyrics · {song.title}"),
            content=ft.Container(field, width=640),
            actions=[ft.TextButton("Cancel", on_click=close(False)),
                     ft.FilledButton("Save", on_click=close(True))],
            on_dismiss=lambda e: setattr(self.state, "editing", False),
        )
        self.state.editing = True
        self.page.show_dialog(dialog)

    # ------------------------------------------------------------- chart view
    @staticmethod
    def _chart_bars(r: Rendered) -> list[ChartBar]:
        """The bars the chart view prints, in order (what the lyrics align to)."""
        return [b for b in r.bars if b.chords or b.rest_bars or b.number not in ("0", "")]

    def _chart_lyric_counts(self, r: Rendered, bars: list[ChartBar],
                            words: list[str]) -> list[int]:
        """The saved words-per-bar split, or an even spread when it no longer fits."""
        saved = self.library.lyrics_alignment(r.song.id) if words else None
        if saved:
            saved_bars, counts = saved
            # MEI xml:ids change from one engraving to the next; bar numbers don't.
            if (saved_bars == [b.number for b in bars] and len(counts) == len(bars)
                    and sum(counts) == len(words)):
                return counts
        return spread_words(len(words), len(bars))

    def _chart_lyrics_toolbar(self) -> ft.Control:
        return ft.Row(
            [
                ft.Switch(label="Lyrics under chords", value=self.state.chart_lyrics,
                          on_change=self.on_chart_lyrics),
                ft.FilledTonalButton("Align lyrics", icon=ft.Icons.FORMAT_ALIGN_LEFT,
                                     on_click=self.open_alignment_editor),
            ],
            alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            wrap=True,
        )

    def _chart(self, r: Rendered) -> list[ft.Control]:
        bars = self._chart_bars(r)
        if not any(b.chords for b in bars):
            return [self._notice(ft.Icons.INFO_OUTLINE,
                                 "This leadsheet has no chord symbols — switch to Sheet.")]
        size = self.state.zoom * 0.62  # chord font size follows the zoom setting
        per_line = self._bars_per_line()
        # The lyrics run word by word across the tune's bars; each bar shows its own.
        words = lyric_words(self.library.lyrics(r.song.id))
        counts = self._chart_lyric_counts(r, bars, words) if words else []
        parts = chunks(words, counts) if words else []
        show_lyrics = bool(parts) and self.state.chart_lyrics
        lines: list[list[ChartBar]] = [[]]
        for bar in bars:
            line = lines[-1]
            starts_section = bar.repeat_start or bool(bar.marks)
            if line and (len(line) == per_line or starts_section):
                lines.append(line := [])
            line.append(bar)
            if bar.final or bar.double or bar.repeat_end:
                lines.append([])
        rows: list[ft.Control] = []
        if parts:  # lyrics exist: offer the toggle and the alignment editor
            rows.append(self._chart_lyrics_toolbar())
        index = 0
        for n, line in enumerate(ln for ln in lines if ln):
            row_key = f"chart-row-{n}"
            cells: list[ft.Control] = []
            for b in line:
                lyric = " ".join(parts[index]) if show_lyrics and index < len(parts) else ""
                cell = self._bar_cell(b, size, lyric)
                if b.id:
                    self._chart_cells[b.id] = (cell, row_key, cell.bgcolor)
                cells.append(cell)
                index += 1
            cells += [ft.Container(expand=1) for _ in range(per_line - len(line))]
            rows.append(ft.Row(cells, spacing=0, vertical_alignment=ft.CrossAxisAlignment.STRETCH,
                               intrinsic_height=True, key=ft.ScrollKey(row_key)))
        return rows

    def on_chart_lyrics(self, e: ft.Event[ft.Switch]) -> None:
        self.state.chart_lyrics = bool(e.control.value)
        self.show_content()
        self.page.update()

    def open_alignment_editor(self, e=None) -> None:
        """Move the lyric words from bar to bar on the chord chart."""
        r, song = self.state.rendered, self.state.current
        if r is None or song is None:
            return
        bars = self._chart_bars(r)
        words = lyric_words(self.library.lyrics(song.id))
        if not words or not bars:
            return
        counts = self._chart_lyric_counts(r, bars, words)
        fields: list[ft.TextField] = []
        error = ft.Text("", size=12, color=ft.Colors.ERROR)

        def label(bar: ChartBar) -> str:
            chords = " · ".join(c.text for c in bar.chords) if bar.chords else "%"
            return f"{bar.number}   {chords}"

        def refill(new_counts: list[int]) -> None:
            for field, part in zip(fields, chunks(words, new_counts)):
                field.value = " ".join(part)
            error.value = ""
            self.page.update()

        def close(e=None) -> None:
            self._alignment = None
            self.state.editing = False
            self.page.pop_dialog()
            self.page.update()

        def dismiss(e=None) -> None:
            self._alignment = None
            self.state.editing = False

        def save(e=None) -> None:
            moved = [w for field in fields for w in (field.value or "").split()]
            if moved != words:
                error.value = ("The words must stay the same as on the Lyrics page — move "
                               "whole words between bars, or edit the lyrics there.")
                self.page.update()
                return
            self.library.set_lyrics_alignment(
                song.id, [b.number for b in bars],
                [len((field.value or "").split()) for field in fields],
            )
            self._alignment = None
            self.state.editing = False
            self.page.pop_dialog()
            if self.state.current is song and self.state.mode == CHART:
                self.show_content()
            self.page.update()

        rows: list[ft.Control] = []
        for index, (bar, part) in enumerate(zip(bars, chunks(words, counts))):
            field = ft.TextField(
                value=" ".join(part), dense=True, text_size=13, expand=True,
                content_padding=ft.Padding.symmetric(horizontal=8, vertical=6),
                on_focus=lambda e, i=index: self._align_focus(i),
                on_blur=lambda e, i=index: self._align_blur(i),
                on_selection_change=lambda e, i=index: self._align_selection(i, e),
            )
            fields.append(field)
            rows.append(ft.Row([
                ft.Container(
                    ft.Text(label(bar), size=12, color=ft.Colors.ON_SURFACE_VARIANT,
                            no_wrap=True, overflow=ft.TextOverflow.ELLIPSIS),
                    width=190),
                field,
            ], spacing=8, vertical_alignment=ft.CrossAxisAlignment.CENTER))
        note = ft.Text(
            "Move whole words between the bars to line them up with the tune. Enter splits at "
            "the cursor and pushes the later bars forward (at a bar's start it leaves an empty "
            "bar); Backspace/Delete at a bar's edges join it with a neighbour and pull the later "
            "bars back. The last bar takes the overflow, and the words stay as they are on the "
            "Lyrics page.",
            size=12, color=ft.Colors.ON_SURFACE_VARIANT)
        width = min(880, max(320, (self.page.width or 900) - 160))
        editor = ft.Column(rows, scroll=ft.ScrollMode.AUTO, height=440, spacing=6, tight=True)
        dialog = ft.AlertDialog(
            title=ft.Text(f"Lyrics alignment · {song.title}"),
            content=ft.Container(ft.Column([note, editor, error], spacing=8, tight=True),
                                 width=width),
            actions=[
                ft.TextButton("Auto-align",
                              on_click=lambda e: refill(spread_words(len(words), len(bars)))),
                ft.TextButton("Cancel", on_click=close),
                ft.FilledButton("Save", on_click=save),
            ],
            on_dismiss=dismiss,
        )
        self._alignment = {"fields": fields, "focused": None, "sel": {}, "error": error}
        self.state.editing = True
        self.page.show_dialog(dialog)

    # -- keyboard editing across bar boundaries in the alignment editor -----
    def _align_focus(self, index: int) -> None:
        if self._alignment is not None:
            self._alignment["focused"] = index

    def _align_blur(self, index: int) -> None:
        """The caret left the bars (for a button or the dialog), so keys aren't lyrics keys."""
        if self._alignment is not None and self._alignment.get("focused") == index:
            self._alignment["focused"] = None

    def _align_selection(self, index: int, e) -> None:
        if self._alignment is not None:
            self._alignment["sel"][index] = e.selection

    def _align_caret(self, index: int, field: ft.TextField) -> tuple[bool, int]:
        """(is a caret rather than a range, caret position) for a bar's field."""
        sel = self._alignment["sel"].get(index) if self._alignment else None
        if sel is None:
            sel = field.selection
        text = field.value or ""
        if sel is None:
            return True, len(text)
        if sel.base_offset != sel.extent_offset:
            return False, 0  # a selection: let the text field handle the key normally
        return True, min(sel.base_offset, len(text))

    async def _align_move_focus(self, fields: list[ft.TextField], index: int, caret: int) -> None:
        """Put the caret back in the right field after a split or join."""
        if self._alignment is None:
            return
        selection = ft.TextSelection(caret, caret)
        self._alignment["focused"] = index
        self._alignment["sel"][index] = selection
        self._alignment["error"].value = ""
        await fields[index].focus()
        fields[index].selection = selection
        self.page.update()

    async def _align_key(self, e: ft.KeyboardEvent) -> bool:
        """Enter splits a bar, Backspace/Delete join it with a neighbour.

        Splitting pushes the later bars forward and joining pulls them back, so
        a gap only ends up where the user asked for it (at the start of a bar)
        or at the end of the chart, never in the middle of the lyrics.
        """
        ed = self._alignment
        if ed is None or ed.get("focused") is None:
            return False
        index = ed["focused"]
        fields = ed["fields"]
        field = fields[index]
        text = field.value or ""
        collapsed, caret = self._align_caret(index, field)
        if not collapsed:
            return False  # a selected range: Backspace/Delete should just delete it
        lines = [f.value or "" for f in fields]

        def apply(new_lines: list[str]) -> None:
            for f, value in zip(fields, new_lines):
                f.value = value

        if e.key == "Enter":
            _, after = split_words(text, caret)
            if not after or index + 1 >= len(fields):
                return False
            apply(split_bars(lines, index, caret))
            await self._align_move_focus(fields, index + 1, 0)
            return True
        if e.key == "Backspace":
            if caret != 0 or index == 0:
                return False
            before_text = (fields[index - 1].value or "").rstrip()
            apply(join_bars(lines, index))
            caret_in_previous = len(before_text) + (1 if before_text and text.strip() else 0)
            await self._align_move_focus(fields, index - 1, caret_in_previous)
            return True
        if e.key == "Delete":
            if caret != len(text) or index + 1 >= len(fields):
                return False
            apply(join_bars(lines, index + 1))
            await self._align_move_focus(fields, index, len(text.rstrip()))
            return True
        return False

    def _bar_cell(self, bar: ChartBar, size: float, lyric: str = "") -> ft.Control:
        line = ft.Colors.ON_SURFACE
        faint = ft.Colors.ON_SURFACE_VARIANT
        thick = 4 if bar.final else 1.5
        left = ft.BorderSide(4 if bar.repeat_start else 1.5, line)
        right = ft.BorderSide(4 if bar.repeat_end else thick, line)

        top_labels: list[ft.Control] = [ft.Text(bar.number, size=10, color=faint)]
        if bar.ending:
            top_labels.append(ft.Text(f"{bar.ending}.", size=13, weight=ft.FontWeight.BOLD))
        for mark in bar.marks:
            label = {"segno": "𝄋 Segno", "coda": "𝄌 Coda"}.get(mark, mark)
            top_labels.append(
                ft.Container(
                    bgcolor=ft.Colors.TERTIARY_CONTAINER, border_radius=6,
                    padding=ft.Padding.symmetric(horizontal=6),
                    content=ft.Text(label, size=12, weight=ft.FontWeight.W_600,
                                    color=ft.Colors.ON_TERTIARY_CONTAINER),
                )
            )

        if bar.rest_bars:
            body: ft.Control = ft.Text(f"{bar.rest_bars} bars rest", size=size * 0.6, italic=True,
                                       color=faint)
        elif not bar.chords:  # same chord as the previous bar
            body = ft.Text("%", size=size, color=faint, text_align=ft.TextAlign.CENTER)
        else:
            body = self._chord_row(bar, size)

        stack_rows: list[ft.Control] = [ft.Row(top_labels, spacing=6, height=20), body]
        if lyric:
            stack_rows.append(
                ft.Text(lyric, size=max(10.0, size * 0.46), color=faint)
            )
        if bar.directions:
            stack_rows.append(ft.Text(" · ".join(bar.directions), size=12, italic=True,
                                      color=faint, text_align=ft.TextAlign.RIGHT))
        inner: list[ft.Control] = [
            ft.Column(stack_rows, spacing=2, expand=True,
                      horizontal_alignment=ft.CrossAxisAlignment.STRETCH)
        ]
        if bar.repeat_start:
            inner.insert(0, self._repeat_dots())
        if bar.repeat_end:
            inner.append(self._repeat_dots())

        return ft.Container(
            expand=1,
            border=ft.Border(left=left, right=right),
            border_radius=0,
            padding=ft.Padding.only(left=6 if bar.repeat_start else 12,
                                    right=6 if bar.repeat_end else 12, top=2, bottom=8),
            bgcolor=ft.Colors.with_opacity(0.04, ft.Colors.PRIMARY) if bar.ending else None,
            content=ft.Row(inner, spacing=8, vertical_alignment=ft.CrossAxisAlignment.CENTER),
        )

    def _chart_width(self) -> float:
        width = self.page.width or 1200
        if self.state.library_open and not self.narrow:
            width -= SIDEBAR_WIDTH + 1
        return width - 48

    def _bars_per_line(self) -> int:
        return 4 if self._chart_width() >= 640 else 2

    def _cell_width(self) -> float:
        """Usable width of one bar in the chart, from the window and sidebar sizes."""
        return max(80.0, self._chart_width() / self._bars_per_line() - 28)

    @staticmethod
    def _chord_width(text: str, size: float) -> float:
        """Rough rendered width of a chord symbol, for layout decisions."""
        root, quality, bass = split_chord(text)
        return size * (0.7 * len(root) + 0.36 * len(quality) + 0.5 * (len(bass) + bool(bass)))

    def _chord_row(self, bar: ChartBar, size: float) -> ft.Control:
        """Chords placed by beat when they fit; otherwise packed and shrunk to fit."""
        cell, gap = self._cell_width(), size * 0.4
        beats = [min(max(1.0, c.beat), bar.beats + 0.99) for c in bar.chords]
        ends = beats[1:] + [bar.beats + 1]
        fits = all(
            self._chord_width(c.text, size) + gap <= cell * (end - b) / bar.beats
            for c, b, end in zip(bar.chords, beats, ends, strict=True)
        )
        if fits:
            slots: list[ft.Control] = []
            if beats[0] > 1:
                slots.append(ft.Container(expand=round((beats[0] - 1) * 4)))
            for chord, b, end in zip(bar.chords, beats, ends, strict=True):
                slots.append(ft.Container(expand=max(1, round((end - b) * 4)),
                                          content=self._chord_text(chord.text, size)))
            return ft.Row(slots, spacing=0)
        needed = sum(self._chord_width(c.text, size) for c in bar.chords) + gap * (len(bar.chords) - 1)
        scale = min(1.0, cell / needed)
        return ft.Row([self._chord_text(c.text, size * scale) for c in bar.chords],
                      spacing=gap * scale, tight=True)

    @staticmethod
    def _repeat_dots() -> ft.Control:
        def dot() -> ft.Control:
            return ft.Container(width=6, height=6, border_radius=3, bgcolor=ft.Colors.ON_SURFACE)

        return ft.Container(ft.Column([dot(), dot()], spacing=8, tight=True),
                            padding=ft.Padding.only(top=18))

    def _chord_text(self, text: str, size: float) -> ft.Control:
        root, quality, bass = split_chord(text)
        spans = [ft.TextSpan(quality, ft.TextStyle(size=size * 0.62, weight=ft.FontWeight.W_500))]
        if bass:
            spans.append(ft.TextSpan(f"/{bass}", ft.TextStyle(size=size * 0.8)))
        return ft.Text(root, size=size, weight=ft.FontWeight.W_700, spans=spans, no_wrap=True,
                       max_lines=1, overflow=ft.TextOverflow.VISIBLE)

    # --------------------------------------------------------------- playback
    def _swing_ratio(self, r: Rendered, notes) -> float:
        if not r.song.time.endswith(("/4", "/2")):
            return 0.5  # compound meters (6/8, 12/8) already move in threes
        ratio = SWING.get(self.state.swing, 0.5)
        if ratio is None:  # Auto: guess from the tune
            ratio = SWING["Medium"] if should_swing(notes, r.song.time) else 0.5
        return ratio

    def _is_playing(self) -> bool:
        if self._browser is not None:
            return self._browser.playing
        return self.player.playing

    def _is_paused(self) -> bool:
        if self._browser is not None:
            return self._browser.paused
        return self.player.paused

    def _playback_position(self) -> float:
        if self._browser is not None:
            return self._browser.position
        return self.player.position

    def _playback_length(self) -> float:
        if self._browser is not None:
            return self._browser.length
        return self.player.length

    def _finish_playback(self) -> None:
        """The browser finished the tune: reset the transport and the highlight."""
        self.stop_playback()
        try:
            self.page.update()
        except Exception:  # the page went away
            pass

    def _shutdown_playback(self) -> None:
        """The window/tab is going away: stop the device and forget the browser player."""
        self.player.stop()
        if self._browser is not None:
            self._browser.abandon()
            self._browser = None

    async def toggle_playback(self, e=None) -> None:
        if self._is_playing():
            self.stop_playback()
            self.page.update()
            return
        r = self.state.rendered
        if r is None or not r.playable or self.play_button.disabled:
            return
        # The score may be written for a transposing instrument; play what it
        # sounds like: concert pitch plus the player's own transposition.
        sounding = -(r.shift - self.state.shift)
        mode, speed = self.state.play_mode, self.state.speed

        def prepare():
            tune = melody(r.melody, transpose=sounding)
            notes = tune if mode in ("melody", "both") else []
            if mode in ("both", "chords"):
                bars = {b.id: b for b in r.bars}
                notes = notes + accompaniment(r.timeline, bars, transpose=sounding)
            tempo = tempo_map(r.tempos)
            ratio = self._swing_ratio(r, tune)
            performance = perform(notes, tempo, swing_ratio=ratio, speed=speed)
            return synthesize(performance), bar_times(r.timeline, tempo, swing_ratio=ratio, speed=speed)

        self.play_button.icon = ft.Icons.HOURGLASS_TOP
        self.page.update()
        pcm, times = await asyncio.to_thread(prepare)
        if r is not self.state.rendered:
            return  # the song or its settings changed while we were synthesizing
        if not len(pcm):
            self.play_button.icon = ft.Icons.PLAY_ARROW_ROUNDED
            what = "melody" if mode == "melody" else "chords"
            self.page.show_dialog(ft.SnackBar(ft.Text(f"This leadsheet has no {what} to play.")))
            self.page.update()
            return
        try:
            if self.page.web:  # each browser hears it locally; the host stays silent
                url = _audio_asset(pcm)
                browser = BrowserAudio(self.page, on_finished=self._finish_playback)
                self._browser = browser
                await browser.play(url, len(pcm) / SAMPLE_RATE)
                if self._browser is not browser:  # stopped while the browser was starting
                    return
            else:
                self.player.play(pcm)
        except Exception as exc:  # no audio device, bad source, etc.
            self.stop_playback()
            self.play_button.icon = ft.Icons.PLAY_ARROW_ROUNDED
            self.page.show_dialog(ft.SnackBar(ft.Text(f"Can't play audio: {exc}")))
            self.page.update()
            return
        self.play_button.icon = ft.Icons.STOP_ROUNDED
        self.play_button.tooltip = "Stop (P)"
        self.page.update()
        self.page.run_task(self._follow_playback, times)

    async def _follow_playback(self, times: list[tuple[float, str]]) -> None:
        """While playing: tick the clock and progress, highlight the bar being played."""
        starts = [t for t, _ in times]
        while self._is_playing() and self._playback_position() < self._playback_length():
            pos, length = self._playback_position(), self._playback_length()
            self.time_text.value = f"{_mmss(pos)} / {_mmss(length)}"
            if self._is_paused():
                self.time_text.value = f"Paused {_mmss(pos)}"
            self.play_progress.value = pos / length if length else 0
            i = bisect.bisect_right(starts, pos) - 1
            try:
                if i >= 0:
                    await self.highlight_bar(times[i][1])
                self.page.update()
            except Exception:  # the session went away (window or tab closed)
                self.stop_playback()
                return
            await asyncio.sleep(0.1)
        if self._is_playing():  # reached the end (not stopped by the user)
            self.stop_playback()
            self.page.update()

    async def highlight_bar(self, bar_id: str | None) -> None:
        """Mark the bar being played and keep it in view, in either view."""
        if bar_id == self._highlighted:
            return
        self._highlighted = bar_id
        self.show_scales(bar_id)
        # Chord chart: tint the cell, scroll its line into view
        for cell, _, bg in self._chart_cells.values():
            cell.bgcolor = bg
        if bar_id in self._chart_cells:
            cell, row_key, _ = self._chart_cells[bar_id]
            cell.bgcolor = ft.Colors.with_opacity(0.22, ft.Colors.PRIMARY)
            if self._scrolled_to != row_key:
                self._scrolled_to = row_key
                await self.content.scroll_to(scroll_key=row_key, duration=300)
        # Sheet: draw a box over the bar, scroll when it moves to a new system
        for info in self._sheet_pages:
            box = info["boxes"].get(bar_id) if bar_id else None
            hl = info["highlight"]
            if box is None or not info["width"]:
                hl.visible = False
                continue
            w = info["width"]
            h = w * info["aspect"]
            left, top, right, bottom = box
            staff = bottom - top
            top_px = (top - staff * 1.6) * h  # include the chord symbols above
            hl.left, hl.top = left * w - 4, top_px
            hl.width, hl.height = (right - left) * w + 8, (bottom - top + staff * 2.2) * h
            hl.visible = True
            system = (info["index"], round(top, 3))
            if self._scrolled_to != system:
                self._scrolled_to = system
                before = sum(self._content_heights.get(i, 0) for i in range(info["index"]))
                offset = before + 16 * info["index"] + SHEET_PAD + top_px
                await self.content.scroll_to(offset=max(0.0, offset - 40), duration=300)

    async def toggle_pause(self) -> None:
        """Pause or resume what's playing; the bar stays highlighted while paused."""
        if self._browser is not None:
            if self._browser.paused:
                await self._browser.resume()
            else:
                await self._browser.pause()
        elif self.player.paused:
            self.player.resume()
        else:
            self.player.pause()
        self.page.update()

    def stop_playback(self) -> None:
        browser, self._browser = self._browser, None
        if browser is not None:
            try:
                self.page.run_task(browser.close)  # a coroutine function, not a coroutine
            except Exception:  # the page is gone
                browser.abandon()
        self.player.stop()
        self.play_button.icon = ft.Icons.PLAY_ARROW_ROUNDED
        self.play_button.tooltip = "Play (P)"
        self.time_text.value = "0:00"
        self.play_progress.value = 0
        for cell, _, bg in self._chart_cells.values():
            cell.bgcolor = bg
        for info in self._sheet_pages:
            info["highlight"].visible = False
        self.show_scales(None)
        self._highlighted, self._scrolled_to = None, None

    # ----------------------------------------------------------------- scales
    def show_scales(self, bar_id: str | None) -> None:
        """Draw the suggested scales over the bar being played (Sheet view) and
        name them in the transport bar; clear them when bar_id is None."""
        r = self.state.rendered
        bar = next((b for b in r.bars if b.id == bar_id), None) if r and bar_id else None
        if not self.state.show_scales or bar is None:
            bar = None
        for info in self._sheet_pages:
            layer = info["scales"]
            layer.visible, layer.controls = False, []
            if bar is None or bar.id not in info["staves"] or not info["width"]:
                continue
            w = info["width"]
            h = w * info["aspect"]
            marks = scale_marks(bar, r.guides, info["staves"][bar.id], info["boxes"][bar.id][2],
                                info["chords_x"], w, h)
            layer.width, layer.height = w, h
            layer.controls = [self._mark(m) for m in marks]
            layer.visible = bool(marks)
        self.scale_text.value = "   ".join(
            f"{c.text}: {r.guides[c.id].name}" for c in bar.chords if c.id in r.guides
        ) if bar else ""

    @staticmethod
    def _mark(m: Mark) -> ft.Control:
        ink = ft.Colors.TERTIARY
        if m.kind == "accidental":
            return ft.Text(m.text, left=m.x, top=m.y, size=m.height, no_wrap=True,
                           color=ft.Colors.with_opacity(0.85, ink))
        opacity = {"root": 0.85, "note": 0.55, "ledger": 0.6}[m.kind]
        return ft.Container(left=m.x, top=m.y, width=m.width, height=m.height,
                            border_radius=m.height / 2 if m.kind != "ledger" else 0,
                            bgcolor=ft.Colors.with_opacity(opacity, ink))

    async def on_scales(self, e=None) -> None:
        self.state.show_scales = bool(self.scales_switch.value)
        self.show_scales(self._highlighted)
        self.page.update()

    async def toggle_scales(self) -> None:
        self.scales_switch.value = not self.scales_switch.value
        await self.on_scales()

    async def on_playback_setting(self, e) -> None:
        self.state.play_mode = self.play_mode_dd.value or "melody"
        self.state.swing = self.swing_dd.value or "Off"
        self.state.speed = float(self.speed_dd.value or 1.0)
        if self._is_playing():  # restart with the new settings
            self.stop_playback()
            await self.toggle_playback()

    # ---------------------------------------------------------------- actions
    async def on_mode(self, e) -> None:
        await self.set_mode(e.control.selected[0])

    async def set_mode(self, mode: str) -> None:
        self.state.mode = mode
        self.mode_button.selected = [mode]
        self.show_content()
        self.page.update()
        await self.content.scroll_to(offset=0)

    async def on_instrument(self, e) -> None:
        self.state.instrument = self.instrument_dd.value or "C"
        await self.render()

    async def on_chord_style(self, e) -> None:
        self.state.chord_style = self.chord_style_dd.value or "compact"
        await self.render()

    async def change_shift(self, delta: int) -> None:
        self.state.shift = max(-6, min(6, self.state.shift + delta))
        await self.render()

    async def change_zoom(self, delta: int) -> None:
        i = ZOOM_STEPS.index(min(ZOOM_STEPS, key=lambda z: abs(z - self.state.zoom)))
        self.state.zoom = ZOOM_STEPS[max(0, min(len(ZOOM_STEPS) - 1, i + delta))]
        await self.render()

    @property
    def narrow(self) -> bool:
        return bool(self.page.width) and self.page.width < NARROW

    def set_library(self, open_: bool) -> None:
        self.state.library_open = open_
        self.apply_layout()

    def apply_layout(self) -> None:
        """Side by side on wide screens; one pane at a time on narrow ones."""
        open_, narrow = self.state.library_open, self.narrow
        self.sidebar.visible = open_
        self.sidebar.width = None if narrow else SIDEBAR_WIDTH
        self.sidebar.expand = narrow
        self.divider.visible = open_ and not narrow
        self.viewer.visible = not (narrow and open_)
        self.library_button.icon = ft.Icons.MENU_OPEN if open_ else ft.Icons.MENU
        self.library_button.tooltip = "Hide library (L)" if open_ else "Show library (L)"

    def toggle_library(self, e=None) -> None:
        self.set_library(not self.state.library_open)
        if self.state.mode == CHART:
            self.show_content()  # bars got wider or narrower
        self.page.update()

    def _page_width(self) -> int:
        """Engraving page width for the current viewer: phones get fewer bars per line."""
        return int(min(Engraver.PAGE_WIDTH, max(1000, self._chart_width() * 3)))

    async def on_resize(self, e=None) -> None:
        self.apply_layout()
        r = self.state.rendered
        if r and self.state.mode == CHART:
            self.show_content()
        self.page.update()
        if r and abs(self._page_width() - r.page_width) > 0.15 * r.page_width:
            await self.render()  # re-engrave so the bars fit the new width

    async def toggle_theme(self, e=None) -> None:
        self.page.theme_mode = ft.ThemeMode.LIGHT if self.dark else ft.ThemeMode.DARK
        await self.on_theme_changed()

    async def on_theme_changed(self, e=None) -> None:
        self.theme_button.icon = ft.Icons.LIGHT_MODE_OUTLINED if self.dark else ft.Icons.DARK_MODE_OUTLINED
        self.page.update()
        await self.render()  # the notation's ink colour follows the theme

    def toggle_fullscreen(self, e=None) -> None:
        if self.page.web:
            return
        self.page.window.full_screen = not self.page.window.full_screen
        self.fullscreen_button.icon = (ft.Icons.FULLSCREEN_EXIT if self.page.window.full_screen
                                       else ft.Icons.FULLSCREEN)
        self.page.update()

    async def scroll_by(self, fraction: float) -> None:
        viewport = (self.page.height or 800) - 120
        await self.content.scroll_to(delta=viewport * fraction, duration=250)

    async def on_key(self, e: ft.KeyboardEvent) -> None:
        key = e.key
        if self._alignment is not None and await self._align_key(e):
            return  # Enter/Backspace/Delete at a bar edge split or join the bar
        if self.state.editing:
            return  # typing in the lyrics editor
        if self.state.search_focused:
            if key == "Escape":
                self.search_field.value = ""
                self.populate_list(self.state.songs)
                self.page.update()
            return
        if key == " " and self._is_playing():  # Space pauses and resumes playback
            await self.toggle_pause()
        elif key in ("Arrow Down", "Page Down", " "):  # page-turner pedals send these
            await self.scroll_by(0.85 if key != "Arrow Down" else 0.3)
        elif key in ("Arrow Up", "Page Up"):
            await self.scroll_by(-0.85 if key == "Page Up" else -0.3)
        elif key == "Home":
            await self.content.scroll_to(offset=0, duration=250)
        elif key == "/":
            await self.search_field.focus()
        elif key == "]":
            await self.change_shift(+1)
        elif key == "[":
            await self.change_shift(-1)
        elif key in ("=", "+"):
            await self.change_zoom(+1)
        elif key == "-":
            await self.change_zoom(-1)
        elif key == "S":
            await self.set_mode(SHEET)
        elif key == "C":
            await self.set_mode(CHART)
        elif key == "W":
            await self.set_mode(LYRICS)
        elif key == "L":
            self.toggle_library()
        elif key == "D":
            await self.toggle_theme()
        elif key == "P":
            await self.toggle_playback()
        elif key == "G":
            await self.toggle_scales()
        elif key == "F11":
            self.toggle_fullscreen()


def _mmss(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    return f"{m}:{s:02d}"


async def main(page: ft.Page) -> None:
    library = Library()
    if library.is_new():  # first run: start with an empty library to add music to
        library.create()
    if not library.is_ready():
        page.add(
            ft.Container(
                padding=40,
                content=ft.Text(
                    f"{library.path} was made by a different version of TuneView.\n\n"
                    "Rebuild it from your folder of MusicXML files:\n"
                    "    uv run tuneview-import /path/to/musicxml",
                    selectable=True,
                    size=16,
                ),
            )
        )
        return
    TuneView(page, library)
    page.update()


def _lan_address() -> str:
    """This machine's address on the local network, or '' if it can't be found."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))  # a route lookup; no packet is sent
            return sock.getsockname()[0]
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith(("127.", "169.254.")):
                return ip
    except OSError:
        pass
    return ""


def _web_port() -> int:
    """The port to serve on: FLET_SERVER_PORT/TUNEVIEW_PORT, else a free one."""
    for name in ("FLET_SERVER_PORT", "TUNEVIEW_PORT"):
        value = (os.environ.get(name) or "").strip()
        if value.isdigit() and int(value) > 0:
            return int(value)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("0.0.0.0", 0))
        return sock.getsockname()[1]


def _announce_web(port: int) -> None:
    """Print the addresses to open, including the local-network address."""
    print(f"TuneView web server: http://127.0.0.1:{port}/", flush=True)
    if lan := _lan_address():
        print(f"  from another computer on the local network: http://{lan}:{port}/", flush=True)


def run() -> None:
    if os.environ.get("TUNEVIEW_WEB"):
        port = _web_port()
        os.environ["FLET_SERVER_PORT"] = str(port)  # Flet reads this first: same address
        _announce_web(port)
        ft.run(main, view=ft.AppView.WEB_BROWSER, port=port, assets_dir=str(_web_assets()))
    else:
        ft.run(main, view=ft.AppView.FLET_APP, port=int(os.environ.get("TUNEVIEW_PORT", 0)))


if __name__ == "__main__":
    run()
