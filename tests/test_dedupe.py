from site_cartographer.links import body_hash


def test_identical_bodies_hash_identically():
    a = "<html><body>Same content</body></html>"
    b = "<html><body>Same content</body></html>"
    assert body_hash(a) == body_hash(b)


def test_different_bodies_hash_differently():
    a = "<html><body>Content A</body></html>"
    b = "<html><body>Content B</body></html>"
    assert body_hash(a) != body_hash(b)


def test_hash_is_stable_hex_string():
    h = body_hash("<html></html>")
    assert isinstance(h, str)
    assert len(h) == 64  # sha256 hex
    int(h, 16)  # parses as hex
