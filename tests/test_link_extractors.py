"""Tests for the pluggable link extractors."""
from pathlib import Path

from site_cartographer.links import (
    ALL_EXTRACTORS,
    COMMON_EXTRACTORS,
    DEFAULT_EXTRACTORS,
    KNOWN_EXTRACTORS,
    extract_links,
)

FIXTURES = Path(__file__).parent / "fixtures"
BASE = "https://example.com/section/"


def _load() -> str:
    return (FIXTURES / "rich_links_page.html").read_text(encoding="utf-8")


def _kinds(links) -> set[str]:
    return {link.kind for link in links}


def _urls(links) -> set[str]:
    return {link.url for link in links}


def test_defaults_only_a_and_area():
    # Anchor only (no <area> in this fixture).
    links = extract_links(_load(), BASE)
    assert _kinds(links) == {"a"}
    assert "https://example.com/about" in _urls(links)


def test_iframe_extractor_picks_up_iframe_and_frame():
    links = extract_links(_load(), BASE, extractors={"iframe"})
    urls = _urls(links)
    assert "https://example.com/widgets/embed-1" in urls
    assert "https://example.com/legacy-frame.htm" in urls
    assert _kinds(links) == {"iframe"}


def test_form_extractor():
    links = extract_links(_load(), BASE, extractors={"form"})
    assert _urls(links) == {"https://example.com/submit"}
    assert _kinds(links) == {"form"}


def test_link_extractor_skips_stylesheet_but_keeps_alternate():
    links = extract_links(_load(), BASE, extractors={"link"})
    urls = _urls(links)
    assert "https://example.com/feed.rss" in urls
    assert "https://example.com/canonical-here" in urls
    assert "https://example.com/style.css" not in urls


def test_onclick_extractor_finds_window_location():
    links = extract_links(_load(), BASE, extractors={"onclick"})
    urls = _urls(links)
    assert "https://example.com/onclick-target" in urls
    assert "https://example.com/onclick-target-2" in urls


def test_data_attrs_extractor():
    links = extract_links(_load(), BASE, extractors={"data_attrs"})
    urls = _urls(links)
    assert "https://example.com/data-href-target" in urls
    assert "https://example.com/data-url-target" in urls


def test_text_url_extractor():
    links = extract_links(_load(), BASE, extractors={"text_url"})
    urls = _urls(links)
    assert "https://text-url.example.com/page" in urls


def test_custom_regex_with_capture_group():
    # User-defined extractor: pull URLs from HTML comments.
    links = extract_links(
        _load(), BASE,
        extractors=set(),  # disable everything else
        custom_regex=r"<!--[^>]*?(https?://\S+)[^>]*?-->",
    )
    urls = _urls(links)
    assert "https://commented.example.com/" in urls


def test_combined_extractors_dedupe_by_url():
    # Same destination via both <a> and data-href shouldn't appear twice.
    html = '<a href="/x">A</a><span data-href="/x">D</span>'
    links = extract_links(html, "https://e.com/", extractors={"a", "data_attrs"})
    urls_kinds = [(link.url, link.kind) for link in links]
    # The URL appears once per kind (a, data_attr) since dedupe is keyed by both.
    assert len(urls_kinds) == 2
    kinds = {k for _, k in urls_kinds}
    assert kinds == {"a", "data_attr"}


def test_known_extractors_constants():
    assert "a" in KNOWN_EXTRACTORS
    assert "area" in KNOWN_EXTRACTORS
    assert set(DEFAULT_EXTRACTORS).issubset(set(KNOWN_EXTRACTORS))
    assert set(COMMON_EXTRACTORS).issubset(set(KNOWN_EXTRACTORS))
    assert set(ALL_EXTRACTORS) == set(KNOWN_EXTRACTORS)


def test_skips_javascript_and_mailto_in_a_extractor():
    # Existing behaviour preserved for the default <a> extractor.
    html = '<a href="javascript:alert(1)">x</a><a href="mailto:a@b.c">y</a>'
    assert extract_links(html, "https://e.com/", extractors={"a"}) == []
