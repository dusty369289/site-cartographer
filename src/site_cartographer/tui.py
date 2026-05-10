"""Interactive TUI: main menu, scan setup, and run picker."""
from __future__ import annotations

import asyncio
import logging
import re
import shutil
import webbrowser
from datetime import datetime
from pathlib import Path

import questionary
from questionary import Style
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from . import __version__
from .archive import RunSummary, format_size, list_runs, parse_size
from .crawler import CrawlConfig, crawl
from .graph import export_cytoscape_json
from .progress import RichProgressReporter
from .serve import serve

console = Console()

_QUESTIONARY_STYLE = Style(
    [
        ("qmark", "fg:#00d7af bold"),
        ("question", "bold"),
        ("answer", "fg:#5fafff bold"),
        ("pointer", "fg:#00d7af bold"),
        ("highlighted", "fg:#00d7af bold"),
        ("selected", "fg:#5fafff"),
        ("instruction", "fg:#808080"),
    ]
)


def _banner() -> None:
    console.print(
        Panel.fit(
            Text.from_markup(
                "[bold cyan]site-cartographer[/bold cyan]"
                f" [dim]v{__version__}[/dim]\n"
                "[dim]crawl. archive. graph. explore.[/dim]"
            ),
            border_style="cyan",
        )
    )


def main_menu(output_root: Path) -> int:
    """Top-level interactive loop. Returns process exit code."""
    while True:
        _banner()
        choice = questionary.select(
            "what would you like to do?",
            choices=[
                "scan a new site",
                "view a saved scan",
                "list saved scans",
                "quit",
            ],
            style=_QUESTIONARY_STYLE,
        ).ask()
        if choice is None or choice == "quit":
            console.print("[dim]bye[/dim]")
            return 0
        if choice == "scan a new site":
            _interactive_scan(output_root)
        elif choice == "view a saved scan":
            _interactive_view(output_root)
        elif choice == "list saved scans":
            _print_runs_table(list_runs(output_root))


def _print_runs_table(runs: list[RunSummary]) -> None:
    if not runs:
        console.print("[dim]no saved scans found[/dim]")
        return
    table = Table(title="saved scans", border_style="cyan", header_style="bold cyan")
    table.add_column("name", style="bold")
    table.add_column("url", style="white")
    table.add_column("started")
    table.add_column("pages", justify="right")
    table.add_column("archived", justify="right")
    table.add_column("edges", justify="right")
    table.add_column("size", justify="right")
    table.add_column("status")
    for r in runs:
        status = "running" if r.finished_at is None else (
            r.halt_reason or "finished"
        )
        table.add_row(
            r.display_name,
            (r.start_url or "")[:60],
            (r.started_at or "")[:19],
            str(r.page_count),
            str(r.archived_count),
            str(r.edge_count),
            format_size(r.total_bytes),
            status[:30],
        )
    console.print(table)


_INVALID_NAME_RE = re.compile(r"[^a-zA-Z0-9._-]")


def _slugify_name(name: str) -> str:
    return _INVALID_NAME_RE.sub("-", name.strip()).strip("-") or "scan"


def _interactive_scan(output_root: Path) -> None:
    answers = questionary.form(
        url=questionary.text(
            "start URL:",
            validate=lambda v: v.startswith(("http://", "https://"))
            or "must start with http:// or https://",
        ),
        name=questionary.text(
            "name for this scan (optional, used for the directory):",
            default="",
        ),
        max_pages=questionary.text("max pages:", default="100"),
        max_depth=questionary.text("max depth:", default="15"),
        max_size=questionary.text(
            "max archive size (e.g. 500MB, 2GB; blank = unlimited):",
            default="",
        ),
        delay_ms=questionary.text("delay between requests (ms):", default="250"),
        include_subdomains=questionary.confirm(
            "include subdomains?", default=False
        ),
        respect_robots=questionary.confirm(
            "respect robots.txt?", default=False
        ),
        headed=questionary.confirm(
            "show browser window (debug)?", default=False
        ),
    ).ask(style=_QUESTIONARY_STYLE)
    if answers is None:
        return  # user cancelled

    try:
        max_size = parse_size(answers["max_size"]) if answers["max_size"].strip() else None
    except ValueError as e:
        console.print(f"[red]bad size:[/red] {e}")
        return

    name = answers["name"].strip()
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    if name:
        slug = _slugify_name(name)
        output_dir = output_root / f"{slug}-{timestamp}"
    else:
        slug = None
        output_dir = output_root / timestamp

    config = CrawlConfig(
        start_url=answers["url"].strip(),
        output_dir=output_dir,
        name=name or None,
        max_pages=int(answers["max_pages"]),
        max_depth=int(answers["max_depth"]),
        max_file_size=max_size,
        delay_ms=int(answers["delay_ms"]),
        include_subdomains=answers["include_subdomains"],
        respect_robots=answers["respect_robots"],
        headless=not answers["headed"],
    )

    console.print(f"\n[dim]→ writing to[/dim] [cyan]{output_dir}[/cyan]\n")
    try:
        with RichProgressReporter(console) as reporter:
            asyncio.run(crawl(config, progress=reporter))
    except KeyboardInterrupt:
        console.print("[yellow]interrupted by user[/yellow]")
    _post_crawl(output_dir)


def _post_crawl(run_dir: Path) -> None:
    """Copy viewer assets and export graph.json after a crawl."""
    src = Path(__file__).parent / "viewer"
    dst = run_dir / "viewer"
    dst.mkdir(parents=True, exist_ok=True)
    for entry in src.iterdir():
        if entry.is_file():
            shutil.copy2(entry, dst / entry.name)
    try:
        graph_path = export_cytoscape_json(run_dir)
        console.print(f"[dim]graph written ->[/dim] [cyan]{graph_path}[/cyan]")
    except Exception as e:
        console.print(f"[yellow]graph export failed: {e}[/yellow]")
    console.print(
        "[dim]launch viewer:[/dim]"
        f" [cyan]python -m site_cartographer.serve {run_dir}[/cyan]"
    )


def _interactive_view(output_root: Path) -> None:
    runs = list_runs(output_root)
    if not runs:
        console.print("[dim]no saved scans found. try 'scan a new site' first.[/dim]")
        return

    choices = []
    for r in runs:
        size = format_size(r.total_bytes)
        when = (r.started_at or "")[:16]
        status = "✓" if r.finished_at else "…"
        label = f"{status} {r.display_name:<40}  {when}  {r.archived_count} pages  {size}"
        choices.append(questionary.Choice(title=label, value=r))
    choices.append(questionary.Choice(title="< back", value=None))

    selected = questionary.select(
        "select a scan to view:", choices=choices, style=_QUESTIONARY_STYLE,
    ).ask()
    if selected is None:
        return

    port = 8000
    url = f"http://127.0.0.1:{port}/viewer/"
    console.print(
        Panel.fit(
            Text.from_markup(
                f"[bold]starting viewer[/bold]\n[cyan]{url}[/cyan]\n\n"
                "[dim]press Ctrl+C to stop and return to the menu[/dim]"
            ),
            border_style="green",
        )
    )
    try:
        webbrowser.open(url)
    except Exception:
        pass
    try:
        serve(selected.dir, port=port)
    except KeyboardInterrupt:
        console.print("[dim]viewer stopped.[/dim]")
    except OSError as e:
        console.print(f"[red]server error:[/red] {e}")
