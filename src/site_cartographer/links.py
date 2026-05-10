from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup
from w3lib.url import canonicalize_url

_SKIP_SCHEMES = ("mailto:", "javascript:", "tel:", "data:", "ftp:")

# Built-in extractor identifiers. The crawler accepts any subset of these
# (plus a user-supplied regex) for each scan.
KNOWN_EXTRACTORS = (
    "a",          # <a href>      anchor links (default)
    "area",       # <area href>   image-map links (default)
    "iframe",     # <iframe src> + <frame src>
    "form",       # <form action> targets
    "link",       # <link href>   (rel=alternate, canonical, prev/next, etc.)
    "onclick",    # URLs inside onclick="…" JS handlers
    "data_attrs", # data-href / data-url / data-link / data-target
    "text_url",   # bare http(s)://… URLs in element text
)
DEFAULT_EXTRACTORS = ("a", "area")
ALL_EXTRACTORS = KNOWN_EXTRACTORS

# Reasonable defaults if the user picks "common options" without choosing.
COMMON_EXTRACTORS = ("a", "area", "iframe", "form", "data_attrs")

# URLs hidden inside JS-flavoured attribute values.
_JS_URL_RE = re.compile(
    r"""(?:window\.location|location\.href|location)\s*=\s*['"]([^'"]+)['"]"""
    r"""|['"]((?:https?:)?//[^'"\s<>]+)['"]"""
)
# Bare URLs in text content.
_TEXT_URL_RE = re.compile(r"https?://[^\s<>'\"]+")


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


SCOPE_MODES = ("host", "descendants", "domain", "regex")


def is_in_scope(
    url: str, base: str, *, scope_mode: str = "host", scope_value: str = "",
) -> bool:
    """Whether *url* belongs to the crawl scope rooted at *base*.

    Scheme and port are ignored (sites mix http/https). Leading `www.` is
    stripped from hosts so `www.foo.com` and `foo.com` are always equivalent.

    Modes:
      * ``host``         — only the exact base host (default).
      * ``descendants``  — base host + any descendant subdomain
        (e.g. ``foo.base.com`` matches base ``base.com``).
      * ``domain``       — *scope_value* is a domain; any host equal to it or
        ending in ``.scope_value`` matches. Useful for sibling subdomains
        (e.g. with base ``public.3net.dev`` and scope ``3net.dev``,
        ``ytrss.3net.dev`` is in scope).
      * ``regex``        — *scope_value* is a regex; matched against the
        target host (post-www-strip).
    """
    target = _strip_www(urlsplit(url).netloc.lower())
    base_host = _strip_www(urlsplit(base).netloc.lower())

    if scope_mode == "host":
        return target == base_host
    if scope_mode == "descendants":
        if not base_host:
            return False
        return target == base_host or target.endswith("." + base_host)
    if scope_mode == "domain":
        d = (scope_value or "").lower().lstrip(".")
        if not d:
            return target == base_host
        return target == d or target.endswith("." + d)
    if scope_mode == "regex":
        if not scope_value:
            return False
        try:
            return bool(re.search(scope_value, target))
        except re.error:
            return False
    return target == base_host


def is_same_origin(url: str, base: str, *, include_subdomains: bool) -> bool:
    """Backwards-compat shim. Prefer :func:`is_in_scope`."""
    return is_in_scope(
        url, base,
        scope_mode="descendants" if include_subdomains else "host",
    )


def body_hash(content: str) -> str:
    """sha256 hex digest of UTF-8-encoded body. Used for catch-all-404 dedupe."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def extract_links(
    html: str,
    base_url: str,
    *,
    extractors: set[str] | None = None,
    custom_regex: str | re.Pattern[str] | None = None,
) -> list[ExtractedLink]:
    """Pull navigable links out of *html*, optionally including extras.

    *extractors* is a set drawn from KNOWN_EXTRACTORS; defaults to {"a", "area"}.
    *custom_regex* is an additional pattern run against the raw HTML; the first
    capturing group (or the whole match) is treated as a URL.

    Returns canonicalised absolute URLs. Drops self-references (links that
    canonicalise to *base_url*), non-http schemes, and fragment-only links.
    """
    if extractors is None:
        extractors = set(DEFAULT_EXTRACTORS)
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
    seen_urls: set[tuple[str, str]] = set()  # (canonical_url, kind)

    def _add(link: ExtractedLink | None) -> None:
        if link is None:
            return
        key = (link.url, link.kind)
        if key in seen_urls:
            return
        seen_urls.add(key)
        links.append(link)

    if "a" in extractors:
        for a in soup.find_all("a", href=True):
            _add(_build_link(a, base_url, base_canonical, kind="a"))

    if "area" in extractors:
        for area in soup.find_all("area", href=True):
            _add(_build_link(area, base_url, base_canonical, kind="area",
                             map_to_img=map_to_img))

    if "iframe" in extractors:
        for tag_name in ("iframe", "frame"):
            for el in soup.find_all(tag_name, src=True):
                _add(_simple_link(el.get("src"), el.get_text(strip=True),
                                  base_url, base_canonical, kind="iframe"))

    if "form" in extractors:
        for form in soup.find_all("form", action=True):
            _add(_simple_link(form.get("action"), "", base_url, base_canonical,
                              kind="form"))

    if "link" in extractors:
        for el in soup.find_all("link", href=True):
            rel = " ".join(el.get("rel") or []).lower()
            if any(k in rel for k in ("stylesheet", "icon", "preload",
                                      "preconnect", "dns-prefetch", "manifest")):
                continue
            _add(_simple_link(el.get("href"), rel, base_url, base_canonical,
                              kind="link"))

    if "onclick" in extractors:
        for el in soup.find_all(attrs={"onclick": True}):
            for m in _JS_URL_RE.finditer(el["onclick"]):
                url = m.group(1) or m.group(2)
                if url:
                    _add(_simple_link(url, el.get_text(strip=True), base_url,
                                      base_canonical, kind="onclick"))

    if "data_attrs" in extractors:
        for el in soup.find_all(True):
            for attr in ("data-href", "data-url", "data-link", "data-target"):
                val = el.get(attr)
                if val:
                    _add(_simple_link(val, el.get_text(strip=True), base_url,
                                      base_canonical, kind="data_attr"))

    if "text_url" in extractors:
        for m in _TEXT_URL_RE.finditer(soup.get_text(" ", strip=True)):
            _add(_simple_link(m.group(0), "", base_url, base_canonical,
                              kind="text_url"))

    if custom_regex is not None:
        pattern = (custom_regex if isinstance(custom_regex, re.Pattern)
                   else re.compile(custom_regex))
        for m in pattern.finditer(html):
            url = m.group(1) if m.lastindex else m.group(0)
            _add(_simple_link(url, "", base_url, base_canonical, kind="custom"))

    return links


def _simple_link(
    href: str | None,
    text: str,
    base_url: str,
    base_canonical: str,
    *,
    kind: str,
) -> ExtractedLink | None:
    """Build an ExtractedLink from a non-anchor source (no shape/coords)."""
    if not href:
        return None
    href = href.strip()
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
    return ExtractedLink(
        url=canonical, kind=kind, shape=None, coords=None,
        image_src=None, text=text or "",
    )


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
