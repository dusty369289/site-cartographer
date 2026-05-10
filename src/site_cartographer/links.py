from __future__ import annotations

import hashlib
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup
from w3lib.url import canonicalize_url

_SKIP_SCHEMES = ("mailto:", "javascript:", "tel:", "data:", "ftp:")


@dataclass(frozen=True)
class ExtractedLink:
    url: str
    kind: str  # "a" or "area"
    shape: str | None  # "rect" | "poly" | "circle" | "default" | None
    coords: list[int] | None
    image_src: str | None  # for <area>, the canonicalised URL of the owning <img>
    text: str


def canonicalize(url: str) -> str:
    """Return a canonical form of *url* suitable for graph-key dedupe.

    Drops fragment, sorts query params, lowercases scheme + host,
    rewrites `/index.html` to `/`, ensures empty path becomes `/`.
    """
    normalised = canonicalize_url(url, keep_fragments=False)
    parsed = urlsplit(normalised)
    path = parsed.path or "/"
    if path.endswith("/index.html"):
        path = path[: -len("index.html")]
    return urlunsplit(
        (parsed.scheme.lower(), parsed.netloc.lower(), path, parsed.query, "")
    )


def _strip_www(host: str) -> str:
    return host[4:] if host.startswith("www.") else host


def is_same_origin(url: str, base: str, *, include_subdomains: bool) -> bool:
    """Whether *url* is the same origin as *base* for crawl-scope purposes.

    Scheme and ports are ignored (sites mix http/https). A leading `www.` is
    stripped from both sides — `www.example.com` and `example.com` are treated
    as the same site by default. With *include_subdomains*, any descendant of
    the base host also matches.
    """
    target = _strip_www(urlsplit(url).netloc.lower())
    base_host = _strip_www(urlsplit(base).netloc.lower())
    if target == base_host:
        return True
    if include_subdomains and base_host:
        return target.endswith("." + base_host)
    return False


def body_hash(content: str) -> str:
    """sha256 hex digest of UTF-8-encoded body. Used for catch-all-404 dedupe."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def extract_links(html: str, base_url: str) -> list[ExtractedLink]:
    """Pull every navigable link out of *html*, including image-map `<area>` tags.

    Returns canonicalised absolute URLs. Drops self-references (links that
    canonicalise to *base_url*), non-http schemes, and fragment-only links.
    """
    soup = BeautifulSoup(html, "html.parser")
    base_canonical = canonicalize(base_url)

    map_to_img: dict[str, str] = {}
    for img in soup.find_all("img", attrs={"usemap": True}):
        usemap = img["usemap"]
        if usemap.startswith("#"):
            usemap = usemap[1:]
        src = img.get("src")
        if src:
            map_to_img[usemap] = urljoin(base_url, src)

    links: list[ExtractedLink] = []

    for a in soup.find_all("a", href=True):
        link = _build_link(a, base_url, base_canonical, kind="a")
        if link is not None:
            links.append(link)

    for area in soup.find_all("area", href=True):
        link = _build_link(area, base_url, base_canonical, kind="area",
                           map_to_img=map_to_img)
        if link is not None:
            links.append(link)

    return links


def _build_link(
    el,
    base_url: str,
    base_canonical: str,
    *,
    kind: str,
    map_to_img: dict[str, str] | None = None,
) -> ExtractedLink | None:
    href = (el.get("href") or "").strip()
    if not href or href.startswith("#") or href.lower().startswith(_SKIP_SCHEMES):
        return None
    try:
        absolute = urljoin(base_url, href)
        canonical = canonicalize(absolute)
    except (ValueError, KeyError):
        return None
    if not canonical.startswith(("http://", "https://")):
        return None
    if canonical == base_canonical:
        return None

    if kind == "a":
        text = el.get_text(strip=True) or el.get("title", "") or ""
        return ExtractedLink(
            url=canonical, kind="a", shape=None, coords=None,
            image_src=None, text=text,
        )

    shape = (el.get("shape") or "rect").lower()
    coords_raw = el.get("coords", "")
    coords: list[int] | None = None
    if coords_raw:
        try:
            coords = [int(c.strip()) for c in coords_raw.split(",") if c.strip()]
        except ValueError:
            coords = None

    image_src: str | None = None
    if map_to_img is not None:
        parent_map = el.find_parent("map")
        if parent_map is not None:
            map_name = parent_map.get("name") or parent_map.get("id")
            if map_name and map_name in map_to_img:
                image_src = map_to_img[map_name]

    text = el.get("alt", "") or el.get("title", "") or ""
    return ExtractedLink(
        url=canonical, kind="area", shape=shape, coords=coords,
        image_src=image_src, text=text,
    )
