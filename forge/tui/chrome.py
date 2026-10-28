"""
`Chrome`: the custom "title bar" every Forge screen shows in place of
Textual's stock `Header()`. Three low-opacity colored dot glyphs — the
same error/warning/success semantics `screens.py::_status_style` already
draws status dots in, just always-on rather than status-derived — then a
muted `forge — <context>` title, left-aligned. This is what the original
mockups' chrome bar looked like structurally (three dots, then a title,
left-aligned); this widget matches that *structure*, never the mockup's
literal hex colors, which were for a one-off HTML illustration in a
completely different medium.

"Reduced opacity" for the dots is the theme's own `-muted` variant of
each semantic color (`error-muted`/`warning-muted`/`success-muted` —
already real tokens on every Textual theme, confirmed via
`App.get_css_variables()`), not a hand-rolled alpha blend: Textual's
character-grid renderer has no per-span alpha channel, so the "pre-muted
token" *is* the idiomatic way to get a dimmer version of a semantic color
here, the same way `dag_canvas.py` and `screens.py::_status_style` always
look colors up by their theme name rather than picking one themselves.

The title half is deliberately *not* given an explicit per-span Rich
style built from `get_css_variables()["text-muted"]`: that variable's
real value is Textual's own `"auto 60%"` directive (confirmed from a
live `get_css_variables()` dump, not assumed) — syntax Textual's CSS
engine understands but `rich.text.Text`'s own style parser does not
(passing it to `Text.append(style=...)` raises `MissingStyle` the moment
Rich tries to resolve it as a color). The muted title color instead
comes from this widget's own `DEFAULT_CSS`'s `color: $text-muted;` —
handled by Textual's CSS engine, which *does* understand `"auto 60%"` —
with the title span left unstyled so it inherits that base widget color,
the same "no override" pattern `_status_style` already uses for an
unrecognized status (an empty style string, never a bad one).

This widget carries no behavior of its own beyond rendering itself and
re-rendering when the app's theme changes (`App.theme_changed_signal`,
the same signal-subscribe pattern Textual's own `Footer` uses for
`bindings_updated_signal`) — it changes nothing about any screen's data
flow, bindings, or `check_action` gating.
"""

from __future__ import annotations

from rich.text import Text
from textual.theme import Theme
from textual.widgets import Static

_DOT = "● "  # ● plus a trailing space, one per dot.
_DOT_COLOR_KEYS = ("error-muted", "warning-muted", "success-muted")


class Chrome(Static):
    """Three-dot-plus-title chrome bar. Used in place of `Header()`."""

    DEFAULT_CSS = """
    Chrome {
        /* 2 rows, not 1: Textual's box model counts a border edge
           against the declared height, so a plain `height: 1` here
           left zero rows for the widget's own text once `border-bottom`
           claimed the only row available — confirmed empirically (the
           dots/title rendered as a blank line) before raising this to
           2, the same fix `panel.py::LabeledPanel`'s label needed. */
        height: 2;
        padding: 0 1;
        background: $panel;
        color: $text-muted;
        border-bottom: solid $panel-darken-1;
    }
    """

    def __init__(self, title: str = "forge", *, id: str | None = None) -> None:
        super().__init__(id=id)
        self._title = title

    def on_mount(self) -> None:
        self._render_chrome()
        self.app.theme_changed_signal.subscribe(self, self._on_theme_changed)

    def on_unmount(self) -> None:
        self.app.theme_changed_signal.unsubscribe(self)

    def set_title(self, title: str) -> None:
        """Updates the title half of the bar — for the one screen
        (`DashboardScreen`) whose context (the repo name) isn't known
        until after project discovery resolves `ForgeApp.repo_root`,
        later than this widget's own construction."""
        self._title = title
        self._render_chrome()

    def _on_theme_changed(self, _theme: Theme) -> None:
        self._render_chrome()

    def _render_chrome(self) -> None:
        colors = self.app.get_css_variables()
        text = Text()
        for key in _DOT_COLOR_KEYS:
            text.append(_DOT, style=colors.get(key, "dim"))
        text.append(" " + self._title)  # unstyled: inherits this widget's own $text-muted color
        self.update(text)
