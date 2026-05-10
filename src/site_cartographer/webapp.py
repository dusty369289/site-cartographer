"""aiohttp web application: home, scans list, new/resume scan, live progress.

Replaces the TUI/CLI prompts with browser-driven flows. Scans run inside the
running web server's event loop; progress is broadcast via Server-Sent Events.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import webbrowser
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from aiohttp import web

from .archive import (
    ensure_run_dirs,
    format_size,
    list_runs,
    parse_size,
)
from .crawler import EXTERNAL_POLICIES, MAX_WORKERS, CrawlConfig, ProgressReporter, crawl
from .graph import export_graph_json

logger = logging.getLogger(__name__)
WEB_DIR = Path(__file__).parent / "web"
VIEWER_DIR = Path(__file__).parent / "viewer"

_NAME_RE = re.compile(r"[^a-zA-Z0-9._-]")


def _slugify(name: str) -> str:
    return _NAME_RE.sub("-", name.strip()).strip("-") or "scan"


# ---------------------------------------------------------------------- registry
@dataclass
class ScanContext:
    run_dir: Path
    config: CrawlConfig
    task: asyncio.Task | None = None
    queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    snapshot: dict[str, Any] = field(default_factory=dict)
    history: list[dict] = field(default_factory=list)
    finished: bool = False
    error: str | None = None

    @property
    def run_name(self) -> str:
        return self.run_dir.name


class ScanRegistry:
    def __init__(self) -> None:
        self._scans: dict[str, ScanContext] = {}

    def get(self, name: str) -> ScanContext | None:
        return self._scans.get(name)

    def register(self, ctx: ScanContext) -> None:
        self._scans[ctx.run_name] = ctx

    def drop(self, name: str) -> None:
        self._scans.pop(name, None)


class WebProgressReporter(ProgressReporter):
    """Pushes ProgressReporter events into the ScanContext queue."""

    def __init__(self, ctx: ScanContext) -> None:
        self.ctx = ctx

    def _emit(self, event_type: str, **kwargs: Any) -> None:
        event = {"type": event_type, **kwargs}
        # Snapshot of latest "start" + "halt"/"finish" so late-joining
        # SSE clients can rebuild current state.
        if event_type in ("start", "halt", "finish"):
            self.ctx.snapshot[event_type] = event
        if event_type == "page":
            self.ctx.history.append(event)
            self.ctx.history[:] = self.ctx.history[-25:]
        try:
            self.ctx.queue.put_nowait(event)
        except asyncio.QueueFull:
            pass  # drop event under back-pressure

    def on_start(self, **kw: Any) -> None: self._emit("start", **kw)
    def on_page(self, **kw: Any) -> None: self._emit("page", **kw)
    def on_halt(self, reason: str) -> None: self._emit("halt", reason=reason)
    def on_finish(self, **kw: Any) -> None: self._emit("finish", **kw)


# ---------------------------------------------------------------------- routes
def _serve_template(name: str):
    path = WEB_DIR / name
    async def handler(request: web.Request) -> web.Response:
        return web.FileResponse(path)
    return handler


async def api_list_scans(request: web.Request) -> web.Response:
    runs = list_runs(request.app["output_root"])
    out = []
    for r in runs:
        ctx = request.app["registry"].get(r.dir.name)
        running = ctx is not None and not ctx.finished
        out.append({
            "name": r.dir.name,
            "display_name": r.display_name,
            "url": r.start_url,
            "started_at": r.started_at,
            "finished_at": r.finished_at,
            "halt_reason": r.halt_reason,
            "page_count": r.page_count,
            "archived_count": r.archived_count,
            "edge_count": r.edge_count,
            "queue_depth": r.queue_depth,
            "total_bytes": r.total_bytes,
            "total_bytes_human": format_size(r.total_bytes),
            "is_resumable": r.is_resumable,
            "running": running,
        })
    return web.json_response(out)


def _build_config_from_payload(
    payload: dict,
    output_dir: Path,
    *,
    resume: bool = False,
) -> CrawlConfig:
    name = payload.get("name") or None
    max_size_raw = payload.get("max_file_size") or ""
    max_size = parse_size(max_size_raw) if str(max_size_raw).strip() else None
    extractors = payload.get("link_extractors")
    if isinstance(extractors, str):
        extractors = [e.strip() for e in extractors.split(",") if e.strip()]
    if not extractors:
        from .links import DEFAULT_EXTRACTORS
        extractors = list(DEFAULT_EXTRACTORS)
    return CrawlConfig(
        start_url=payload["start_url"],
        output_dir=output_dir,
        name=name,
        max_pages=int(payload.get("max_pages", 100)),
        max_depth=int(payload.get("max_depth", 15)),
        max_file_size=max_size,
        delay_ms=int(payload.get("delay_ms", 250)),
        page_timeout_ms=int(payload.get("page_timeout_ms", 30000)),
        parallel_workers=int(payload.get("parallel_workers", 1)),
        include_subdomains=bool(payload.get("include_subdomains", False)),
        respect_robots=bool(payload.get("respect_robots", False)),
        external_policy=payload.get("external_policy", "metadata"),
        link_extractors=tuple(extractors),
        custom_link_regex=payload.get("custom_link_regex") or None,
        viewport=tuple(payload.get("viewport", [320, 240])),
        headless=True,
        resume=resume,
    )


def _post_run_artifacts(run_dir: Path) -> None:
    """Install viewer assets + export graph.json after a crawl completes
    or is paused."""
    dst = run_dir / "viewer"
    dst.mkdir(parents=True, exist_ok=True)
    for entry in VIEWER_DIR.iterdir():
        if entry.is_file():
            shutil.copy2(entry, dst / entry.name)
    try:
        export_graph_json(run_dir)
    except Exception as e:
        logger.warning("graph export failed for %s: %s", run_dir, e)


async def _run_scan(ctx: ScanContext, reporter: WebProgressReporter) -> None:
    try:
        await crawl(ctx.config, progress=reporter)
    except (asyncio.CancelledError, KeyboardInterrupt):
        # The crawler already records halt_reason="interrupted by user" in
        # the DB; we only need to surface the event here.
        reporter._emit("halt", reason="interrupted by user")
    except Exception as e:
        ctx.error = str(e)
        logger.exception("scan crashed")
        reporter._emit("error", error=str(e))
    finally:
        _post_run_artifacts(ctx.run_dir)
        ctx.finished = True
        reporter._emit("done", run_name=ctx.run_name)


async def api_new_scan(request: web.Request) -> web.Response:
    payload = await request.json()
    output_root: Path = request.app["output_root"]
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    name = payload.get("name") or ""
    if name:
        slug = _slugify(name)
        output_dir = output_root / f"{slug}-{timestamp}"
    else:
        output_dir = output_root / timestamp

    config = _build_config_from_payload(payload, output_dir)
    ensure_run_dirs(output_dir)

    ctx = ScanContext(run_dir=output_dir, config=config)
    request.app["registry"].register(ctx)
    reporter = WebProgressReporter(ctx)
    ctx.task = asyncio.create_task(_run_scan(ctx, reporter))
    return web.json_response({
        "run_name": ctx.run_name,
        "redirect": f"/scan-progress?name={ctx.run_name}",
    })


async def api_resume_scan(request: web.Request) -> web.Response:
    name = request.match_info["name"]
    output_root: Path = request.app["output_root"]
    run_dir = output_root / name
    if not run_dir.is_dir() or not (run_dir / "crawl.sqlite").is_file():
        return web.json_response({"error": "not found"}, status=404)

    payload = await request.json()
    config = _build_config_from_payload(payload, run_dir, resume=True)
    ctx = ScanContext(run_dir=run_dir, config=config)
    request.app["registry"].register(ctx)
    reporter = WebProgressReporter(ctx)
    ctx.task = asyncio.create_task(_run_scan(ctx, reporter))
    return web.json_response({
        "run_name": ctx.run_name,
        "redirect": f"/scan-progress?name={ctx.run_name}",
    })


async def api_stop_scan(request: web.Request) -> web.Response:
    name = request.match_info["name"]
    ctx = request.app["registry"].get(name)
    if ctx is None or ctx.task is None or ctx.task.done():
        return web.json_response({"error": "no running scan"}, status=400)
    ctx.task.cancel()
    return web.json_response({"ok": True})


async def api_delete_scan(request: web.Request) -> web.Response:
    name = request.match_info["name"]
    output_root: Path = request.app["output_root"]
    run_dir = output_root / name
    if not run_dir.is_dir():
        return web.json_response({"error": "not found"}, status=404)
    ctx = request.app["registry"].get(name)
    if ctx is not None and not ctx.finished:
        return web.json_response(
            {"error": "scan is still running — stop it first"}, status=400,
        )
    shutil.rmtree(run_dir)
    request.app["registry"].drop(name)
    return web.json_response({"ok": True})


async def api_scan_config(request: web.Request) -> web.Response:
    """Return the stored config_json + diagnostics for a run, used by the
    resume form to pre-populate fields."""
    import sqlite3
    name = request.match_info["name"]
    output_root: Path = request.app["output_root"]
    run_dir = output_root / name
    db = run_dir / "crawl.sqlite"
    if not db.is_file():
        return web.json_response({"error": "not found"}, status=404)
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(runs)").fetchall()}
        select = ["config_json"]
        if "halt_reason" in cols:
            select.append("halt_reason")
        if "dropped_for_depth" in cols:
            select.append("dropped_for_depth")
        row = conn.execute(
            f"SELECT {', '.join(select)} FROM runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return web.json_response({"error": "no run row"}, status=404)
    cfg = json.loads(row["config_json"])
    return web.json_response({
        "config": cfg,
        "halt_reason": row["halt_reason"] if "halt_reason" in row.keys() else None,
        "dropped_for_depth": row["dropped_for_depth"] if "dropped_for_depth" in row.keys() else 0,
    })


async def api_events(request: web.Request) -> web.StreamResponse:
    name = request.match_info["name"]
    ctx = request.app["registry"].get(name)
    if ctx is None:
        return web.Response(status=404, text="no running scan")

    response = web.StreamResponse()
    response.headers["Content-Type"] = "text/event-stream"
    response.headers["Cache-Control"] = "no-cache"
    response.headers["X-Accel-Buffering"] = "no"
    await response.prepare(request)

    async def send(event: dict) -> None:
        data = "data: " + json.dumps(event) + "\n\n"
        await response.write(data.encode("utf-8"))

    # Replay snapshot + history so a late-joining client gets caught up.
    if "start" in ctx.snapshot:
        await send(ctx.snapshot["start"])
    for h in ctx.history:
        await send(h)
    if ctx.finished:
        if "halt" in ctx.snapshot:
            await send(ctx.snapshot["halt"])
        if "finish" in ctx.snapshot:
            await send(ctx.snapshot["finish"])
        await send({"type": "done", "run_name": ctx.run_name})
        return response

    while True:
        try:
            event = await asyncio.wait_for(ctx.queue.get(), timeout=15.0)
        except asyncio.TimeoutError:
            try:
                await response.write(b": ping\n\n")
            except (ConnectionResetError, ConnectionAbortedError):
                break
            continue
        try:
            await send(event)
        except (ConnectionResetError, ConnectionAbortedError):
            break
        if event.get("type") == "done":
            break
    return response


async def api_save_layout(request: web.Request) -> web.Response:
    """Persist a viewer-computed layout (positions + params) to layout.json
    inside the run dir, so the next viewer load uses it as a starting point."""
    name = request.match_info["name"]
    output_root: Path = request.app["output_root"]
    base = (output_root / name).resolve()
    if not base.is_dir() or not (base / "crawl.sqlite").is_file():
        return web.json_response({"error": "not found"}, status=404)
    payload = await request.json()
    if not isinstance(payload, dict) or "positions" not in payload:
        return web.json_response({"error": "missing positions"}, status=400)
    payload["saved_at"] = datetime.utcnow().isoformat() + "Z"
    payload.setdefault("version", 1)
    out = base / "layout.json"
    out.write_text(json.dumps(payload), encoding="utf-8")
    return web.json_response({"ok": True, "saved_at": payload["saved_at"]})


async def api_clear_layout(request: web.Request) -> web.Response:
    name = request.match_info["name"]
    output_root: Path = request.app["output_root"]
    target = (output_root / name / "layout.json").resolve()
    base = (output_root / name).resolve()
    try:
        target.relative_to(base)
    except ValueError:
        raise web.HTTPForbidden()
    if target.is_file():
        target.unlink()
    return web.json_response({"ok": True})


async def serve_run_file(request: web.Request) -> web.StreamResponse:
    """Serve a file from a run dir at /scans/{name}/{path}.

    A bare /scans/{name}[/] redirects to /scans/{name}/viewer/ so the
    viewer's relative `../graph.json` resolves against the right URL prefix.
    """
    name = request.match_info["name"]
    sub = request.match_info.get("path", "")
    output_root: Path = request.app["output_root"]
    base = (output_root / name).resolve()
    target = (base / sub).resolve() if sub else base
    try:
        target.relative_to(base)
    except ValueError:
        raise web.HTTPForbidden()

    if target.is_dir():
        if (target / "index.html").is_file():
            # e.g. /scans/X/viewer/  → serve viewer/index.html.
            return web.FileResponse(target / "index.html")
        if (base / "viewer" / "index.html").is_file():
            # bare run dir → bounce to viewer with a trailing slash.
            raise web.HTTPFound(f"/scans/{name}/viewer/")
        raise web.HTTPNotFound()
    if not target.is_file():
        raise web.HTTPNotFound()
    return web.FileResponse(target)


# ---------------------------------------------------------------- app factory
def make_app(output_root: Path) -> web.Application:
    output_root.mkdir(parents=True, exist_ok=True)
    app = web.Application()
    app["output_root"] = output_root
    app["registry"] = ScanRegistry()

    app.router.add_get("/", _serve_template("home.html"))
    app.router.add_get("/scans", _serve_template("scans.html"))
    app.router.add_get("/scan-new", _serve_template("scan-new.html"))
    app.router.add_get("/scan-resume", _serve_template("scan-resume.html"))
    app.router.add_get("/scan-progress", _serve_template("scan-progress.html"))

    app.router.add_get("/api/scans", api_list_scans)
    app.router.add_post("/api/scans", api_new_scan)
    app.router.add_post("/api/scans/{name}/resume", api_resume_scan)
    app.router.add_post("/api/scans/{name}/stop", api_stop_scan)
    app.router.add_delete("/api/scans/{name}", api_delete_scan)
    app.router.add_get("/api/scans/{name}/config", api_scan_config)
    app.router.add_get("/api/scans/{name}/events", api_events)
    app.router.add_post("/api/scans/{name}/layout", api_save_layout)
    app.router.add_delete("/api/scans/{name}/layout", api_clear_layout)

    app.router.add_static("/static", WEB_DIR)
    app.router.add_get("/scans/{name}", serve_run_file)
    app.router.add_get("/scans/{name}/", serve_run_file)
    app.router.add_get("/scans/{name}/{path:.+}", serve_run_file)
    return app


def serve_app(output_root: Path, port: int = 8000, *, open_browser: bool = True) -> None:
    app = make_app(output_root)
    url = f"http://127.0.0.1:{port}/"
    print(f"site-cartographer web @ {url}  (Ctrl+C to stop)")
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    web.run_app(app, host="127.0.0.1", port=port, print=lambda *_: None)
