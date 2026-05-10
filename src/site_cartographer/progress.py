"""Rich-based live crawl progress display."""
from __future__ import annotations

from collections import deque
from typing import Deque

from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.progress import BarColumn, Progress, TextColumn
from rich.table import Table
from rich.text import Text

from .archive import format_size
from .crawler import ProgressReporter

_KIND_GLYPH = {
    "archived": ("[green]+[/green]", "[dim]archived[/dim]"),
    "phantom": ("[yellow]?[/yellow]", "[dim]phantom 404[/dim]"),
    "duplicate": ("[cyan]=[/cyan]", "[dim]dup body[/dim]"),
    "error": ("[red]![/red]", "[dim]error[/dim]"),
}


class RichProgressReporter(ProgressReporter):
    """Live-updating Rich panel showing the crawl as it runs.

    Use as a context manager so the display starts/stops cleanly:

        with RichProgressReporter(console) as reporter:
            await crawl(config, progress=reporter)
    """

    def __init__(self, console: Console | None = None, *, history: int = 8):
        self.console = console or Console()
        self.history: Deque[Text] = deque(maxlen=history)
        self.start_url = ""
        self.run_id = 0
        self.max_pages = 0
        self.max_size: int | None = None
        self.current_url = ""
        self.archived_count = 0
        self.discovered = 0
        self.queue_size = 0
        self.archived_bytes = 0
        self.halt_msg = ""

        self._progress = Progress(
            TextColumn("[bold]pages"),
            BarColumn(bar_width=None),
            TextColumn("{task.completed}/{task.total}"),
            expand=True,
        )
        self._task_id: int | None = None
        self._live: Live | None = None

    def __enter__(self) -> "RichProgressReporter":
        self._live = Live(self._render(), console=self.console, refresh_per_second=4)
        self._live.__enter__()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._live is not None:
            self._live.update(self._render(), refresh=True)
            self._live.__exit__(exc_type, exc, tb)

    def _refresh(self) -> None:
        if self._live is not None:
            self._live.update(self._render())

    def _render(self):
        title = f"[bold cyan]site-cartographer[/bold cyan]   [white]{self.start_url}[/white]"

        stats = Table.grid(padding=(0, 2), expand=True)
        stats.add_column(justify="left")
        stats.add_column(justify="left")
        stats.add_column(justify="left")
        stats.add_column(justify="left")
        size_part = format_size(self.archived_bytes)
        if self.max_size is not None:
            size_part += f" / {format_size(self.max_size)}"
        stats.add_row(
            f"[green]archived[/green] {self.archived_count}",
            f"[blue]discovered[/blue] {self.discovered}",
            f"[yellow]queue[/yellow] {self.queue_size}",
            f"[magenta]size[/magenta] {size_part}",
        )

        now_line = Text("now: ", style="bold")
        now_line.append(self.current_url or "(starting…)", style="white")

        if self.history:
            recent = Group(*self.history)
        else:
            recent = Text("(no pages fetched yet)", style="dim")

        body = Group(
            self._progress,
            stats,
            now_line,
            Text("recent:", style="bold dim"),
            recent,
            Text(self.halt_msg, style="bold yellow") if self.halt_msg else Text(""),
        )
        return Panel(body, title=title, border_style="cyan")

    # ProgressReporter hooks ------------------------------------------------

    def on_start(self, *, run_id, start_url, max_pages, max_size) -> None:
        self.run_id = run_id
        self.start_url = start_url
        self.max_pages = max_pages
        self.max_size = max_size
        self._task_id = self._progress.add_task("crawl", total=max_pages)
        self._refresh()

    def on_page(self, *, idx, depth, url, status, title, archived_bytes,
                queue_size, archived_count, discovered, kind) -> None:
        self.current_url = url
        self.archived_count = archived_count
        self.discovered = discovered
        self.queue_size = queue_size
        self.archived_bytes = archived_bytes
        if self._task_id is not None:
            self._progress.update(self._task_id, completed=archived_count)
        glyph, _ = _KIND_GLYPH.get(kind, (f"[white]{kind}[/white]", ""))
        line = Text.from_markup(
            f" {glyph}  [dim]d{depth}[/dim]  [white]{url}[/white]"
            + (f"  [dim]{title[:60]}[/dim]" if title else "")
        )
        self.history.append(line)
        self._refresh()

    def on_halt(self, reason: str) -> None:
        self.halt_msg = f"halted: {reason}"
        self._refresh()

    def on_finish(self, *, archived_count, total_pages, edges) -> None:
        self.archived_count = archived_count
        if self._task_id is not None:
            self._progress.update(self._task_id, completed=archived_count)
        if not self.halt_msg:
            self.halt_msg = (
                f"done: {archived_count} archived, {total_pages} discovered,"
                f" {edges} edges"
            )
        self._refresh()
