"""Unit tests for the resume-bump suggestion logic.

These cover the "what should we pre-fill in the resume form?" decision based
on the previous run's halt reason and depth-drop counter.
"""
from site_cartographer.tui import _suggest_resume_bumps


def _bumps(halt, dropped, *, pages=100, depth=15, size=None):
    return _suggest_resume_bumps(halt, dropped, pages, depth, size)


def test_max_pages_halt_doubles_pages_only():
    pages, depth, size = _bumps("reached max-pages cap (100)", 0)
    assert (pages, depth, size) == (200, 15, None)


def test_max_file_size_halt_doubles_size_only():
    pages, depth, size = _bumps(
        "reached max-file-size cap (...)", 0, size=500 * 1024 * 1024
    )
    assert pages == 100
    assert depth == 15
    assert size == 1000 * 1024 * 1024  # exactly 2× 500MiB


def test_interrupted_by_user_changes_nothing():
    assert _bumps("interrupted by user", 0) == (100, 15, None)


def test_queue_drained_changes_nothing():
    assert _bumps("queue drained", 0) == (100, 15, None)


def test_no_halt_reason_changes_nothing():
    assert _bumps(None, 0) == (100, 15, None)


def test_depth_dropped_doubles_depth_independent_of_halt():
    # User interrupted, but during the run 5 URLs were dropped for depth.
    # Depth still gets doubled because depth-drops are an independent signal.
    pages, depth, size = _bumps("interrupted by user", 5)
    assert (pages, depth, size) == (100, 30, None)


def test_pages_cap_and_depth_drops_combine():
    pages, depth, size = _bumps("reached max-pages cap (50)", 7, pages=50, depth=5)
    assert (pages, depth, size) == (100, 10, None)


def test_max_file_size_unlimited_stays_unlimited():
    # Halt for size, but cur_max_size is None — don't fabricate one.
    assert _bumps("reached max-file-size cap (...)", 0, size=None) == (100, 15, None)
