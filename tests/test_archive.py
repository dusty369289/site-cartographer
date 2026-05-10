from pathlib import Path

import pytest

from site_cartographer.archive import format_size, parse_size
from site_cartographer.crawler import CrawlConfig


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


@pytest.mark.parametrize("policy", ["ignore", "metadata", "archive", "crawl"])
def test_crawl_config_accepts_valid_external_policies(policy, tmp_path):
    cfg = CrawlConfig(
        start_url="https://example.com/",
        output_dir=tmp_path,
        external_policy=policy,
    )
    assert cfg.external_policy == policy


def test_crawl_config_rejects_unknown_external_policy(tmp_path):
    with pytest.raises(ValueError, match="external_policy"):
        CrawlConfig(
            start_url="https://example.com/",
            output_dir=tmp_path,
            external_policy="bogus",
        )
