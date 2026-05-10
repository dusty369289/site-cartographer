from site_cartographer.links import canonicalize


def test_strips_fragment():
    assert canonicalize("https://example.com/page#section") == "https://example.com/page"


def test_lowercases_host():
    assert canonicalize("https://Example.COM/Path") == "https://example.com/Path"


def test_normalises_index_html():
    assert canonicalize("https://example.com/index.html") == "https://example.com/"
    assert canonicalize("https://example.com/dir/index.html") == "https://example.com/dir/"


def test_keeps_other_extensions():
    # .htm and bare paths are distinct resources, do not collapse them
    assert canonicalize("https://example.com/page.htm") == "https://example.com/page.htm"
    assert canonicalize("https://example.com/page") == "https://example.com/page"


def test_preserves_trailing_slash_distinction_for_files():
    # /foo and /foo/ are different at the protocol level — preserve as-is
    assert canonicalize("https://example.com/foo/") == "https://example.com/foo/"
    assert canonicalize("https://example.com/foo") == "https://example.com/foo"


def test_root_always_has_trailing_slash():
    assert canonicalize("https://example.com") == "https://example.com/"
    assert canonicalize("https://example.com/") == "https://example.com/"


def test_query_canonical_order():
    a = canonicalize("https://example.com/p?b=2&a=1")
    b = canonicalize("https://example.com/p?a=1&b=2")
    assert a == b


def test_keeps_scheme():
    # http and https are NOT collapsed — they are distinct origins
    assert canonicalize("http://example.com/") != canonicalize("https://example.com/")
