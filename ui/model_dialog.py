"""Searchable model choices for the active provider."""
from prompt_toolkit.data_structures import Point
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.mouse_events import MouseEventType
from prompt_toolkit.widgets import Button, Dialog, Label, TextArea

from .workspace import clean_label


class ModelDialog:
    def __init__(self, models, provider, current, source, on_choose, on_close, *, choice_name="model"):
        self.models = models
        self.current = current
        self.on_choose = on_choose
        self.choice_name = choice_name
        self.index = next((i for i, model in enumerate(models) if model["id"] == current), 0)
        self.search = TextArea(multiline=False, prompt="Search: ")
        self.search.buffer.on_text_changed += self._filter_changed
        bindings = KeyBindings()

        @bindings.add("up")
        def up(event):
            self.index = max(0, self.index - 1)

        @bindings.add("down")
        def down(event):
            self.index = min(max(0, len(self.matches) - 1), self.index + 1)

        @bindings.add("enter")
        def choose(event):
            self.choose()

        @bindings.add("escape", eager=True)
        @bindings.add("c-c")
        def cancel(event):
            on_close()

        self.list_control = FormattedTextControl(
            self._rows, focusable=True, key_bindings=bindings,
            get_cursor_position=lambda: Point(0, self.index),
        )
        # Keep typing in the filter while arrows move through the full list.
        self.search.control.key_bindings = bindings
        self.dialog = Dialog(
            title=f"{clean_label(provider)} · Choose {choice_name}",
            width=Dimension(min=30, preferred=82, max=100),
            body=HSplit([
                Label(clean_label(source)), self.search,
                Window(self.list_control, height=Dimension(min=1, preferred=min(12, len(models)), max=16),
                       style="class:folder-list", always_hide_cursor=True),
                Label("↑ ↓ choose · Enter select · Esc cancel"),
            ], padding=1),
            buttons=[Button("Select", handler=self.choose), Button("Cancel", handler=on_close)],
            with_background=False,
        )

    @property
    def matches(self):
        query = self.search.text.casefold().strip()
        return [model for model in self.models
                if query in f"{model['id']} {model.get('label', '')}".casefold()]

    def _filter_changed(self, buffer):
        self.index = 0

    def choose(self):
        models = self.matches
        if models:
            self.on_choose(models[min(self.index, len(models) - 1)]["id"])

    def _rows(self):
        fragments = []
        for index, model in enumerate(self.matches):
            def click(event, model_id=model["id"]):
                if event.event_type == MouseEventType.MOUSE_UP:
                    self.on_choose(model_id)
            marker = "*" if model["id"] == self.current else " "
            label = clean_label(model.get("label") or model["id"])
            model_id = clean_label(model["id"])
            text = f" {marker} {label}" + (f"  ({model_id})" if label != model_id else "")
            fragments.append(("class:folder-selected" if index == self.index else "",
                              text + "\n", click))
        return fragments or [("class:muted", f"  No matching {self.choice_name} options")]
