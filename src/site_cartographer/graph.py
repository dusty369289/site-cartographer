from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .archive import page_key, run_layout


def _to_url_path(p: str | None) -> str | None:
    """Normalise stored path to forward-slash form for use as a relative URL."""
    return p.replace("\\", "/") if p else p


def export_cytoscape_json(run_dir: Path, run_id: int | None = None) -> Path:
    """Read SQLite for the latest (or specified) run and write graph.json
    in Cytoscape.js elements format. Duplicate-body URLs (different paths
    serving identical content) are folded into their canonical: the alias
    nodes are not emitted, edges pointing at them are rewritten to target
    the canonical, and the canonical exposes alias URLs in `aliases`.
    Returns the JSON path.
    """
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

        nodes: list[dict] = []
        seen_ids: set[str] = set()
        # node_id -> canonical_id when this node is an alias of another
        alias_to_canonical: dict[str, str] = {}
        # canonical_id -> list of alias urls (for display in the panel)
        canonical_aliases: dict[str, list[str]] = {}

        # Build a mapping from body_hash -> canonical archived page id, so
        # we can mark duplicate-body pages (same content under a different URL)
        # and point them at the page whose archive actually exists.
        body_canonical: dict[str, tuple[str, str, str]] = {}
        for row in conn.execute(
            "SELECT body_hash, url_canonical, archive_path, thumb_path"
            " FROM pages WHERE run_id = ? AND archive_path IS NOT NULL"
            " AND body_hash IS NOT NULL",
            (run_id,),
        ):
            # First archived page per body_hash wins.
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

            # If this is a duplicate-body alias of an already-archived page,
            # don't emit it as a node — record the mapping and add it to the
            # canonical's `aliases` list. Edges to this node will be
            # redirected to the canonical below.
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
                "data": {
                    "id": node_id,
                    "url": row["url_canonical"],
                    "label": row["title"] or row["url_canonical"],
                    "thumb": thumb,
                    "archive": archive,
                    "is_external": bool(row["is_external"]),
                    "is_phantom_404": bool(row["is_phantom_404"]),
                    "http_status": row["http_status"],
                    "depth": row["depth"],
                }
            })

        # Attach alias metadata to canonical nodes.
        for node in nodes:
            aliases = canonical_aliases.get(node["data"]["id"])
            if aliases:
                node["data"]["aliases"] = aliases
                node["data"]["alias_count"] = len(aliases)

        edges: list[dict] = []
        edge_seq = 0
        # De-dupe edges that collapse onto the same (src, dst) after alias
        # rewriting — common when many aliases all point to the same canonical.
        seen_edge_keys: set[tuple[str, str, str]] = set()
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
            src_id = page_key(src_row["url_canonical"])
            # If src is itself an alias, redirect it to its canonical.
            src_id = alias_to_canonical.get(src_id, src_id)
            dst_id = page_key(row["dst_url_canonical"])
            dst_id = alias_to_canonical.get(dst_id, dst_id)
            if src_id == dst_id:
                continue  # self-loop after collapsing

            edge_key = (src_id, dst_id, row["link_kind"])
            if edge_key in seen_edge_keys:
                continue
            seen_edge_keys.add(edge_key)

            if dst_id not in seen_ids:
                # Stub node — referenced but never visited (e.g. over max-pages)
                seen_ids.add(dst_id)
                nodes.append({
                    "data": {
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
                    }
                })

            edge_seq += 1
            edges.append({
                "data": {
                    "id": f"e{edge_seq}",
                    "source": src_id,
                    "target": dst_id,
                    "kind": row["link_kind"],
                    "text": row["link_text"] or "",
                    "shape": row["shape"],
                    "coords_json": row["coords_json"],
                }
            })

        graph = {"nodes": nodes, "edges": edges}
        out = layout["root"] / "graph.json"
        out.write_text(json.dumps(graph, indent=2), encoding="utf-8")
        return out
    finally:
        conn.close()
