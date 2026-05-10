from pathlib import Path

from site_cartographer.links import extract_links

FIXTURES = Path(__file__).parent / "fixtures"


def test_extracts_area_and_a_tags():
    html = (FIXTURES / "imagemap_page.html").read_text(encoding="utf-8")
    base = "https://www.ourmachineisdown.com/hub/"
    links = extract_links(html, base_url=base)

    kinds = [link.kind for link in links]
    assert kinds.count("area") == 4  # excluding href="#"
    assert kinds.count("a") == 1  # mailto + javascript skipped, plain.html kept

    # poly coords parsed into ints
    poly = next(link for link in links if link.shape == "poly")
    assert poly.coords == [200, 200, 300, 250, 250, 350]
    assert poly.url == "https://www.ourmachineisdown.com/beta/"
    assert poly.image_src == "https://www.ourmachineisdown.com/hub/hub.jpg"

    # absolute href preserved
    gamma = next(link for link in links if link.url.endswith("/gamma"))
    assert gamma.shape == "circle"
    assert gamma.coords == [400, 400, 50]

    # external link captured (filtering happens in crawler, not links.py)
    assert any("external.example.com" in link.url for link in links)


def test_skips_self_anchor_and_non_http_schemes():
    html = (FIXTURES / "imagemap_page.html").read_text(encoding="utf-8")
    links = extract_links(html, base_url="https://www.ourmachineisdown.com/hub/")
    assert not any(link.url.startswith("mailto:") for link in links)
    assert not any(link.url.startswith("javascript:") for link in links)
    assert not any(link.url.endswith("#") for link in links)


def test_relative_urls_resolved_against_base():
    html = (FIXTURES / "plain_links_page.html").read_text(encoding="utf-8")
    base = "https://example.com/section/"
    links = extract_links(html, base_url=base)
    urls = {link.url for link in links}
    assert "https://example.com/about" in urls
    # query preserved through canonicalisation
    assert any(u.startswith("https://example.com/contact") and "utm=foo" in u for u in urls)


def test_fragment_only_links_dropped():
    html = (FIXTURES / "plain_links_page.html").read_text(encoding="utf-8")
    base = "https://example.com/section/"
    links = extract_links(html, base_url=base)
    # #section against base resolves to the base URL itself — dropped as same-page
    assert not any(link.url.endswith("#section") for link in links)
