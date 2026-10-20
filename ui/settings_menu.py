"""Clickable entry points to the terminal's existing settings flows."""
from prompt_toolkit.layout import HSplit
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.widgets import Button, Dialog, Label


class SettingsMenu:
    def __init__(self, options, on_close):
        self.message = ""
        self.buttons = [Button(title, width=26, handler=action)
                        for title, _, action in options]
        rows = []
        for button, (_, detail, _) in zip(self.buttons, options):
            rows.extend([button, Label(detail, style="class:muted", wrap_lines=False)])
        self.dialog = Dialog(
            title="⚙ Settings", width=Dimension(min=30, preferred=68, max=76),
            body=HSplit([*rows, Label(lambda: self.message)], padding=0,
                        width=Dimension(preferred=64, max=10000)),
            buttons=[Button("Close", handler=on_close)],
            with_background=False,
        )
