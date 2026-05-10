from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path


def page_key(url_canonical: str) -> str:
    """Stable filename stem derived from a canonical URL.

    16 hex chars of sha1 — short enough to avoid Windows path-length issues,
    long enough that collisions on a ~10k-page crawl are vanishing.
    """
    return hashlib.sha1(url_canonical.encode("utf-8")).hexdigest()[:16]


def run_layout(run_dir: Path) -> dict[str, Path]:
    """Standard subdirectories under a single crawl run."""
    return {
        "root": run_dir,
        "pages": run_dir / "pages",
        "thumbs": run_dir / "thumbs",
        "viewer": run_dir / "viewer",
        "db": run_dir / "crawl.sqlite",
    }


def ensure_run_dirs(run_dir: Path) -> dict[str, Path]:
    layout = run_layout(run_dir)
    for key in ("root", "pages", "thumbs", "viewer"):
        layout[key].mkdir(parents=True, exist_ok=True)
    return layout


def archive_path(run_dir: Path, url_canonical: str) -> Path:
    return run_layout(run_dir)["pages"] / f"{page_key(url_canonical)}.html"


def thumb_path(run_dir: Path, url_canonical: str) -> Path:
    return run_layout(run_dir)["thumbs"] / f"{page_key(url_canonical)}.png"


_SIZE_RE = re.compile(r"^\s*([\d.]+)\s*([KMGT]?)B?\s*$", re.IGNORECASE)
_SIZE_MULT = {"": 1, "K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}


def parse_size(s: str) -> int:
    """Parse a human-readable byte size into an int.

    Accepts: '500', '500B', '100K', '100KB', '1.5G', '2GB', '1T'. Case-insensitive.
    """
    if isinstance(s, int):
        return s
    m = _SIZE_RE.match(s)
    if not m:
        raise ValueError(f"unrecognised size: {s!r}")
    value = float(m.group(1))
    unit = m.group(2).upper()
    return int(value * _SIZE_MULT[unit])


def format_size(n: int) -> str:
    """Inverse of parse_size: return a short human-readable form."""
    for unit, mult in (("T", 1024**4), ("G", 1024**3), ("M", 1024**2), ("K", 1024)):
        if n >= mult:
            return f"{n / mult:.1f}{unit}B"
    return f"{n}B"


def dir_size(path: Path) -> int:
    """Total size in bytes of every regular file under *path* (recursive)."""
    total = 0
    if not path.exists():
        return 0
    for p in path.rglob("*"):
        if p.is_file():
            try:
                total += p.stat().st_size
            except OSError:
                pass
    return total


@dataclass
class RunSummary:
    dir: Path
    run_id: int | None
    name: str | None
    start_url: str | None
    started_at: str | None
    finished_at: str | None
    halt_reason: str | None
    page_count: int
    archived_count: int
    edge_count: int
    total_bytes: int

    @property
    def display_name(self) -> str:
        return self.name or self.dir.name


def list_runs(base_dir: Path) -> list[RunSummary]:
    """Inspect every immediate subdirectory of *base_dir* for a crawl.sqlite
    and return a summary, sorted newest-first.
    """
    if not base_dir.is_dir():
        return []
    summaries: list[RunSummary] = []
    for child in sorted(base_dir.iterdir(), reverse=True):
        if not child.is_dir():
            continue
        db = child / "crawl.sqlite"
        if not db.is_file():
            continue
        try:
            summaries.append(_summarise_run(child, db))
        except Exception:
            continue
    return summaries


def _summarise_run(run_dir: Path, db: Path) -> RunSummary:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(runs)").fetchall()}
        select_cols = ["id", "start_url", "started_at", "finished_at"]
        if "name" in cols:
            select_cols.append("name")
        if "halt_reason" in cols:
            select_cols.append("halt_reason")
        run_row = conn.execute(
            f"SELECT {', '.join(select_cols)} FROM runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if run_row is None:
            page_count = archived_count = edge_count = 0
        else:
            page_count = conn.execute(
                "SELECT COUNT(*) FROM pages WHERE run_id = ?", (run_row["id"],)
            ).fetchone()[0]
            archived_count = conn.execute(
                "SELECT COUNT(*) FROM pages WHERE run_id = ? AND archive_path IS NOT NULL",
                (run_row["id"],),
            ).fetchone()[0]
            edge_count = conn.execute(
                "SELECT COUNT(*) FROM edges WHERE run_id = ?", (run_row["id"],)
            ).fetchone()[0]
    finally:
        conn.close()

    def _maybe(row, key):
        if row is None:
            return None
        try:
            return row[key]
        except (IndexError, KeyError):
            return None

    return RunSummary(
        dir=run_dir,
        run_id=_maybe(run_row, "id"),
        name=_maybe(run_row, "name"),
        start_url=_maybe(run_row, "start_url"),
        started_at=_maybe(run_row, "started_at"),
        finished_at=_maybe(run_row, "finished_at"),
        halt_reason=_maybe(run_row, "halt_reason"),
        page_count=page_count,
        archived_count=archived_count,
        edge_count=edge_count,
        total_bytes=dir_size(run_dir),
    )
