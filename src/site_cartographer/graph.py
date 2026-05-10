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
    in Cytoscape.js elements format. Returns the JSON path.
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

        for row in conn.execute(
            "SELECT url_canonical, title, thumb_path, archive_path,"
            " is_external, is_phantom_404, http_status, depth"
            " FROM pages WHERE run_id = ?",
            (run_id,),
        ):
            node_id = page_key(row["url_canonical"])
            seen_ids.add(node_id)
            nodes.append({
                "data": {
                    "id": node_id,
                    "url": row["url_canonical"],
                    "label": row["title"] or row["url_canonical"],
                    "thumb": _to_url_path(row["thumb_path"]),
                    "archive": _to_url_path(row["archive_path"]),
                    "is_external": bool(row["is_external"]),
                    "is_phantom_404": bool(row["is_phantom_404"]),
                    "http_status": row["http_status"],
                    "depth": row["depth"],
                }
            })

        edges: list[dict] = []
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
            src_id = page_key(src_row["url_canonical"])
            dst_id = page_key(row["dst_url_canonical"])

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
