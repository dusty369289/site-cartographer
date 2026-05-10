"""Interactive TUI: main menu, scan setup, and run picker."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import sqlite3
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
    """Top-level interactive loop. Returns process exit code.

    Ctrl+C at any prompt returns to the main menu; Ctrl+C at the main menu
    quits cleanly. While a scan is running, Ctrl+C halts it safely (the run
    becomes resumable).
    """
    while True:
        _banner()
        try:
            choice = questionary.select(
                "what would you like to do?",
                choices=[
                    "scan a new site",
                    "resume an unfinished scan",
                    "view a saved scan",
                    "list saved scans",
                    "quit",
                ],
                style=_QUESTIONARY_STYLE,
            ).ask()
        except KeyboardInterrupt:
            choice = None
        if choice is None or choice == "quit":
            console.print("[dim]bye[/dim]")
            return 0
        try:
            if choice == "scan a new site":
                _interactive_scan(output_root)
            elif choice == "resume an unfinished scan":
                _interactive_resume(output_root)
            elif choice == "view a saved scan":
                _interactive_view(output_root)
            elif choice == "list saved scans":
                _print_runs_table(list_runs(output_root))
        except KeyboardInterrupt:
            console.print("\n[yellow]returned to menu[/yellow]")


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

# Sentinel for "back" choices in questionary selectors. We can't use None
# because questionary substitutes the title for the value when value is None.
_BACK = object()


def _slugify_name(name: str) -> str:
    return _INVALID_NAME_RE.sub("-", name.strip()).strip("-") or "scan"


def _interactive_scan(output_root: Path) -> None:
    s = _QUESTIONARY_STYLE
    answers = questionary.form(
        url=questionary.text(
            "start URL:",
            validate=lambda v: v.startswith(("http://", "https://"))
            or "must start with http:// or https://",
            style=s,
        ),
        name=questionary.text(
            "name for this scan (optional, used for the directory):",
            default="", style=s,
        ),
        max_pages=questionary.text("max pages:", default="100", style=s),
        max_depth=questionary.text("max depth:", default="15", style=s),
        max_size=questionary.text(
            "max archive size (e.g. 500MB, 2GB; blank = unlimited):",
            default="", style=s,
        ),
        delay_ms=questionary.text("delay between requests (ms):", default="250", style=s),
        include_subdomains=questionary.confirm(
            "include subdomains?", default=False, style=s,
        ),
        respect_robots=questionary.confirm(
            "respect robots.txt?", default=False, style=s,
        ),
        headed=questionary.confirm(
            "show browser window (debug)?", default=False, style=s,
        ),
    ).ask()
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
        console.print(
            "[yellow]paused.[/yellow]"
            " resume from the main menu when you're ready."
        )
    _post_crawl(output_dir)


def _load_run_config(run_dir: Path) -> dict:
    """Deserialise the stored config_json from the latest run in *run_dir*."""
    db = run_dir / "crawl.sqlite"
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT config_json FROM runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return {}
    try:
        return json.loads(row["config_json"])
    except Exception:
        return {}


def _interactive_resume(output_root: Path) -> None:
    runs = [r for r in list_runs(output_root) if r.is_resumable]
    if not runs:
        console.print(
            "[dim]nothing to resume — all scans are either finished and queue-empty,"
            " or there are no scans yet.[/dim]"
        )
        return

    choices = []
    for r in runs:
        when = (r.started_at or "")[:16]
        reason = r.halt_reason or ("interrupted" if r.finished_at is None else "")
        label = (
            f"  {r.display_name:<30}  {when}  "
            f"archived {r.archived_count}  queue {r.queue_depth}"
            + (f"  [{reason[:25]}]" if reason else "")
        )
        choices.append(questionary.Choice(title=label, value=r))
    choices.append(questionary.Choice(title="< back", value=_BACK))

    selected = questionary.select(
        "select an unfinished scan to resume:",
        choices=choices, style=_QUESTIONARY_STYLE,
    ).ask()
    if selected is None or selected is _BACK:
        return

    existing = _load_run_config(selected.dir)
    cur_max_pages = int(existing.get("max_pages") or 100)
    cur_max_depth = int(existing.get("max_depth") or 15)
    cur_max_size = existing.get("max_file_size")
    cur_size_str = format_size(cur_max_size) if cur_max_size else ""

    suggested_pages = cur_max_pages + max(50, cur_max_pages)

    s = _QUESTIONARY_STYLE
    answers = questionary.form(
        max_pages=questionary.text(
            f"max pages (was {cur_max_pages}, suggesting {suggested_pages}):",
            default=str(suggested_pages), style=s,
        ),
        max_depth=questionary.text(
            f"max depth (was {cur_max_depth}):",
            default=str(cur_max_depth), style=s,
        ),
        max_size=questionary.text(
            f"max archive size (was {cur_size_str or 'unlimited'}; blank = unlimited):",
            default=cur_size_str, style=s,
        ),
    ).ask()
    if answers is None:
        return

    try:
        new_size = parse_size(answers["max_size"]) if answers["max_size"].strip() else None
    except ValueError as e:
        console.print(f"[red]bad size:[/red] {e}")
        return

    config = CrawlConfig(
        start_url=existing.get("start_url", selected.start_url or ""),
        output_dir=selected.dir,
        name=existing.get("name") or selected.name,
        max_pages=int(answers["max_pages"]),
        max_depth=int(answers["max_depth"]),
        max_file_size=new_size,
        delay_ms=int(existing.get("delay_ms") or 250),
        page_timeout_ms=int(existing.get("page_timeout_ms") or 30000),
        include_subdomains=bool(existing.get("include_subdomains")),
        respect_robots=bool(existing.get("respect_robots")),
        user_agent=existing.get("user_agent") or f"site-cartographer/{__version__}",
        viewport=tuple(existing.get("viewport") or [320, 240]),
        headless=True,
        resume=True,
    )

    console.print(f"\n[dim]→ resuming[/dim] [cyan]{selected.dir}[/cyan]\n")
    try:
        with RichProgressReporter(console) as reporter:
            asyncio.run(crawl(config, progress=reporter))
    except KeyboardInterrupt:
        console.print(
            "[yellow]paused.[/yellow]"
            " resume from the main menu when you're ready."
        )
    _post_crawl(selected.dir)


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
    choices.append(questionary.Choice(title="< back", value=_BACK))

    selected = questionary.select(
        "select a scan to view:", choices=choices, style=_QUESTIONARY_STYLE,
    ).ask()
    if selected is None or selected is _BACK:
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
