"""Chordview's Flet interface: a library on the left, the leadsheet on the music stand."""

from __future__ import annotations

import asyncio
import os
from collections import Counter
from dataclasses import dataclass, field

import flet as ft

from .engraver import (
    INSTRUMENTS,
    ChartBar,
    Engraver,
    chart_from_mei,
    normalize_shift,
    split_chord,
)
from .library import Library, SongInfo, search

SHEET, CHART = "sheet", "chart"
ZOOM_STEPS = [30, 35, 40, 45, 50, 55, 60, 70, 80]
SIDEBAR_WIDTH = 330
NARROW = 900  # below this width the library and the music take turns
SEED_COLOR = "#3f51b5"

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
    pages: list[bytes]
    bars: list[ChartBar]
    shift: int
    page_width: int = 0
    error: str = ""


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
    render_token: int = 0

    @property
    def total_shift(self) -> int:
        return normalize_shift(INSTRUMENTS[self.instrument] + self.shift)


class ChordView:
    def __init__(self, page: ft.Page, library: Library):
        self.page = page
        self.library = library
        self.engraver = Engraver()
        self.state = State(songs=library.songs())
        self._build()

    # ------------------------------------------------------------------ layout
    def _build(self) -> None:
        p = self.page
        p.title = "Chordview"
        p.theme = ft.Theme(color_scheme_seed=SEED_COLOR)
        p.dark_theme = ft.Theme(color_scheme_seed=SEED_COLOR)
        p.theme_mode = ft.ThemeMode.SYSTEM
        p.padding = 0
        p.spacing = 0
        p.on_keyboard_event = self.on_key
        p.on_platform_brightness_change = self.on_theme_changed
        p.on_resize = self.on_resize

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
                            ft.Text("Chordview", size=22, weight=ft.FontWeight.W_600),
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
                        controls=[self.mode_button, self.instrument_dd, transpose_box, zoom_box,
                                  self.theme_button, self.fullscreen_button],
                    ),
                ],
            ),
        )

        self.content = ft.ListView(expand=True, spacing=16, padding=ft.Padding.all(16))
        self.progress = ft.ProgressBar(visible=False)
        self.viewer = ft.Column(expand=True, spacing=0,
                                horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
                                controls=[self.toolbar, self.progress, self.content])

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
        self.progress.visible = True
        self.update_header()
        self.page.update()

        shift, zoom, page_width = self.state.total_shift, self.state.zoom, self._page_width()
        ink = "#e6e1e5" if self.dark else "#000000"

        def work() -> Rendered:
            try:
                xml = self.library.musicxml(song.id)
                eng = self.engraver.load(xml, transpose=shift, zoom=zoom, page_width=page_width)
                bars = chart_from_mei(eng.mei)
                pages = [self.engraver.render_page(i, ink=ink) for i in range(1, eng.pages + 1)]
                return Rendered(song, pages, bars, shift, page_width)
            except Exception as exc:  # a bad file must not take the app down
                return Rendered(song, [], [], shift, page_width, error=str(exc) or type(exc).__name__)

        result = await asyncio.to_thread(work)
        if token != self.state.render_token:
            return  # a newer request superseded this one
        self.state.rendered = result
        self.progress.visible = False
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
                        ft.Text("Pick a tune from the library", size=24, weight=ft.FontWeight.W_500),
                        ft.Text(
                            f"{len(self.state.songs)} leadsheets · melody, chords and lyrics, "
                            "transposed for your instrument",
                            color=ft.Colors.ON_SURFACE_VARIANT,
                        ),
                        ft.Text(
                            "Keys: / search · ↓ ↑ PgDn PgUp scroll · [ ] transpose · "
                            "S sheet · C chords · L library · D dark",
                            size=12,
                            color=ft.Colors.ON_SURFACE_VARIANT,
                        ),
                    ],
                ),
            )
        ]
        self.title_text.value = "Chordview"
        self.subtitle_text.value = "Leadsheets on your music stand"

    def show_content(self) -> None:
        r = self.state.rendered
        if r is None:
            return
        if r.error:
            self.content.controls = [self._notice(ft.Icons.ERROR_OUTLINE,
                                                  f"Couldn't engrave this leadsheet: {r.error}")]
        elif self.state.mode == SHEET:
            self.content.controls = self._sheet(r)
        else:
            self.content.controls = self._chart(r)

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
        for png in r.pages:
            out.append(
                ft.Container(
                    bgcolor=paper,
                    border_radius=6,
                    padding=ft.Padding.symmetric(horizontal=8, vertical=12),
                    shadow=ft.BoxShadow(blur_radius=8, color=ft.Colors.with_opacity(0.18, ft.Colors.BLACK)),
                    content=ft.Image(src=png, fit=ft.BoxFit.FIT_WIDTH, gapless_playback=True),
                )
            )
        return out

    # ------------------------------------------------------------- chart view
    def _chart(self, r: Rendered) -> list[ft.Control]:
        bars = [b for b in r.bars if b.chords or b.rest_bars or b.number not in ("0", "")]
        if not any(b.chords for b in bars):
            return [self._notice(ft.Icons.INFO_OUTLINE,
                                 "This leadsheet has no chord symbols — switch to Sheet.")]
        size = self.state.zoom * 0.62  # chord font size follows the zoom setting
        per_line = self._bars_per_line()
        lines: list[list[ChartBar]] = [[]]
        for bar in bars:
            line = lines[-1]
            starts_section = bar.repeat_start or bool(bar.marks)
            if line and (len(line) == per_line or starts_section):
                lines.append(line := [])
            line.append(bar)
            if bar.final or bar.double or bar.repeat_end:
                lines.append([])
        rows = []
        for line in (ln for ln in lines if ln):
            cells: list[ft.Control] = [self._bar_cell(b, size) for b in line]
            cells += [ft.Container(expand=1) for _ in range(per_line - len(line))]
            rows.append(ft.Row(cells, spacing=0, vertical_alignment=ft.CrossAxisAlignment.STRETCH,
                               intrinsic_height=True))
        return [ft.Container(padding=ft.Padding.symmetric(horizontal=8, vertical=8),
                             content=ft.Column(rows, spacing=14))]

    def _bar_cell(self, bar: ChartBar, size: float) -> ft.Control:
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
        if self.state.search_focused:
            if key == "Escape":
                self.search_field.value = ""
                self.populate_list(self.state.songs)
                self.page.update()
            return
        if key in ("Arrow Down", "Page Down", " "):  # page-turner pedals send these
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
        elif key == "L":
            self.toggle_library()
        elif key == "D":
            await self.toggle_theme()
        elif key == "F11":
            self.toggle_fullscreen()


async def main(page: ft.Page) -> None:
    library = Library()
    if not library.is_ready():
        page.add(
            ft.Container(
                padding=40,
                content=ft.Text(
                    f"No leadsheet library at {library.path}.\n\n"
                    "Build it from a folder of MusicXML files:\n"
                    "    uv run chordview-import /path/to/musicxml",
                    selectable=True,
                    size=16,
                ),
            )
        )
        return
    ChordView(page, library)
    page.update()


def run() -> None:
    view = ft.AppView.WEB_BROWSER if os.environ.get("CHORDVIEW_WEB") else ft.AppView.FLET_APP
    ft.run(main, view=view, port=int(os.environ.get("CHORDVIEW_PORT", 0)))


if __name__ == "__main__":
    run()
