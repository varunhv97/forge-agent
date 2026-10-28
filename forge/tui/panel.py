"""
`LabeledPanel`: a bordered box with a small uppercase label across its
top edge — the TASKS/TRACE/CHAT-style panels the original mockups used
to separate every screen's logical sections, in place of today's flat
sibling widgets that have at most a single-line divider border and no
label at all.

Purely a visual grouping container: a caller mounts whatever it would
have mounted directly before inside a `with LabeledPanel(...):` block
(the same context-manager idiom every other container in this codebase
already uses — see `screens.py`'s `with Horizontal(id="list-view"):`),
and keeps `query_one`-ing that content by its own existing id exactly as
before. This widget changes nothing about data flow, bindings, or
`check_action` gating anywhere it's used.
"""

from __future__ import annotations

from textual.containers import Vertical
from textual.widget import Widget
from textual.widgets import Static


class LabeledPanel(Vertical):
    """A `Vertical` with a small uppercase label `Static` mounted as its
    first child, and a border around the whole group. Any children
    passed positionally (or yielded inside a `with LabeledPanel(...):`
    block) are mounted after the label, inside the same bordered box."""

    DEFAULT_CSS = """
    LabeledPanel {
        border: solid $panel;
    }
    LabeledPanel > .panel-label {
        /* 2 rows, not 1: Textual's box model counts a border edge
           against the declared height, so a plain `height: 1` here
           left zero rows for the label's own text once `border-bottom`
           claimed the only row available — confirmed empirically (the
           label rendered as a blank line) before raising this to 2. */
        height: 2;
        padding: 0 1;
        color: $text-muted;
        text-style: bold;
        border-bottom: solid $panel;
    }
    """

    def __init__(
        self,
        label: str,
        *children: Widget,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        super().__init__(
            Static(label.upper(), classes="panel-label"),
            *children,
            id=id,
            classes=classes,
        )
