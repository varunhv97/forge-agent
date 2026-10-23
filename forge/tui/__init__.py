"""
Forge's interactive TUI shell (Textual). Launched by running bare `forge`
with no subcommand — `scripts/forge_cli.py` detects that case and calls
`forge.tui.app.run_tui()` instead of printing argparse's help text.

This package only ever *reads* the store on the main/UI thread (`forge.tui
.app`, `forge.tui.screens`) — the scheduler loop itself runs on a
background thread with its own SQLite connection (`forge.tui.runner.
RunWorker`), never a second writer sharing a connection with the reader.
See `architecture.md`'s TUI section for the concrete bridge.
"""

from __future__ import annotations
