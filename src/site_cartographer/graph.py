"""Build the graph.json that the Sigma.js viewer consumes.

Pulls pages + edges out of SQLite, collapses duplicate-body URL aliases into
their canonical, and emits a flat `{nodes:[...], edges:[...]}` JSON file.
The viewer runs d3-force in-browser so layout positions are not baked here.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path

from .archive import page_key, run_layout

logger = logging.getLogger(__name__)


def _to_url_path(p: str | None) -> str | None:
    return p.replace("\\", "/") if p else p


def export_graph_json(run_dir: Path, run_id: int | None = None) -> Path:
    """Read SQLite for the latest (or specified) run and write graph.json."""
    layout = run_layout(run_dir)
    conn = sqlite3.connect(layout["db"])
    conn.row_factory = sqlite3.Row
    try:
        if run_id is None:
            row = conn.execute(
                "SELECT id FROM runs ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if row is None:
                raise ValueError(f"no runs found in {layout['db']}")
            run_id = row["id"]

        nodes, edges = _collect(conn, run_id)
    finally:
        conn.close()

    out = layout["root"] / "graph.json"
    out.write_text(
        json.dumps({"nodes": nodes, "edges": edges}, indent=2),
        encoding="utf-8",
    )
    return out


# Back-compat alias for callers that imported the old name.
export_cytoscape_json = export_graph_json


def _collect(conn: sqlite3.Connection, run_id: int) -> tuple[list[dict], list[dict]]:
    nodes: list[dict] = []
    seen_ids: set[str] = set()
    alias_to_canonical: dict[str, str] = {}
    canonical_aliases: dict[str, list[str]] = {}

    # body_hash -> (canonical_id, archive_path, thumb_path) for the first
    # archived page we see at each hash.
    body_canonical: dict[str, tuple[str, str, str]] = {}
    for row in conn.execute(
        "SELECT body_hash, url_canonical, archive_path, thumb_path"
        " FROM pages WHERE run_id = ? AND archive_path IS NOT NULL"
        " AND body_hash IS NOT NULL",
        (run_id,),
    ):
        if row["body_hash"] not in body_canonical:
            body_canonical[row["body_hash"]] = (
                page_key(row["url_canonical"]),
                _to_url_path(row["archive_path"]),
                _to_url_path(row["thumb_path"]),
            )

    for row in conn.execute(
        "SELECT url_canonical, title, thumb_path, archive_path, body_hash,"
        " is_external, is_phantom_404, http_status, depth"
        " FROM pages WHERE run_id = ?",
        (run_id,),
    ):
        node_id = page_key(row["url_canonical"])
        archive = _to_url_path(row["archive_path"])

        if archive is None and row["body_hash"] in body_canonical:
            canon_id, _, _ = body_canonical[row["body_hash"]]
            if canon_id != node_id:
                alias_to_canonical[node_id] = canon_id
                canonical_aliases.setdefault(canon_id, []).append(
                    row["url_canonical"]
                )
                continue

        seen_ids.add(node_id)
        thumb = _to_url_path(row["thumb_path"])
        nodes.append({
            "id": node_id,
            "url": row["url_canonical"],
            "label": row["title"] or row["url_canonical"],
            "thumb": thumb,
            "archive": archive,
            "is_external": bool(row["is_external"]),
            "is_phantom_404": bool(row["is_phantom_404"]),
            "http_status": row["http_status"],
            "depth": row["depth"],
        })

    for n in nodes:
        aliases = canonical_aliases.get(n["id"])
        if aliases:
            n["aliases"] = aliases
            n["alias_count"] = len(aliases)

    edges: list[dict] = []
    seen_edge_keys: set[tuple[str, str, str]] = set()
    edge_seq = 0
    for row in conn.execute(
        "SELECT src_page_id, dst_url_canonical, link_kind, link_text,"
        " coords_json, shape FROM edges WHERE run_id = ?",
        (run_id,),
    ):
        src_row = conn.execute(
            "SELECT url_canonical FROM pages WHERE id = ?",
            (row["src_page_id"],),
        ).fetchone()
        if src_row is None:
            continue
        src_id = alias_to_canonical.get(
            page_key(src_row["url_canonical"]), page_key(src_row["url_canonical"])
        )
        dst_id = alias_to_canonical.get(
            page_key(row["dst_url_canonical"]), page_key(row["dst_url_canonical"])
        )
        if src_id == dst_id:
            continue

        edge_key = (src_id, dst_id, row["link_kind"])
        if edge_key in seen_edge_keys:
            continue
        seen_edge_keys.add(edge_key)

        if dst_id not in seen_ids:
            seen_ids.add(dst_id)
            nodes.append({
                "id": dst_id,
                "url": row["dst_url_canonical"],
                "label": row["dst_url_canonical"],
                "thumb": None,
                "archive": None,
                "is_external": False,
                "is_phantom_404": False,
                "is_unvisited": True,
                "http_status": None,
                "depth": None,
            })

        edge_seq += 1
        edges.append({
            "id": f"e{edge_seq}",
            "source": src_id,
            "target": dst_id,
            "kind": row["link_kind"],
            "text": row["link_text"] or "",
            "shape": row["shape"],
            "coords_json": row["coords_json"],
        })

    return nodes, edges
