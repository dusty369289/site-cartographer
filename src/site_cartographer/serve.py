"""Tiny static server that maps `.mhtml` to `multipart/related`.

Chromium will not render MHTML loaded from `file://` and `python -m http.server`
serves them as `application/octet-stream`. This subclass fixes the MIME so the
viewer can iframe-load saved pages.
"""
from __future__ import annotations

import argparse
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class CartographerHandler(SimpleHTTPRequestHandler):
    extensions_map = {
        **SimpleHTTPRequestHandler.extensions_map,
        ".mhtml": "multipart/related",
        ".json": "application/json",
        ".js": "application/javascript",
        ".css": "text/css",
    }

    def end_headers(self) -> None:
        # MHTML iframes need cross-origin permissive headers when served locally
        self.send_header("Access-Control-Allow-Origin", "*")
        super().end_headers()


def serve(directory: Path, port: int = 8000) -> None:
    handler_cls = partial(CartographerHandler, directory=str(directory))
    httpd = ThreadingHTTPServer(("127.0.0.1", port), handler_cls)
    print(f"Serving {directory} at http://127.0.0.1:{port}/viewer/")
    print("Ctrl+C to stop.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down")
    finally:
        httpd.server_close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve a site-cartographer run dir")
    parser.add_argument("run_dir", type=Path, help="path to a crawl output dir")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    if not args.run_dir.is_dir():
        raise SystemExit(f"not a directory: {args.run_dir}")
    serve(args.run_dir, args.port)


if __name__ == "__main__":
    main()
