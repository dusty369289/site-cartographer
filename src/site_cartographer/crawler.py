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
from .links import (
    DEFAULT_EXTRACTORS,
    KNOWN_EXTRACTORS,
    SCOPE_MODES,
    body_hash,
    canonicalize,
    extract_links,
    is_in_scope,
)

logger = logging.getLogger(__name__)


class ProgressReporter:
    """Hook for UI layers to observe crawl events. The default impl is silent."""

    def on_start(self, *, run_id: int, start_url: str, max_pages: int,
                 max_size: int | None, workers: int = 1) -> None:
        pass

    def on_page(self, *, idx: int, depth: int, url: str, status: int | None,
                title: str, archived_bytes: int, queue_size: int,
                archived_count: int, discovered: int, kind: str,
                in_flight: int = 0) -> None:
        """kind in {'archived', 'phantom', 'duplicate', 'external', 'error'}"""
        pass

    def on_halt(self, reason: str) -> None:
        pass

    def on_finish(self, *, archived_count: int, total_pages: int,
                  edges: int) -> None:
        pass


@dataclass
class _CrawlState:
    """Shared coordination state between worker tasks."""
    halt_reason: str | None = None
    stop: bool = False
    in_flight: int = 0  # workers currently mid-fetch (post-claim, pre-finish)


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
  halt_reason TEXT,
  dropped_for_depth INTEGER NOT NULL DEFAULT 0
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
  is_fetch_candidate INTEGER NOT NULL DEFAULT 0,
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
  link_kind TEXT NOT NULL,
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
  claimed_at TEXT,
  UNIQUE(run_id, url_canonical)
);
"""


EXTERNAL_POLICIES = ("ignore", "metadata", "archive", "crawl")


MAX_WORKERS = 8


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
    parallel_workers: int = 1
    include_subdomains: bool = False  # legacy alias for scope_mode="descendants"
    scope_mode: str = "host"
    scope_value: str = ""
    respect_robots: bool = False
    external_policy: str = "metadata"
    link_extractors: tuple[str, ...] = DEFAULT_EXTRACTORS
    custom_link_regex: str | None = None
    user_agent: str = "site-cartographer/0.1 (+contact)"
    viewport: tuple[int, int] = (320, 240)
    headless: bool = True
    resume: bool = False

    def __post_init__(self) -> None:
        if self.external_policy not in EXTERNAL_POLICIES:
            raise ValueError(
                f"external_policy must be one of {EXTERNAL_POLICIES},"
                f" got {self.external_policy!r}"
            )
        if not 1 <= self.parallel_workers <= MAX_WORKERS:
            raise ValueError(
                f"parallel_workers must be between 1 and {MAX_WORKERS},"
                f" got {self.parallel_workers}"
            )
        # Promote legacy include_subdomains -> scope_mode unless caller set
        # scope_mode explicitly.
        if self.include_subdomains and self.scope_mode == "host":
            object.__setattr__(self, "scope_mode", "descendants")
        if self.scope_mode not in SCOPE_MODES:
            raise ValueError(
                f"scope_mode must be one of {SCOPE_MODES}, got {self.scope_mode!r}"
            )
        # Coerce + validate extractors
        ext = tuple(e.strip() for e in self.link_extractors if e and e.strip())
        unknown = [e for e in ext if e not in KNOWN_EXTRACTORS]
        if unknown:
            raise ValueError(
                f"unknown link extractor(s): {unknown}; "
                f"valid: {list(KNOWN_EXTRACTORS)}"
            )
        if not ext:
            ext = DEFAULT_EXTRACTORS
        # Use object.__setattr__ to bypass dataclass frozen rules (we're not
        # frozen but we built a normalised tuple).
        object.__setattr__(self, "link_extractors", ext)

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
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA_SQL)
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Backfill columns added after the initial schema. SQLite only supports
    ADD COLUMN, which is enough for our additive changes."""
    runs_cols = {r[1] for r in conn.execute("PRAGMA table_info(runs)").fetchall()}
    for col in (
        "name TEXT",
        "halt_reason TEXT",
        "dropped_for_depth INTEGER NOT NULL DEFAULT 0",
    ):
        col_name = col.split()[0]
        if col_name not in runs_cols:
            conn.execute(f"ALTER TABLE runs ADD COLUMN {col}")
    pages_cols = {r[1] for r in conn.execute("PRAGMA table_info(pages)").fetchall()}
    if "is_fetch_candidate" not in pages_cols:
        conn.execute(
            "ALTER TABLE pages ADD COLUMN is_fetch_candidate INTEGER NOT NULL DEFAULT 0"
        )
    if "archive_in_progress" not in pages_cols:
        conn.execute(
            "ALTER TABLE pages ADD COLUMN archive_in_progress INTEGER NOT NULL DEFAULT 0"
        )
    pending_cols = {r[1] for r in conn.execute("PRAGMA table_info(pending)").fetchall()}
    if "claimed_at" not in pending_cols:
        conn.execute("ALTER TABLE pending ADD COLUMN claimed_at TEXT")

    # Drop the legacy CHECK(link_kind IN ('a','area')) constraint so new
    # extractor kinds (iframe, form, etc.) can be inserted into older DBs.
    edges_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='edges'"
    ).fetchone()
    if edges_sql and "CHECK" in edges_sql[0] and "link_kind IN" in edges_sql[0]:
        conn.executescript("""
            CREATE TABLE edges_new (
              id INTEGER PRIMARY KEY,
              run_id INTEGER NOT NULL,
              src_page_id INTEGER NOT NULL REFERENCES pages(id),
              dst_url_canonical TEXT NOT NULL,
              link_kind TEXT NOT NULL,
              link_text TEXT,
              coords_json TEXT,
              shape TEXT
            );
            INSERT INTO edges_new SELECT id, run_id, src_page_id,
              dst_url_canonical, link_kind, link_text, coords_json, shape
              FROM edges;
            DROP TABLE edges;
            ALTER TABLE edges_new RENAME TO edges;
            CREATE INDEX IF NOT EXISTS idx_edges_src ON edges(run_id, src_page_id);
            CREATE INDEX IF NOT EXISTS idx_edges_dst ON edges(run_id, dst_url_canonical);
        """)
    conn.commit()


def _reset_in_flight(conn: sqlite3.Connection, run_id: int) -> None:
    """Clear stale in-flight markers from a previous worker that didn't get
    to clean up (e.g. interrupted mid-fetch). Called on resume to make every
    pending/external row eligible for re-claiming."""
    conn.execute(
        "UPDATE pending SET claimed_at = NULL"
        " WHERE run_id = ? AND claimed_at IS NOT NULL",
        (run_id,),
    )
    conn.execute(
        "UPDATE pages SET archive_in_progress = 0"
        " WHERE run_id = ? AND archive_in_progress = 1",
        (run_id,),
    )
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
            _reset_in_flight(conn, row["id"])
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
            workers=config.parallel_workers,
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
                # Externals pass runs after the BFS regardless of whether it
                # drained or capped — externals are bounded by their own size
                # cap, not by max_pages.
                if config.external_policy in ("archive", "crawl"):
                    ext_halt = await _externals_pass(
                        conn, run_id, browser, config, start_canonical, progress,
                    )
                    if ext_halt is not None:
                        halt_reason = ext_halt
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


def _claim_external(conn: sqlite3.Connection, run_id: int):
    """Atomic claim for an external page in the externals pass."""
    row = conn.execute(
        "SELECT id, url_canonical, depth FROM pages"
        " WHERE run_id = ? AND is_external = 1 AND is_fetch_candidate = 1"
        " AND archive_path IS NULL AND archive_in_progress = 0"
        " ORDER BY id ASC LIMIT 1",
        (run_id,),
    ).fetchone()
    if row is None:
        return None
    conn.execute(
        "UPDATE pages SET archive_in_progress = 1 WHERE id = ?", (row["id"],)
    )
    conn.commit()
    return row


async def _externals_pass(
    conn: sqlite3.Connection,
    run_id: int,
    browser: BrowserSession,
    config: CrawlConfig,
    start_canonical: str,
    progress: ProgressReporter,
) -> str | None:
    """After the main BFS, fetch externals that were marked is_fetch_candidate.

    Honours max_file_size but not max_pages — externals are bounded by size.
    For 'crawl' policy, also extracts links from the external page (one hop
    only); newly-discovered destinations become metadata-only nodes.
    Runs config.parallel_workers concurrent fetches.
    """
    state = _CrawlState()
    workers = [
        asyncio.create_task(_external_worker(
            i, state, conn, run_id, browser, config, start_canonical, progress,
        ))
        for i in range(config.parallel_workers)
    ]
    try:
        await asyncio.gather(*workers)
    except (asyncio.CancelledError, KeyboardInterrupt):
        state.stop = True
        for w in workers:
            if not w.done():
                w.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        raise
    return state.halt_reason


async def _external_worker(
    worker_id: int,
    state: _CrawlState,
    conn: sqlite3.Connection,
    run_id: int,
    browser: BrowserSession,
    config: CrawlConfig,
    start_canonical: str,
    progress: ProgressReporter,
) -> None:
    while not state.stop:
        if config.max_file_size is not None:
            size = dir_size(config.output_dir)
            if size >= config.max_file_size:
                if state.halt_reason is None:
                    state.halt_reason = (
                        f"reached max-file-size during externals pass "
                        f"({size}B >= {config.max_file_size}B)"
                    )
                    logger.info(state.halt_reason)
                    progress.on_halt(state.halt_reason)
                state.stop = True
                return

        claimed = _claim_external(conn, run_id)
        if claimed is None:
            if state.in_flight == 0:
                state.stop = True
                return
            await asyncio.sleep(0.1)
            continue

        page_id = claimed["id"]
        url = claimed["url_canonical"]
        depth = claimed["depth"]

        state.in_flight += 1
        try:
            logger.info("[ext-w%d] %s", worker_id, url)
            await _fetch_external(conn, run_id, browser, config, page_id, url, depth)
        finally:
            conn.execute(
                "UPDATE pages SET archive_in_progress = 0 WHERE id = ?",
                (page_id,),
            )
            conn.commit()
            state.in_flight -= 1

        archived_count = conn.execute(
            "SELECT COUNT(*) FROM pages WHERE run_id = ? AND archive_path IS NOT NULL",
            (run_id,),
        ).fetchone()[0]
        discovered = conn.execute(
            "SELECT COUNT(*) FROM pages WHERE run_id = ?", (run_id,),
        ).fetchone()[0]
        queue_size = conn.execute(
            "SELECT COUNT(*) FROM pages WHERE run_id = ? AND is_external = 1"
            " AND is_fetch_candidate = 1 AND archive_path IS NULL"
            " AND archive_in_progress = 0",
            (run_id,),
        ).fetchone()[0]
        meta_row = conn.execute(
            "SELECT title, http_status FROM pages WHERE id = ?", (page_id,),
        ).fetchone()

        progress.on_page(
            idx=archived_count, depth=depth, url=url,
            status=meta_row["http_status"] if meta_row else None,
            title=meta_row["title"] if meta_row else "",
            archived_bytes=dir_size(config.output_dir),
            queue_size=queue_size,
            archived_count=archived_count,
            discovered=discovered,
            kind="archived",
            in_flight=state.in_flight,
        )

        await asyncio.sleep(config.delay_ms / 1000)


async def _fetch_external(
    conn: sqlite3.Connection,
    run_id: int,
    browser: BrowserSession,
    config: CrawlConfig,
    page_id: int,
    url: str,
    depth: int,
) -> None:
    async with browser.open_page(url, timeout_ms=config.page_timeout_ms) as ph:
        if ph.error is not None:
            conn.execute(
                "UPDATE pages SET error = ?, is_fetch_candidate = 0 WHERE id = ?",
                (ph.error, page_id),
            )
            conn.commit()
            return

        title = await ph.title()
        final_url = ph.page.url
        status = ph.response.status if ph.response is not None else None

        a_path = archive_path(config.output_dir, url)
        t_path = thumb_path(config.output_dir, url)
        try:
            await ph.save_thumbnail(t_path)
        except Exception as e:
            logger.warning("ext thumbnail failed for %s: %s", url, e)
        try:
            await ph.save_inline_html(a_path)
        except Exception as e:
            logger.warning("ext archive failed for %s: %s", url, e)

        rel_a = a_path.relative_to(config.output_dir).as_posix() if a_path.exists() else None
        rel_t = t_path.relative_to(config.output_dir).as_posix() if t_path.exists() else None
        conn.execute(
            "UPDATE pages SET title = ?, http_status = ?, archive_path = ?,"
            " thumb_path = ?, fetched_at = ? WHERE id = ?",
            (title, status, rel_a, rel_t, _now(), page_id),
        )

        if config.external_policy == "crawl":
            html = await ph.html()
            for link in extract_links(
            html, base_url=final_url,
            extractors=set(config.link_extractors),
            custom_regex=config.custom_link_regex,
        ):
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
                # second-level externals are metadata-only — never fetched.
                existing_dst = conn.execute(
                    "SELECT id FROM pages WHERE run_id = ? AND url_canonical = ?",
                    (run_id, link.url),
                ).fetchone()
                if existing_dst is None:
                    conn.execute(
                        "INSERT INTO pages (run_id, url_canonical, url_original,"
                        " is_external, is_fetch_candidate, depth, fetched_at)"
                        " VALUES (?, ?, ?, 1, 0, ?, ?)",
                        (run_id, link.url, link.url, depth + 1, _now()),
                    )

        conn.commit()


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


def _claim_pending(conn: sqlite3.Connection, run_id: int):
    """Atomically pop the next un-claimed pending row. Synchronous — runs
    to completion without yielding, so concurrent asyncio workers each get
    a unique row."""
    row = conn.execute(
        "SELECT id, url_canonical, depth FROM pending"
        " WHERE run_id = ? AND claimed_at IS NULL"
        " ORDER BY id ASC LIMIT 1",
        (run_id,),
    ).fetchone()
    if row is None:
        return None
    conn.execute(
        "UPDATE pending SET claimed_at = ? WHERE id = ?",
        (_now(), row["id"]),
    )
    conn.commit()
    return row


async def _crawl_loop(
    conn: sqlite3.Connection,
    run_id: int,
    browser: BrowserSession,
    config: CrawlConfig,
    start_canonical: str,
    progress: ProgressReporter,
) -> str | None:
    state = _CrawlState()
    workers = [
        asyncio.create_task(_bfs_worker(
            i, state, conn, run_id, browser, config, start_canonical, progress,
        ))
        for i in range(config.parallel_workers)
    ]
    try:
        await asyncio.gather(*workers)
    except (asyncio.CancelledError, KeyboardInterrupt):
        state.stop = True
        for w in workers:
            if not w.done():
                w.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        raise
    return state.halt_reason


async def _bfs_worker(
    worker_id: int,
    state: _CrawlState,
    conn: sqlite3.Connection,
    run_id: int,
    browser: BrowserSession,
    config: CrawlConfig,
    start_canonical: str,
    progress: ProgressReporter,
) -> None:
    while not state.stop:
        pages_done = conn.execute(
            "SELECT COUNT(*) FROM pages WHERE run_id = ?"
            " AND is_external = 0 AND is_phantom_404 = 0",
            (run_id,),
        ).fetchone()[0]
        if pages_done >= config.max_pages:
            if state.halt_reason is None:
                state.halt_reason = f"reached max-pages cap ({config.max_pages})"
                logger.info(state.halt_reason)
                progress.on_halt(state.halt_reason)
            state.stop = True
            return

        if config.max_file_size is not None:
            size = dir_size(config.output_dir)
            if size >= config.max_file_size:
                if state.halt_reason is None:
                    state.halt_reason = (
                        f"reached max-file-size cap "
                        f"({size}B >= {config.max_file_size}B)"
                    )
                    logger.info(state.halt_reason)
                    progress.on_halt(state.halt_reason)
                state.stop = True
                return

        claimed = _claim_pending(conn, run_id)
        if claimed is None:
            # Queue is empty — but other workers may still be mid-fetch and
            # will enqueue new URLs. Only quit when everyone is idle too.
            # Note: a drained queue is the natural end-state, not a halt;
            # we leave halt_reason as None so the run is recorded as
            # finished cleanly rather than "halted because we ran out".
            if state.in_flight == 0:
                state.stop = True
                return
            await asyncio.sleep(0.1)
            continue

        pending_id = claimed["id"]
        url = claimed["url_canonical"]
        depth = claimed["depth"]

        if depth > config.max_depth:
            conn.execute("DELETE FROM pending WHERE id = ?", (pending_id,))
            conn.execute(
                "UPDATE runs SET dropped_for_depth = dropped_for_depth + 1"
                " WHERE id = ?",
                (run_id,),
            )
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

        state.in_flight += 1
        try:
            logger.info("[w%d] depth=%d %s", worker_id, depth, url)
            page_kind, status, title = await _fetch_one(
                conn, run_id, browser, config, start_canonical, url, depth,
            )
        finally:
            conn.execute("DELETE FROM pending WHERE id = ?", (pending_id,))
            conn.commit()
            state.in_flight -= 1

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

        progress.on_page(
            idx=archived_count, depth=depth, url=url, status=status,
            title=title, archived_bytes=dir_size(config.output_dir),
            queue_size=queue_size, archived_count=archived_count,
            discovered=discovered, kind=page_kind,
            in_flight=state.in_flight,
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

        for link in extract_links(
            html, base_url=final_url,
            extractors=set(config.link_extractors),
            custom_regex=config.custom_link_regex,
        ):
            same_origin = is_in_scope(
                link.url, start_canonical,
                scope_mode=config.scope_mode,
                scope_value=config.scope_value,
            )
            if not same_origin and config.external_policy == "ignore":
                continue  # silently drop the edge AND the destination

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
                    fetch_candidate = (
                        1 if config.external_policy in ("archive", "crawl") else 0
                    )
                    conn.execute(
                        "INSERT INTO pages (run_id, url_canonical, url_original,"
                        " is_external, is_fetch_candidate, depth, fetched_at)"
                        " VALUES (?, ?, ?, 1, ?, ?, ?)",
                        (run_id, link.url, link.url, fetch_candidate,
                         depth + 1, _now()),
                    )

        conn.commit()
        return ("archived", status, title)
