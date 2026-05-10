from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from .archive import archive_path, dir_size, ensure_run_dirs, thumb_path
from .browser import BrowserSession
from .links import body_hash, canonicalize, extract_links, is_same_origin

logger = logging.getLogger(__name__)


class ProgressReporter:
    """Hook for UI layers to observe crawl events. The default impl is silent."""

    def on_start(self, *, run_id: int, start_url: str, max_pages: int,
                 max_size: int | None) -> None:
        pass

    def on_page(self, *, idx: int, depth: int, url: str, status: int | None,
                title: str, archived_bytes: int, queue_size: int,
                archived_count: int, discovered: int, kind: str) -> None:
        """kind in {'archived', 'phantom', 'duplicate', 'external', 'error'}"""
        pass

    def on_halt(self, reason: str) -> None:
        pass

    def on_finish(self, *, archived_count: int, total_pages: int,
                  edges: int) -> None:
        pass


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY,
  name TEXT,
  start_url TEXT NOT NULL,
  start_url_canonical TEXT NOT NULL,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  config_json TEXT NOT NULL,
  homepage_body_hash TEXT,
  halt_reason TEXT
);

CREATE TABLE IF NOT EXISTS pages (
  id INTEGER PRIMARY KEY,
  run_id INTEGER NOT NULL REFERENCES runs(id),
  url_canonical TEXT NOT NULL,
  url_original TEXT NOT NULL,
  http_status INTEGER,
  body_hash TEXT,
  title TEXT,
  is_external INTEGER NOT NULL DEFAULT 0,
  is_phantom_404 INTEGER NOT NULL DEFAULT 0,
  archive_path TEXT,
  thumb_path TEXT,
  depth INTEGER NOT NULL,
  fetched_at TEXT,
  error TEXT,
  UNIQUE(run_id, url_canonical)
);
CREATE INDEX IF NOT EXISTS idx_pages_body_hash ON pages(run_id, body_hash);

CREATE TABLE IF NOT EXISTS edges (
  id INTEGER PRIMARY KEY,
  run_id INTEGER NOT NULL,
  src_page_id INTEGER NOT NULL REFERENCES pages(id),
  dst_url_canonical TEXT NOT NULL,
  link_kind TEXT NOT NULL CHECK(link_kind IN ('a','area')),
  link_text TEXT,
  coords_json TEXT,
  shape TEXT
);
CREATE INDEX IF NOT EXISTS idx_edges_src ON edges(run_id, src_page_id);
CREATE INDEX IF NOT EXISTS idx_edges_dst ON edges(run_id, dst_url_canonical);

CREATE TABLE IF NOT EXISTS pending (
  id INTEGER PRIMARY KEY,
  run_id INTEGER NOT NULL,
  url_canonical TEXT NOT NULL,
  depth INTEGER NOT NULL,
  parent_page_id INTEGER,
  enqueued_at TEXT NOT NULL,
  UNIQUE(run_id, url_canonical)
);
"""


@dataclass
class CrawlConfig:
    start_url: str
    output_dir: Path
    name: str | None = None
    max_pages: int = 100
    max_depth: int = 15
    max_file_size: int | None = None  # bytes; None = unlimited
    delay_ms: int = 250
    page_timeout_ms: int = 30000
    include_subdomains: bool = False
    respect_robots: bool = False
    user_agent: str = "site-cartographer/0.1 (+contact)"
    viewport: tuple[int, int] = (320, 240)
    headless: bool = True
    resume: bool = False

    def to_json_dict(self) -> dict:
        d = asdict(self)
        d["output_dir"] = str(self.output_dir)
        d["viewport"] = list(self.viewport)
        return d


def _now() -> str:
    return datetime.now(UTC).isoformat()


def open_db(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SQL)
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Backfill columns that were added after the initial schema. SQLite
    only supports ADD COLUMN, which is enough for our additive changes."""
    existing = {r[1] for r in conn.execute("PRAGMA table_info(runs)").fetchall()}
    for col in ("name TEXT", "halt_reason TEXT"):
        col_name = col.split()[0]
        if col_name not in existing:
            conn.execute(f"ALTER TABLE runs ADD COLUMN {col}")
    conn.commit()


def start_run(conn: sqlite3.Connection, config: CrawlConfig) -> int:
    start_canonical = canonicalize(config.start_url)
    if config.resume:
        # Find the latest run for this start URL regardless of finished status.
        # Capped runs have finished_at set but may still have pending items;
        # interrupted runs have finished_at NULL. Either is resumable.
        row = conn.execute(
            "SELECT id, finished_at FROM runs WHERE start_url_canonical = ?"
            " ORDER BY id DESC LIMIT 1",
            (start_canonical,),
        ).fetchone()
        if row is not None:
            if row["finished_at"] is not None:
                conn.execute(
                    "UPDATE runs SET finished_at = NULL, halt_reason = NULL"
                    " WHERE id = ?",
                    (row["id"],),
                )
                conn.commit()
            logger.info("resuming run %s", row["id"])
            return row["id"]
    cursor = conn.execute(
        "INSERT INTO runs (name, start_url, start_url_canonical, started_at,"
        " config_json) VALUES (?, ?, ?, ?, ?)",
        (config.name, config.start_url, start_canonical, _now(),
         json.dumps(config.to_json_dict())),
    )
    run_id = cursor.lastrowid
    conn.execute(
        "INSERT OR IGNORE INTO pending (run_id, url_canonical, depth, enqueued_at)"
        " VALUES (?, ?, 0, ?)",
        (run_id, start_canonical, _now()),
    )
    conn.commit()
    return run_id


def finalise_run(conn: sqlite3.Connection, run_id: int,
                 halt_reason: str | None = None) -> None:
    conn.execute(
        "UPDATE runs SET finished_at = ?, halt_reason = ? WHERE id = ?",
        (_now(), halt_reason, run_id),
    )
    conn.commit()


def is_phantom_404(
    homepage_hash: str | None,
    this_hash: str,
    url_canonical: str,
    start_canonical: str,
) -> bool:
    return (
        homepage_hash is not None
        and this_hash == homepage_hash
        and url_canonical != start_canonical
    )


async def crawl(config: CrawlConfig,
                progress: ProgressReporter | None = None) -> Path:
    progress = progress or ProgressReporter()
    layout = ensure_run_dirs(config.output_dir)
    conn = open_db(layout["db"])
    halt_reason: str | None = None
    run_id: int | None = None
    try:
        run_id = start_run(conn, config)
        start_canonical = canonicalize(config.start_url)
        progress.on_start(
            run_id=run_id, start_url=config.start_url,
            max_pages=config.max_pages, max_size=config.max_file_size,
        )
        try:
            async with BrowserSession(
                headless=config.headless,
                viewport=config.viewport,
                user_agent=config.user_agent,
            ) as browser:
                halt_reason = await _crawl_loop(
                    conn, run_id, browser, config, start_canonical, progress,
                )
        except (asyncio.CancelledError, KeyboardInterrupt):
            # Always mark the run as interrupted before unwinding so a
            # subsequent resume picks up cleanly. The pending queue is
            # already safe — every iteration commits before sleeping.
            halt_reason = "interrupted by user"
            progress.on_halt(halt_reason)
            _finalise_with_progress(conn, run_id, halt_reason, progress)
            raise
        _finalise_with_progress(conn, run_id, halt_reason, progress)
    finally:
        conn.close()
    return layout["root"]


def _finalise_with_progress(
    conn: sqlite3.Connection,
    run_id: int,
    halt_reason: str | None,
    progress: ProgressReporter,
) -> None:
    archived = conn.execute(
        "SELECT COUNT(*) FROM pages WHERE run_id = ? AND archive_path IS NOT NULL",
        (run_id,),
    ).fetchone()[0]
    total = conn.execute(
        "SELECT COUNT(*) FROM pages WHERE run_id = ?", (run_id,),
    ).fetchone()[0]
    edges = conn.execute(
        "SELECT COUNT(*) FROM edges WHERE run_id = ?", (run_id,),
    ).fetchone()[0]
    finalise_run(conn, run_id, halt_reason)
    progress.on_finish(archived_count=archived, total_pages=total, edges=edges)


async def _crawl_loop(
    conn: sqlite3.Connection,
    run_id: int,
    browser: BrowserSession,
    config: CrawlConfig,
    start_canonical: str,
    progress: ProgressReporter,
) -> str | None:
    while True:
        pages_done = conn.execute(
            "SELECT COUNT(*) AS n FROM pages WHERE run_id = ?"
            " AND is_external = 0 AND is_phantom_404 = 0",
            (run_id,),
        ).fetchone()["n"]
        if pages_done >= config.max_pages:
            reason = f"reached max-pages cap ({config.max_pages})"
            logger.info(reason)
            progress.on_halt(reason)
            return reason

        if config.max_file_size is not None:
            size = dir_size(config.output_dir)
            if size >= config.max_file_size:
                reason = (f"reached max-file-size cap "
                          f"({size}B >= {config.max_file_size}B)")
                logger.info(reason)
                progress.on_halt(reason)
                return reason

        row = conn.execute(
            "SELECT id, url_canonical, depth FROM pending WHERE run_id = ?"
            " ORDER BY id ASC LIMIT 1",
            (run_id,),
        ).fetchone()
        if row is None:
            reason = "queue drained"
            logger.info(reason)
            progress.on_halt(reason)
            return None

        pending_id = row["id"]
        url = row["url_canonical"]
        depth = row["depth"]

        if depth > config.max_depth:
            conn.execute("DELETE FROM pending WHERE id = ?", (pending_id,))
            conn.commit()
            continue

        existing = conn.execute(
            "SELECT id FROM pages WHERE run_id = ? AND url_canonical = ?",
            (run_id, url),
        ).fetchone()
        if existing is not None:
            conn.execute("DELETE FROM pending WHERE id = ?", (pending_id,))
            conn.commit()
            continue

        logger.info("[%d] depth=%d %s", pages_done + 1, depth, url)
        page_kind, status, title = await _fetch_one(
            conn, run_id, browser, config, start_canonical, url, depth,
        )
        conn.execute("DELETE FROM pending WHERE id = ?", (pending_id,))
        conn.commit()

        archived_count = conn.execute(
            "SELECT COUNT(*) FROM pages WHERE run_id = ? AND archive_path IS NOT NULL",
            (run_id,),
        ).fetchone()[0]
        discovered = conn.execute(
            "SELECT COUNT(*) FROM pages WHERE run_id = ?",
            (run_id,),
        ).fetchone()[0] + conn.execute(
            "SELECT COUNT(*) FROM pending WHERE run_id = ?", (run_id,),
        ).fetchone()[0]
        queue_size = conn.execute(
            "SELECT COUNT(*) FROM pending WHERE run_id = ?", (run_id,),
        ).fetchone()[0]
        archived_bytes = dir_size(config.output_dir)

        progress.on_page(
            idx=pages_done + 1, depth=depth, url=url, status=status,
            title=title, archived_bytes=archived_bytes, queue_size=queue_size,
            archived_count=archived_count, discovered=discovered,
            kind=page_kind,
        )

        await asyncio.sleep(config.delay_ms / 1000)


async def _fetch_one(
    conn: sqlite3.Connection,
    run_id: int,
    browser: BrowserSession,
    config: CrawlConfig,
    start_canonical: str,
    url: str,
    depth: int,
) -> tuple[str, int | None, str]:
    """Returns (kind, http_status, title) where kind is one of
    'archived' | 'phantom' | 'duplicate' | 'error'.
    """
    async with browser.open_page(url, timeout_ms=config.page_timeout_ms) as ph:
        if ph.error is not None:
            conn.execute(
                "INSERT OR IGNORE INTO pages (run_id, url_canonical, url_original,"
                " http_status, depth, fetched_at, error)"
                " VALUES (?, ?, ?, NULL, ?, ?, ?)",
                (run_id, url, url, depth, _now(), ph.error),
            )
            return ("error", None, "")

        html = await ph.html()
        title = await ph.title()
        bhash = body_hash(html)
        final_url = ph.page.url
        status = ph.response.status if ph.response is not None else None

        homepage_hash = conn.execute(
            "SELECT homepage_body_hash FROM runs WHERE id = ?", (run_id,),
        ).fetchone()["homepage_body_hash"]
        if homepage_hash is None:
            conn.execute(
                "UPDATE runs SET homepage_body_hash = ? WHERE id = ?",
                (bhash, run_id),
            )
            homepage_hash = bhash

        phantom = is_phantom_404(homepage_hash, bhash, url, start_canonical)
        body_dup_row = conn.execute(
            "SELECT id FROM pages WHERE run_id = ? AND body_hash = ? LIMIT 1",
            (run_id, bhash),
        ).fetchone()
        is_dup_body = body_dup_row is not None and not phantom

        cursor = conn.execute(
            "INSERT INTO pages (run_id, url_canonical, url_original, http_status,"
            " body_hash, title, is_phantom_404, depth, fetched_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run_id, url, final_url, status, bhash, title,
                1 if phantom else 0, depth, _now(),
            ),
        )
        page_id = cursor.lastrowid

        if phantom:
            logger.debug("phantom 404: %s", url)
            return ("phantom", status, title)
        if is_dup_body:
            logger.debug("duplicate body, skipping link extraction: %s", url)
            return ("duplicate", status, title)

        a_path = archive_path(config.output_dir, url)
        t_path = thumb_path(config.output_dir, url)
        try:
            await ph.save_thumbnail(t_path)
        except Exception as e:
            logger.warning("thumbnail failed for %s: %s", url, e)
        try:
            await ph.save_inline_html(a_path)
        except Exception as e:
            logger.warning("archive failed for %s: %s", url, e)

        rel_a = a_path.relative_to(config.output_dir).as_posix() if a_path.exists() else None
        rel_t = t_path.relative_to(config.output_dir).as_posix() if t_path.exists() else None
        conn.execute(
            "UPDATE pages SET archive_path = ?, thumb_path = ? WHERE id = ?",
            (rel_a, rel_t, page_id),
        )

        for link in extract_links(html, base_url=final_url):
            coords_json: str | None = None
            if link.kind == "area" and link.coords is not None:
                coords_json = json.dumps({
                    "shape": link.shape,
                    "coords": link.coords,
                    "image_src": link.image_src,
                })

            conn.execute(
                "INSERT INTO edges (run_id, src_page_id, dst_url_canonical,"
                " link_kind, link_text, coords_json, shape)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (run_id, page_id, link.url, link.kind, link.text,
                 coords_json, link.shape),
            )

            same_origin = is_same_origin(
                link.url, start_canonical,
                include_subdomains=config.include_subdomains,
            )
            existing_dst = conn.execute(
                "SELECT id FROM pages WHERE run_id = ? AND url_canonical = ?",
                (run_id, link.url),
            ).fetchone()

            if same_origin:
                if existing_dst is None:
                    conn.execute(
                        "INSERT OR IGNORE INTO pending (run_id, url_canonical,"
                        " depth, parent_page_id, enqueued_at)"
                        " VALUES (?, ?, ?, ?, ?)",
                        (run_id, link.url, depth + 1, page_id, _now()),
                    )
            else:
                if existing_dst is None:
                    conn.execute(
                        "INSERT INTO pages (run_id, url_canonical, url_original,"
                        " is_external, depth, fetched_at)"
                        " VALUES (?, ?, ?, 1, ?, ?)",
                        (run_id, link.url, link.url, depth + 1, _now()),
                    )

        conn.commit()
        return ("archived", status, title)
