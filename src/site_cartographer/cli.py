from __future__ import annotations

import argparse
import asyncio
import logging
import shutil
import sys
import webbrowser
from datetime import datetime
from pathlib import Path

from rich.console import Console
from rich.logging import RichHandler

from . import __version__
from .archive import RunSummary, format_size, list_runs, parse_size
from .crawler import CrawlConfig, crawl
from .graph import export_cytoscape_json
from .links import ALL_EXTRACTORS, COMMON_EXTRACTORS, DEFAULT_EXTRACTORS
from .progress import RichProgressReporter
from .serve import serve


def _parse_extractors(s: str) -> tuple[str, ...]:
    s = s.strip().lower()
    if s == "default" or s == "":
        return DEFAULT_EXTRACTORS
    if s == "common":
        return COMMON_EXTRACTORS
    if s == "all":
        return ALL_EXTRACTORS
    return tuple(p.strip() for p in s.split(",") if p.strip())

console = Console()


def _parse_viewport(s: str) -> tuple[int, int]:
    try:
        w, h = s.lower().split("x")
        return int(w), int(h)
    except Exception as e:
        raise argparse.ArgumentTypeError(f"viewport must be WxH, got {s!r}") from e


def _add_scan_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("start_url", help="URL to start crawling from")
    p.add_argument("--name", default=None,
                   help="give this scan a memorable name")
    p.add_argument("--output", type=Path, default=None,
                   help="output dir (default: ./output/<name>-<timestamp>)")
    p.add_argument("--max-pages", type=int, default=100)
    p.add_argument("--max-depth", type=int, default=15)
    p.add_argument("--max-file-size", type=parse_size, default=None,
                   help="halt when archive exceeds this size, e.g. 500MB or 2GB")
    p.add_argument("--workers", type=int, default=1,
                   help="parallel discovery workers (1-8, default 1 = sequential)")
    p.add_argument("--delay-ms", type=int, default=250,
                   help="per-worker politeness delay between fetches")
    p.add_argument("--page-timeout-ms", type=int, default=30000)
    p.add_argument(
        "--include-subdomains", action="store_true",
        help="also crawl any subdomain of the start host (e.g. blog.foo.com"
             " when starting at foo.com); `www.` is always treated as same-site",
    )
    p.add_argument(
        "--respect-robots", action="store_true",
        help="honour the target's /robots.txt disallow rules (off by default)",
    )
    p.add_argument(
        "--external-policy",
        choices=("ignore", "metadata", "archive", "crawl"),
        default="metadata",
        help="how to handle out-of-scope links. "
             "ignore: silently drop. "
             "metadata: record edge + URL only (default). "
             "archive: also fetch & save the external page. "
             "crawl: archive AND extract its links (one hop only)",
    )
    p.add_argument(
        "--link-extractors",
        default="a,area",
        help="comma-separated list of HTML link extractors to use. "
             "valid: a,area,iframe,form,link,onclick,data_attrs,text_url. "
             "shortcuts: 'default' (a,area), 'common' (default + iframe,form,data_attrs), 'all'",
    )
    p.add_argument(
        "--link-regex", default=None,
        help="optional regex run against raw HTML; first capture group "
             "(or the whole match) is treated as a URL",
    )
    p.add_argument("--user-agent", default=f"site-cartographer/{__version__}")
    p.add_argument("--resume", type=Path, default=None,
                   help="resume an existing run dir")
    p.add_argument("--viewport", type=_parse_viewport, default=(320, 240))
    p.add_argument("--headed", action="store_true")
    p.add_argument("-v", "--verbose", action="count", default=0)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="site-cartographer",
        description="crawl a site, archive each page, and explore the link graph",
    )
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("--output-root", type=Path, default=Path("output"),
                   help="root dir under which all scan output dirs live")

    sub = p.add_subparsers(dest="command")

    sp_scan = sub.add_parser("scan", help="run a new crawl")
    _add_scan_args(sp_scan)

    sp_view = sub.add_parser("view", help="serve a saved scan in the viewer")
    sp_view.add_argument("run", type=str, nargs="?",
                         help="run directory name or path (omit for picker)")
    sp_view.add_argument("--port", type=int, default=8000)
    sp_view.add_argument("--no-browser", action="store_true")

    sub.add_parser("list", help="list saved scans")
    sub.add_parser("interactive", help="open the interactive TUI")

    sp_web = sub.add_parser("web", help="launch the browser-based UI")
    sp_web.add_argument("--port", type=int, default=8000)
    sp_web.add_argument("--no-browser", action="store_true",
                        help="don't auto-open a browser")
    return p


def _setup_logging(verbose: int) -> None:
    level = logging.WARNING
    if verbose == 1:
        level = logging.INFO
    elif verbose >= 2:
        level = logging.DEBUG
    logging.basicConfig(
        level=level, handlers=[RichHandler(console=console, show_path=False)],
        format="%(message)s", datefmt="%H:%M:%S",
    )


def _install_viewer(run_dir: Path) -> None:
    src = Path(__file__).parent / "viewer"
    if not src.is_dir():
        return
    dst = run_dir / "viewer"
    dst.mkdir(parents=True, exist_ok=True)
    for entry in src.iterdir():
        if entry.is_file():
            shutil.copy2(entry, dst / entry.name)


def _cmd_scan(args: argparse.Namespace) -> int:
    _setup_logging(args.verbose)

    if args.resume is not None:
        output_dir = args.resume
        if not output_dir.is_dir():
            console.print(f"[red]resume dir not found:[/red] {output_dir}")
            return 2
        resume_flag = True
    else:
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        if args.output is not None:
            output_dir = args.output
        elif args.name:
            from .tui import _slugify_name
            output_dir = args.output_root / f"{_slugify_name(args.name)}-{timestamp}"
        else:
            output_dir = args.output_root / timestamp
        resume_flag = False

    config = CrawlConfig(
        start_url=args.start_url,
        output_dir=output_dir,
        name=args.name,
        max_pages=args.max_pages,
        max_depth=args.max_depth,
        max_file_size=args.max_file_size,
        delay_ms=args.delay_ms,
        page_timeout_ms=args.page_timeout_ms,
        parallel_workers=args.workers,
        include_subdomains=args.include_subdomains,
        respect_robots=args.respect_robots,
        external_policy=args.external_policy,
        link_extractors=_parse_extractors(args.link_extractors),
        custom_link_regex=args.link_regex,
        user_agent=args.user_agent,
        viewport=args.viewport,
        headless=not args.headed,
        resume=resume_flag,
    )

    console.print(f"[dim]-> writing to[/dim] [cyan]{output_dir}[/cyan]")
    try:
        with RichProgressReporter(console) as reporter:
            asyncio.run(crawl(config, progress=reporter))
    except KeyboardInterrupt:
        console.print(
            "[yellow]paused.[/yellow]"
            f" resume with: [cyan]site-cartographer scan {config.start_url}"
            f" --resume {output_dir}[/cyan]"
        )
        return 130

    _install_viewer(output_dir)
    try:
        graph_path = export_cytoscape_json(output_dir)
        console.print(f"[dim]graph ->[/dim] [cyan]{graph_path}[/cyan]")
    except Exception as e:
        console.print(f"[yellow]graph export failed: {e}[/yellow]")
    console.print(
        "[dim]launch viewer:[/dim]"
        f" [cyan]site-cartographer view {output_dir}[/cyan]"
    )
    return 0


def _resolve_run(arg: str | None, output_root: Path) -> Path | None:
    """Resolve a run reference (name, dir name, or path) to a run dir."""
    if arg is None:
        return None
    p = Path(arg)
    if p.is_dir() and (p / "crawl.sqlite").is_file():
        return p
    candidate = output_root / arg
    if candidate.is_dir() and (candidate / "crawl.sqlite").is_file():
        return candidate
    runs = list_runs(output_root)
    matches = [r.dir for r in runs if r.name == arg or r.dir.name == arg]
    if matches:
        return matches[0]
    return None


def _cmd_view(args: argparse.Namespace) -> int:
    if args.run is None:
        from .tui import _interactive_view
        _interactive_view(args.output_root)
        return 0
    run_dir = _resolve_run(args.run, args.output_root)
    if run_dir is None:
        console.print(f"[red]run not found:[/red] {args.run}")
        return 2
    url = f"http://127.0.0.1:{args.port}/viewer/"
    console.print(f"[dim]viewer:[/dim] [cyan]{url}[/cyan]  [dim](Ctrl+C to stop)[/dim]")
    if not args.no_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        serve(run_dir, port=args.port)
    except KeyboardInterrupt:
        return 0
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    from .tui import _print_runs_table
    _print_runs_table(list_runs(args.output_root))
    return 0


def _cmd_interactive(args: argparse.Namespace) -> int:
    from .tui import main_menu
    return main_menu(args.output_root)


def _cmd_web(args: argparse.Namespace) -> int:
    from .webapp import serve_app
    serve_app(args.output_root, port=args.port, open_browser=not args.no_browser)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cmd = args.command or "interactive"
    dispatch = {
        "scan": _cmd_scan,
        "view": _cmd_view,
        "list": _cmd_list,
        "interactive": _cmd_interactive,
        "web": _cmd_web,
    }
    return dispatch[cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
