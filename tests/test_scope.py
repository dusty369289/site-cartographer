"""Tests for scope-mode-aware origin matching (is_in_scope)."""
from site_cartographer.links import is_in_scope


BASE = "https://public.3net.dev/"


def _in(target, **kw):
    return is_in_scope(target, BASE, **kw)


# host (default) ---------------------------------------------------------------
def test_host_mode_only_exact_match():
    assert _in("https://public.3net.dev/foo")
    assert _in("https://www.public.3net.dev/foo")  # www stripped
    assert not _in("https://ytrss.3net.dev/foo")
    assert not _in("https://3net.dev/")
    assert not _in("https://other.com/")


# descendants ------------------------------------------------------------------
def test_descendants_mode_includes_children_only():
    assert _in("https://public.3net.dev/", scope_mode="descendants")
    assert _in("https://api.public.3net.dev/", scope_mode="descendants")
    # siblings not included — they're not descendants of public.3net.dev
    assert not _in("https://ytrss.3net.dev/", scope_mode="descendants")
    assert not _in("https://3net.dev/", scope_mode="descendants")


# domain (sibling-friendly) ----------------------------------------------------
def test_domain_mode_matches_siblings_under_shared_domain():
    assert _in("https://public.3net.dev/", scope_mode="domain", scope_value="3net.dev")
    assert _in("https://ytrss.3net.dev/", scope_mode="domain", scope_value="3net.dev")
    assert _in("https://nested.public.3net.dev/", scope_mode="domain", scope_value="3net.dev")
    assert _in("https://3net.dev/", scope_mode="domain", scope_value="3net.dev")
    assert not _in("https://other.com/", scope_mode="domain", scope_value="3net.dev")
    # also accepts a leading dot
    assert _in("https://ytrss.3net.dev/", scope_mode="domain", scope_value=".3net.dev")


def test_domain_mode_with_blank_value_falls_back_to_host():
    assert _in("https://public.3net.dev/", scope_mode="domain", scope_value="")
    assert not _in("https://ytrss.3net.dev/", scope_mode="domain", scope_value="")


# regex ------------------------------------------------------------------------
def test_regex_mode_matches_pattern_against_host():
    assert _in("https://blog.example.com/", scope_mode="regex",
               scope_value=r"^(blog|news)\.example\.com$")
    assert _in("https://news.example.com/", scope_mode="regex",
               scope_value=r"^(blog|news)\.example\.com$")
    assert not _in("https://shop.example.com/", scope_mode="regex",
                   scope_value=r"^(blog|news)\.example\.com$")


def test_regex_mode_invalid_pattern_returns_false():
    assert not _in("https://anything/", scope_mode="regex", scope_value="(unclosed")


def test_regex_mode_blank_pattern_rejects_everything():
    assert not _in("https://public.3net.dev/", scope_mode="regex", scope_value="")


# legacy is_same_origin shim ---------------------------------------------------
def test_legacy_shim_still_works():
    from site_cartographer.links import is_same_origin
    assert is_same_origin("https://www.foo.com/", "https://foo.com/", include_subdomains=False)
    assert is_same_origin("https://x.foo.com/", "https://foo.com/", include_subdomains=True)
    assert not is_same_origin("https://x.foo.com/", "https://foo.com/", include_subdomains=False)
