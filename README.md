# site-cartographer

Crawl a website from a single starting URL, archive each page as MHTML for offline rendering, extract every link (including the rarely-handled HTML image-map `<area>` tags), and view the result as an interactive graph with screenshot thumbnails on each node.

Built for ARG-style hub sites that link via image maps and use a catch-all 404 handler — but works on any static site.

## Install

Requires Python 3.13+.

```bash
python -m venv .venv
.venv/Scripts/activate          # PowerShell: .venv\Scripts\Activate.ps1
pip install -e ".[dev]"
playwright install chromium
```

## Usage

```bash
site-cartographer https://www.ourmachineisdown.com/ --max-pages 100 --max-depth 5
```

The crawl writes everything under `output/<timestamp>/`:

```
output/20260510-164558/
├── crawl.sqlite     # pages, edges, pending queue, run metadata
├── graph.json       # Cytoscape elements format
├── pages/           # one .mhtml per crawled page
├── thumbs/          # one .png viewport screenshot per page
└── viewer/          # self-contained HTML viewer (copied at end of crawl)
```

### Common flags

| Flag | Default | Purpose |
|---|---|---|
| `--max-pages N` | 100 | Cap on internal pages crawled |
| `--max-depth N` | 15 | BFS depth cap |
| `--delay-ms N` | 250 | Politeness delay between page fetches |
| `--include-subdomains` | off | Treat any subdomain of base as in-scope (default already strips `www.`) |
| `--respect-robots` | off | Honour `/robots.txt` (opt-in) |
| `--resume DIR` | — | Resume an unfinished run |
| `--headed` | off | Show the browser window (debug) |
| `-v` / `-vv` | off | INFO / DEBUG logging |

## Viewer

```bash
python -m site_cartographer.serve output/20260510-164558
```

Open <http://127.0.0.1:8000/viewer/> in Chromium. Click any node to load its archived page; clickable elements are highlighted in red (`<a>` outlined, `<area>` polygons drawn on a canvas overlay). Use the checkbox to toggle highlights.

A bundled HTTP server is needed because Chromium will not render MHTML loaded over `file://` — the bundled `serve.py` sets the correct `multipart/related` MIME so the iframe accepts it.

## How it handles common pitfalls

- **Image-map links**: `<area href>` inside `<map>` is extracted alongside `<a href>`, with shape and coords preserved for the viewer overlay.
- **Catch-all 404 → homepage**: the body hash of the first successful page is recorded; subsequent pages whose body matches and whose URL differs from the start URL are flagged `is_phantom_404` and skipped (no MHTML, no link extraction).
- **Mixed `www`/bare domains**: `www.foo` and `foo` are treated as the same origin by default. Use `--include-subdomains` to also crawl true subdomains.
- **Out-of-domain links**: stored as edges in the graph and as `is_external` page rows so they show up as nodes, but not crawled.
- **Resumability**: all crawl state lives in SQLite — kill mid-crawl and rerun with `--resume <run-dir>` to continue.

## Tests

```bash
pytest -q
```

20 unit tests covering URL canonicalisation, link extraction (incl. image maps), body-hash dedupe, and same-origin scope. No live network access required.

## Layout

```
src/site_cartographer/
├── cli.py        # argparse entrypoint
├── crawler.py    # BFS orchestrator, SQLite schema, dedupe
├── browser.py    # Playwright wrapper, MHTML + thumbnail capture
├── links.py      # canonicalise, same-origin, extract <a>/<area>
├── graph.py      # SQLite -> Cytoscape JSON
├── archive.py    # output dir layout + filename helpers
├── serve.py      # MHTML-aware static server for the viewer
└── viewer/       # index.html + viewer.js + cytoscape.min.js
```
