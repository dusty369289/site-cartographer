from __future__ import annotations

import hashlib
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


def mhtml_path(run_dir: Path, url_canonical: str) -> Path:
    return run_layout(run_dir)["pages"] / f"{page_key(url_canonical)}.mhtml"


def thumb_path(run_dir: Path, url_canonical: str) -> Path:
    return run_layout(run_dir)["thumbs"] / f"{page_key(url_canonical)}.png"
