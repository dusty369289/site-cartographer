import pytest

from site_cartographer.archive import format_size, parse_size


@pytest.mark.parametrize("text,expected", [
    ("0", 0),
    ("100", 100),
    ("100B", 100),
    ("1K", 1024),
    ("1KB", 1024),
    ("100kb", 100 * 1024),
    ("1.5M", int(1.5 * 1024**2)),
    ("2GB", 2 * 1024**3),
    ("1T", 1024**4),
    ("  500MB  ", 500 * 1024**2),
])
def test_parse_size_accepts_common_forms(text, expected):
    assert parse_size(text) == expected


@pytest.mark.parametrize("bad", ["", "abc", "100ZB", "1.2.3M", "M100", "-1K"])
def test_parse_size_rejects_garbage(bad):
    with pytest.raises(ValueError):
        parse_size(bad)


def test_parse_size_passthrough_int():
    assert parse_size(500) == 500


@pytest.mark.parametrize("n,expected_substring", [
    (0, "0B"),
    (500, "500B"),
    (2048, "2.0KB"),
    (1024 * 1024, "1.0MB"),
    (3 * 1024**3, "3.0GB"),
])
def test_format_size_picks_appropriate_unit(n, expected_substring):
    assert format_size(n) == expected_substring
