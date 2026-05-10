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

Run with no args for an interactive menu:

```bash
site-cartographer
```

Or use one of the subcommands directly:

```bash
site-cartographer scan https://www.ourmachineisdown.com/ --name omid --max-pages 100
site-cartographer list
site-cartographer view omid              # by name
site-cartographer view output/omid-20260510-171535   # by path
```

The crawl writes everything under `output/<name>-<timestamp>/`:

```
output/omid-20260510-171535/
├── crawl.sqlite     # pages, edges, pending queue, run metadata
├── graph.json       # Cytoscape elements format
├── pages/           # one self-contained .html per crawled page (assets inlined as data URIs)
├── thumbs/          # one .png viewport screenshot per page
└── viewer/          # self-contained HTML viewer (copied at end of crawl)
```

### Scan flags

| Flag | Default | Purpose |
|---|---|---|
| `--name NAME` | — | Memorable label, used in the run dir name and `list` output |
| `--max-pages N` | 100 | Cap on internal pages crawled |
| `--max-depth N` | 15 | BFS depth cap |
| `--max-file-size SIZE` | unlimited | Halt when archive grows past this (e.g. `500MB`, `2GB`) |
| `--workers N` | 1 | Parallel discovery workers (1–8). Each runs as its own asyncio task with an independent Playwright page; they share one browser context and one SQLite DB |
| `--delay-ms N` | 250 | Per-worker politeness delay between page fetches |
| `--include-subdomains` | off | Treat any subdomain of base as in-scope (default already strips `www.`) |
| `--respect-robots` | off | Honour `/robots.txt` (opt-in) |
| `--resume DIR` | — | Resume an unfinished run |
| `--headed` | off | Show the browser window (debug) |
| `-v` / `-vv` | off | INFO / DEBUG logging |

While the crawl runs, a live Rich panel shows: progress bar, archived/discovered/queue counts, current URL, archive size vs cap, and a tail of recently fetched pages with status glyphs (`+` archived, `=` duplicate body, `?` phantom 404, `!` error).

## Viewer

`site-cartographer view <run>` (or pick from the interactive menu) starts a small HTTP server and opens <http://127.0.0.1:8000/viewer/> in your browser. Click any node to load its archived page in the side panel; clickable elements are highlighted in red (`<a>` outlined, `<area>` polygons drawn on a canvas overlay). Use the checkbox to toggle highlights.

The archived pages are self-contained HTML with all images, audio, and stylesheets inlined as data URIs — they render in any iframe from any origin, no MHTML weirdness.

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

42 unit tests covering URL canonicalisation, link extraction (incl. image maps), body-hash dedupe, same-origin scope, and human-readable size parsing. No live network access required.

## Layout

```
src/site_cartographer/
├── cli.py        # argparse entrypoint with scan/view/list subcommands
├── tui.py        # interactive menu + scan/view flows (Rich + questionary)
├── progress.py   # live crawl progress panel
├── crawler.py    # BFS orchestrator, SQLite schema, dedupe, size-cap halt
├── browser.py    # Playwright wrapper, inline-HTML + thumbnail capture
├── links.py      # canonicalise, same-origin, extract <a>/<area>
├── graph.py      # SQLite -> Cytoscape JSON
├── archive.py    # output dir layout, run listing, size helpers
├── serve.py      # static server for the viewer
└── viewer/       # index.html + viewer.js + cytoscape.min.js
```
