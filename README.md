# site-cartographer

Crawl a website from a single starting URL, archive every page as a self-contained HTML file (assets inlined as data URIs), extract every link — including the rarely-handled HTML image-map `<area>` tags — and explore the result as an interactive force-directed graph with thumbnail nodes, community-detection clustering, and a per-node metadata pane.

Built for ARG-style hub sites that link via image maps and use a catch-all 404 handler, but works on any static site.

![status: WIP](https://img.shields.io/badge/status-WIP-yellow) ![python: 3.13+](https://img.shields.io/badge/python-3.13%2B-blue) ![license: MIT](https://img.shields.io/badge/license-MIT-green)

## Install

```bash
python -m venv .venv
# Windows: .venv\Scripts\Activate.ps1   |   POSIX: source .venv/bin/activate
pip install -e ".[dev]"
playwright install chromium
```

Requires Python 3.13+.

## Quick start — web UI

```bash
site-cartographer web
```

Opens a browser at `http://127.0.0.1:8000/` with:

| Page | Purpose |
|---|---|
| `/` | home menu + recent scans |
| `/scan-new` | start a new crawl (form for every option) |
| `/scan-progress` | live SSE-driven progress dashboard while a scan runs |
| `/scan-resume` | resume an unfinished scan; suggested cap-bumps adapt to halt reason |
| `/scans` | manage saved scans (view / resume / delete) |
| `/scans/{name}/` | the interactive graph viewer for a saved scan |

Scans run inside the web server's event loop, so closing the server stops the scan — but the run dir stays resumable.

## CLI alternatives

The web UI is the recommended path; the CLI subcommands stay for scripting:

```bash
site-cartographer                        # interactive TUI menu
site-cartographer scan <url> [flags]     # one-shot crawl
site-cartographer list                   # tabular list of scans
site-cartographer view <name>            # serve the viewer for a saved scan
```

### Scan flags

| Flag | Default | Purpose |
|---|---|---|
| `--name NAME` | — | Memorable label used in the run dir |
| `--max-pages N` | 100 | Cap on internal pages crawled |
| `--max-depth N` | 15 | BFS depth cap |
| `--max-file-size SIZE` | unlimited | Halt when archive exceeds this (`500MB`, `2GB`, …) |
| `--workers N` | 1 | Parallel workers (1–8) sharing one Chromium context |
| `--delay-ms N` | 250 | Per-worker politeness delay |
| `--external-policy` | `metadata` | `ignore` / `metadata` / `archive` / `crawl` for out-of-scope links |
| `--include-subdomains` | off | Crawl subdomains too (`www.` is always same-site) |
| `--respect-robots` | off | Honour `/robots.txt` |
| `--resume DIR` | — | Resume an unfinished run |
| `--headed` | off | Show the browser window (debug) |

## How it handles common pitfalls

- **Image-map links**: `<area href>` inside `<map>` is extracted alongside `<a href>`, with shape and coords preserved.
- **Catch-all 404 → homepage**: the body hash of the first successful page is recorded; subsequent pages whose body matches *and* whose URL differs from the start URL are flagged `is_phantom_404` and skipped.
- **Duplicate-body URLs** (e.g. `index.htm` vs `index.html` serving the same content): folded into their canonical at graph-export time so they don't clutter the layout, but the alias list is preserved on the canonical's metadata.
- **Mixed `www`/bare domains**: `www.foo` and `foo` are treated as the same origin by default; use `--include-subdomains` to also crawl true subdomains.
- **Out-of-domain links**: configurable per scan — see `--external-policy`.
- **Resumability**: all state in SQLite. Ctrl+C any time. `pending` rows survive interruption; on resume, in-flight markers are cleared and any halted run continues with bumped caps.
- **Parallelism**: workers claim pending rows atomically (synchronous SELECT+UPDATE between awaits is naturally race-free in single-threaded asyncio); pages_done caps overshoot by at most `workers - 1`.

## Output layout

Each scan lives under `output/<name>-<timestamp>/`:

```
output/omid-20260510-181122/
├── crawl.sqlite        # pages, edges, pending queue, run metadata (WAL)
├── graph.json          # nodes + edges flattened for the viewer
├── pages/<hash>.html   # one self-contained HTML per crawled page (data-URI assets)
├── thumbs/<hash>.png   # 320×240 viewport thumbnail per page
└── viewer/             # bundled Sigma.js viewer (copied at end of crawl)
```

`output/` is gitignored — scan content is third-party and may be large. **Never commit it.**

## Viewer features

- **Sigma.js + WebGL** rendering, scales smoothly to 10k+ nodes
- **d3-force** layout with live-tunable parameters (link distance, repulsion, collision, centering, convergence)
- **Cluster pull** (custom force pulling nodes toward their detected community centroid)
- **Hub softening** (weakens edges from high-degree hubs so leaves can re-cluster)
- **Label-propagation** community detection runs in-browser, results colour-coded
- **Thumbnail overlay** on each node, with type-coloured ring
- **Click any node** to load its archived page in an iframe with red-highlighted clickable elements (`<a>` outline + `<area>` polygons on a canvas overlay)
- **Metadata tab** per node: identity (URL, status, depth, archive size, fetched-at, body hash, community), aliases, foldable links-in / links-out lists with click-to-navigate
- **Draggable side-panel divider** (double-click to reset; width persists)

## Tests

```bash
pytest -q
```

74 tests covering URL canonicalisation, link extraction (8 built-in extractor types + custom regex), body-hash dedupe, scope-mode policy (host / descendants / domain / regex), size-string parsing, resume-suggestion logic, and `CrawlConfig` validation. No live network required.

## Project layout

```
src/site_cartographer/
├── cli.py            # argparse entrypoint with scan/view/list/web subcommands
├── webapp.py         # aiohttp web app (home, scans, new/resume/progress, SSE)
├── tui.py            # Rich + questionary TUI (alternative to web)
├── progress.py       # live crawl progress panel for terminal mode
├── crawler.py        # BFS orchestrator, SQLite schema, dedupe, externals pass
├── browser.py        # Playwright wrapper; inline-HTML + thumbnail capture
├── links.py          # URL canonicalisation, same-origin, <a>/<area> extraction
├── graph.py          # SQLite → flat graph.json with alias collapse
├── archive.py        # output dir layout, run listing, size helpers
├── serve.py          # legacy static server (kept for `site-cartographer view`)
├── viewer/           # Sigma.js + d3-force + Graphology, vendored offline
└── web/              # aiohttp templates + shared CSS/JS
```

## License

MIT — see [LICENSE](LICENSE).
