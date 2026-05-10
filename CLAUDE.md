# CLAUDE.md — site-cartographer

## Identity

User: Ethan Wright (`dusty`). UoB CS student, Python/JS/Deno developer on Windows 11 (Git Bash).

## Tone

Terse and direct. No summaries, no pleasantries. Surface useful proactive observations.

## Project

**site-cartographer** — a Python tool that crawls a website starting from a single URL, captures screenshots of every page, identifies all clickable links (with a second annotated screenshot highlighting them in red), and builds a navigable graph of the entire site.

The original use case is mapping ARG-style hub pages where many in-domain links lead to further pages forming a tree/graph. The output should let the user visualise and traverse the discovered structure offline.

## Status

**Greenfield.** No code yet. Detailed planning is the next step — discuss architecture and scope with the user before writing implementation.

## Likely Tech Stack

To be confirmed during planning, but expected:

- **Python 3.13** (matches Ethan's system default)
- **Playwright** for browser automation + screenshots (handles JS-rendered pages, supports full-page screenshots, can highlight DOM elements via injected CSS/JS)
- **BeautifulSoup or Playwright's own DOM API** for link extraction
- **JSON or SQLite** for the graph store (nodes = pages, edges = links, attributes = screenshot paths, link text, etc.)
- **Optional viewer**: a small static HTML/JS frontend (vis.js / cytoscape.js / d3) to render the graph with screenshot thumbnails

## Open Design Questions

These need resolving with the user before implementation:

1. **Scope of crawl**
   - Same-domain only? Subdomain handling? Path prefix restrictions?
   - Max depth, max pages, rate limits?
   - Treat fragment-only links (`#section`) as the same node?
   - URL canonicalisation (trailing slashes, query param ordering, normalising case)?

2. **Screenshot strategy**
   - Full-page (scrolling) vs. viewport-only?
   - Two screenshots per page (clean + annotated) or just annotated?
   - How are clickable elements highlighted — red CSS outline injected before capture? Numbered overlays?
   - File format (PNG for fidelity vs. JPEG/WebP for size)?
   - Naming convention for screenshot files (URL hash? sequential ID?)?

3. **Link discovery**
   - `<a href>` only, or also buttons / `onclick` handlers / JS-driven navigation?
   - Form submissions?
   - Handling of pages that require interaction to reveal links (modals, hover menus)?

4. **Graph storage**
   - On-disk format — flat JSON, JSONL per crawl session, SQLite?
   - Resumable crawls? (track visited / pending queue)
   - Versioning — re-crawls overwrite or stack as snapshots?

5. **Concurrency**
   - Single browser context, sequential? Or multiple workers in parallel?
   - Politeness delays?

6. **Visualisation**
   - In-scope for v1 or phase 2?
   - Interactive graph viewer, or just export GraphViz / Mermaid?
   - Should it embed screenshot thumbnails on graph nodes?

7. **Error handling**
   - 4xx/5xx responses — record as failed nodes vs. skip?
   - Pages that timeout / hang?
   - robots.txt — respect or ignore?

## Directory Structure (intended)

```
site-cartographer/
├── .claude/              # Claude config (gitignored)
├── src/
│   └── site_cartographer/
│       ├── __init__.py
│       ├── crawler.py    # main crawl orchestrator
│       ├── browser.py    # Playwright wrapper, screenshot capture
│       ├── graph.py      # graph datastructure + persistence
│       ├── links.py      # link extraction + canonicalisation
│       └── cli.py        # entrypoint
├── tests/
├── output/               # crawl artifacts (screenshots, graph JSON) — gitignored
├── viewer/               # optional static HTML graph viewer
├── pyproject.toml
├── README.md
└── CLAUDE.md
```

To be confirmed once the user finalises scope.

## Workflow

- **Branching:** Feature branches only. Merge to `main` only after explicit user confirmation.
- **Commits:** Short, focused on *why*. No co-author lines.
- **Testing:** Run tests before and after changes once a suite exists.
- **Dev servers:** Kill after testing to avoid zombie node.exe processes (Playwright spawns Chromium).
- **Output dir:** Crawl artifacts must stay out of git — they will be large.

## Constraints

- **Always ask** before installing dependencies, scaffolding code, or running crawls against external sites.
- **Respect target sites.** Default to conservative concurrency and reasonable delays unless the user opts otherwise.
- **No secrets in repo.** Any auth/cookies/session tokens for protected sites stay in env vars or a gitignored config.
- **Privacy:** Never reference `.claude/` in root `.gitignore`. The `.claude/.gitignore` already handles that.
