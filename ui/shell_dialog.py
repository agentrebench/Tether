"""Search included and installed zsh themes for new terminal sessions."""
from prompt_toolkit.completion import PathCompleter
from prompt_toolkit.filters import Condition
from prompt_toolkit.layout import ConditionalContainer, HSplit, VSplit
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.widgets import Button, Dialog, Label, RadioList, TextArea

from .theme_catalog import theme_catalog


class ShellDialog:
    def __init__(self, config, on_save, on_close, on_preview=None):
        self.message = ""
        self.catalog = theme_catalog()
        shell_mode = config.terminal_shell if config.terminal_shell in ("", "zsh", "bash") else "custom"
        self.shell_choice = RadioList([
            ("", "Automatic"), ("zsh", "Zsh"), ("bash", "Bash"), ("custom", "Custom path"),
        ], default=shell_mode, show_scrollbar=False)
        self.shell = TextArea(text=config.terminal_shell, multiline=False)
        self.search = TextArea(multiline=False, prompt="Find theme: ")
        self.theme = RadioList([(item.id, item.name) for item in self.catalog],
                               default=config.terminal_theme, select_on_focus=True)
        self.theme.window.height = Dimension(min=1, preferred=13, max=18)
        self.search.buffer.on_text_changed += self.filter_themes
        self.font = RadioList([
            ("ascii", "ASCII"), ("powerline", "Powerline"), ("nerd", "Nerd Font"),
        ], default=config.terminal_font_mode, show_scrollbar=False)
        self.theme_file = TextArea(text=config.terminal_theme_file, multiline=False,
                                   completer=PathCompleter(expanduser=True))
        self.save = lambda: on_save(*self.values())
        self.dialog = Dialog(
            title="Settings / Zsh themes", width=Dimension(min=30, preferred=88, max=96),
            body=HSplit([
                self.search,
                VSplit([
                    HSplit([Label(f"{len(self.catalog) - 4} Oh My Zsh · 2 built in", style="class:muted"),
                            self.theme], width=Dimension(min=16, preferred=48)),
                    HSplit([
                        Label("Shell", style="class:brand"), self.shell_choice,
                        Label("Built-in prompt glyphs", style="class:brand"), self.font,
                        ConditionalContainer(HSplit([Label("Shell executable path"), self.shell]),
                                             filter=Condition(lambda: self.shell_choice.current_value == "custom")),
                        ConditionalContainer(HSplit([Label("Custom theme file"), self.theme_file]),
                                             filter=Condition(lambda: self.theme.current_value == "custom")),
                    ], width=Dimension(min=18, preferred=26, max=32)),
                ], padding=2),
                Label(lambda: self.message or self.preview(), wrap_lines=False, style="class:muted"),
            ], width=Dimension(preferred=82)),
            buttons=[*([Button("Preview", handler=lambda: on_preview(*self.values()))] if on_preview else []),
                     Button("Save", handler=self.save), Button("Cancel", handler=on_close)],
            with_background=False,
        )

    def values(self):
        shell = self.shell.text.strip() if self.shell_choice.current_value == "custom" else self.shell_choice.current_value
        theme = self.theme.current_value
        # Selecting a zsh theme is enough; users needn't also change a shell radio.
        if theme != "inherit" and shell in ("", "bash"):
            shell = "zsh"
        return shell, theme, self.font.current_value, self.theme_file.text.strip()

    def filter_themes(self, _):
        query = self.search.text.casefold().strip()
        matches = [item for item in self.catalog if query in f"{item.name} {item.source}".casefold()]
        self.theme.values = [(item.id, item.name) for item in matches] or [(None, "No matching themes")]
        self.theme._selected_index = next((i for i, (key, _) in enumerate(self.theme.values)
                                          if key == self.theme.current_value), 0)
        if matches and self.theme.current_value not in {item.id for item in matches}:
            self.theme.current_value = matches[0].id
        elif not matches:
            self.theme.current_value = None

    def preview(self):
        theme, font = self.theme.current_value, self.font.current_value
        if theme == "inherit":
            return "Keep your existing shell prompt and startup configuration."
        if theme == "custom":
            return "Load a local zsh theme file."
        if theme == "minimal":
            return "me@host ~/project $"
        if theme == "powerline":
            separator = ">" if font == "ascii" else "\ue0b0"
            folder = "\uf07b " if font == "nerd" else ""
            return f"me@host {separator} {folder}~/project {separator} $"
        item = next((item for item in self.catalog if item.id == theme), None)
        return f"{item.name} · {item.source} · Uses your terminal's font" if item else "No matching themes"
