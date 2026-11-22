"""Time-remaining estimate on the scan page."""
from salvage.engine.models import ScanProgress
from salvage.ui.format_utils import human_remaining
from salvage.ui.scan_page import ScanPage


def test_no_estimate_until_enough_progress_to_extrapolate():
    # Too early: the rate is noise, and a wrong first guess is worse than none.
    early = ScanProgress(sector=1, total_sectors=1000, elapsed_s=5)
    assert ScanPage._remaining_text(early) == "Estimating time left..."


def test_estimate_appears_once_progress_is_meaningful():
    # 25% done after 60s => ~180s left => "About 3 minutes left".
    p = ScanProgress(sector=250, total_sectors=1000, elapsed_s=60)
    assert ScanPage._remaining_text(p) == "About 3 minutes left"


def test_unknown_total_sectors_does_not_fabricate_an_estimate():
    assert ScanPage._remaining_text(ScanProgress(elapsed_s=30)) == "Estimating time left..."


def test_bands_avoid_false_precision():
    assert human_remaining(None) == "Estimating time left..."
    assert human_remaining(30) == "About a minute left"
    assert human_remaining(1500) == "About 25 minutes left"   # rounded to 5-minute steps
    assert human_remaining(5000) == "Over an hour left"
