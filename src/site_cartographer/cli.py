from __future__ import annotations

import argparse
import asyncio
import logging
import shutil
import sys
from datetime import datetime
from pathlib import Path

from . import __version__
from .crawler import CrawlConfig, crawl
from .graph import export_cytoscape_json


def _parse_viewport(s: str) -> tuple[int, int]:
    try:
        w, h = s.lower().split("x")
        return int(w), int(h)
    except Exception as e:
        raise argparse.ArgumentTypeError(f"viewport must be WxH, got {s!r}") from e


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="site-cartographer",
        description="Crawl a site, archive each page as MHTML, and emit"
                    " an interactive Cytoscape graph viewer.",
    )
    p.add_argument("start_url", help="URL to start crawling from")
    p.add_argument("--output", type=Path, default=None,
                   help="output dir (default: ./output/<timestamp>)")
    p.add_argument("--max-pages", type=int, default=100)
    p.add_argument("--max-depth", type=int, default=15)
    p.add_argument("--delay-ms", type=int, default=250)
    p.add_argument("--page-timeout-ms", type=int, default=30000)
    p.add_argument("--include-subdomains", action="store_true")
    p.add_argument("--respect-robots", action="store_true")
    p.add_argument("--user-agent", default=f"site-cartographer/{__version__}")
    p.add_argument("--resume", type=Path, default=None,
                   help="resume an existing run dir")
    p.add_argument("--viewport", type=_parse_viewport, default=(320, 240),
                   help="WxH for thumbnails / page rendering (default 320x240)")
    p.add_argument("--headed", action="store_true",
                   help="show the browser window (debug)")
    p.add_argument("-v", "--verbose", action="count", default=0)
    p.add_argument("--version", action="version", version=__version__)
    return p


def install_viewer(run_dir: Path) -> None:
    src = Path(__file__).parent / "viewer"
    if not src.is_dir():
        return
    dst = run_dir / "viewer"
    dst.mkdir(parents=True, exist_ok=True)
    for entry in src.iterdir():
        target = dst / entry.name
        if entry.is_file():
            shutil.copy2(entry, target)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    log_level = logging.WARNING
    if args.verbose == 1:
        log_level = logging.INFO
    elif args.verbose >= 2:
        log_level = logging.DEBUG
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.resume is not None:
        output_dir = args.resume
        if not output_dir.is_dir():
            print(f"resume dir not found: {output_dir}", file=sys.stderr)
            return 2
        resume_flag = True
    else:
        output_dir = (
            args.output
            if args.output is not None
            else Path("output") / datetime.now().strftime("%Y%m%d-%H%M%S")
        )
        resume_flag = False

    config = CrawlConfig(
        start_url=args.start_url,
        output_dir=output_dir,
        max_pages=args.max_pages,
        max_depth=args.max_depth,
        delay_ms=args.delay_ms,
        page_timeout_ms=args.page_timeout_ms,
        include_subdomains=args.include_subdomains,
        respect_robots=args.respect_robots,
        user_agent=args.user_agent,
        viewport=args.viewport,
        headless=not args.headed,
        resume=resume_flag,
    )

    print(f"crawl -> {output_dir}")
    asyncio.run(crawl(config))

    install_viewer(output_dir)
    graph_path = export_cytoscape_json(output_dir)
    print(f"graph written -> {graph_path}")
    print(f"launch viewer: python -m site_cartographer.serve {output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
