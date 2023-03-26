import flet as ft
# from flet import (AppBar, Text, ElevatedButton, UserControl,
#     View, colors, TextField, FloatingActionButton, icons, margin, Column,
#     Row, Dropdown, dropdown, PopupMenuItem, Icon, PopupMenuButton, Container,
#     AlertDialog)
from search import search_song
from tabs import build_tabs


class ChordViewApp(ft.UserControl):
    def __init__(self, page: ft.Page):
        super().__init__()
        self.page = page
        self.dlg = ft.AlertDialog(
            title=ft.Text("Not Found!"),
            content = ft.Text(""),
            actions= [ft.TextButton("Ok", on_click=self.close_dialog)],
            on_dismiss=lambda e: 1
        )
        self.appbar_items = [
            ft.PopupMenuItem(text="Songs"),
            ft.PopupMenuItem(text="Chord Progressions"),
            ft.PopupMenuItem(text="About"),
            ft.PopupMenuItem(),  # divider
            ft.PopupMenuItem(text="Settings")
        ]
        self.appbar = ft.AppBar(
            leading=ft.Icon(ft.icons.AUDIOTRACK),
            leading_width=100,
            title=ft.Text("Chord View", size=32, text_align="start"),
            center_title=False,
            toolbar_height=75,
            bgcolor=ft.colors.LIGHT_BLUE_ACCENT_700,
            actions=[
                ft.Container(
                    content=ft.PopupMenuButton(
                        items=self.appbar_items
                    ),
                    margin=ft.margin.only(left=50, right=25)
                )
            ],
        )
        

        self.update()

    def search_clicked(self, e):
        titles = sorted([t[0] for t in search_song(self.song.value)])
        if not titles:
            self.dlg.content = ft.Text(f"No song containing the word '{self.song.value}'.")
            self.page.dialog = self.dlg
            self.page.dialog.open = True
            self.page.update()
        else:
            self.sdd.options = [ft.dropdown.Option(t) for t in titles]
            self.sdd.value = titles[0]
        self.update()

    def close_dialog(self, e):
        self.dlg.open = False
        self.song.value = "Which song do you want to play?"
        self.page.update()
        self.update()
    def select_song(self, e):
        self.sngname.value = self.sdd.value
        self.update()

    def build(self):
        ''' Build the GUI '''
        titles = sorted([t[0] for t in search_song()])
        self.song = ft.TextField(hint_text="Filter the song list", tooltip="Type a word", width=400)
        self.sngname = ft.Text(" ", style="titleLarge")
        self.selection = ft.Row([ft.Text("Selected Song: ", style="titleMedium"), self.sngname])
        self.sdd = ft.Dropdown(width=600, options=[ft.dropdown.Option(t) for t in titles], 
                            on_change=self.select_song, autofocus=True,
                            value=titles[0])
        self.page.appbar = self.appbar
        self.search_view = ft.Row(controls=[self.sdd, self.song, 
                                         ft.FloatingActionButton(icon=ft.icons.SEARCH,
                                                                 tooltip="Click to filter the list", 
                                                                 on_click=self.search_clicked)])
        
        
    
        return ft.Column(controls=[self.search_view, self.selection])




def main(page: ft.Page):
    page.title = "Chord Viewer"
    page.bgcolor = ft.colors.BLUE_GREY_200
    app = ChordViewApp(page)
    page.add(app)

    page.update()


ft.app(target=main)
