from site_cartographer.links import is_same_origin


def test_exact_host_match():
    assert is_same_origin("https://example.com/page", "https://example.com/", include_subdomains=False)


def test_www_prefix_treated_as_same_origin_by_default():
    # `www.` is a no-op prefix on the public web — strip it on both sides.
    assert is_same_origin(
        "https://www.example.com/", "https://example.com/", include_subdomains=False
    )
    assert is_same_origin(
        "https://example.com/", "https://www.example.com/", include_subdomains=False
    )


def test_other_subdomains_rejected_unless_flag_set():
    assert not is_same_origin(
        "https://blog.example.com/", "https://example.com/", include_subdomains=False
    )
    assert is_same_origin(
        "https://blog.example.com/", "https://example.com/", include_subdomains=True
    )


def test_unrelated_host_rejected_either_way():
    assert not is_same_origin(
        "https://attacker.com/", "https://example.com/", include_subdomains=False
    )
    assert not is_same_origin(
        "https://attacker.com/", "https://example.com/", include_subdomains=True
    )


def test_scheme_mismatch_still_same_origin():
    # Some sites mix http/https — treat host-equal as same-origin so we don't double-crawl
    assert is_same_origin(
        "http://example.com/", "https://example.com/", include_subdomains=False
    )
