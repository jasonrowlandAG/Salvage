from __future__ import annotations

from pathlib import Path

import pytest

from salvage.engine.models import ScanMode
from salvage.engine.photorec import (
    PhotoRecEngine,
    _access_denied_message,
    _parse_blocked_device,
)
from salvage.engine.results import recover_files

TEST_IMAGE = Path("/Users/jasonrowland/Salvage/.probe/test_partition.raw")

_binary = PhotoRecEngine.locate_binary()

pytestmark = pytest.mark.skipif(
    _binary is None or not TEST_IMAGE.exists(),
    reason="photorec binary or .probe/test_partition.raw not available",
)


def test_deep_scan_recovers_jpg(tmp_path):
    engine = PhotoRecEngine()
    result = engine.scan(TEST_IMAGE, tmp_path, ScanMode.DEEP)

    assert result.success
    assert result.error is None
    assert not result.cancelled
    assert result.output_dirs
    assert any(f.ext == "jpg" for f in result.files)


def test_extension_filter_limits_results(tmp_path):
    engine = PhotoRecEngine()
    result = engine.scan(TEST_IMAGE, tmp_path, ScanMode.DEEP, extensions=["jpg"])

    assert result.success
    assert result.files
    assert all(f.ext == "jpg" for f in result.files)


def test_recover_files_copies_and_organises(tmp_path):
    engine = PhotoRecEngine()
    result = engine.scan(TEST_IMAGE, tmp_path / "scan", ScanMode.DEEP, extensions=["jpg"])
    assert result.files

    dest = tmp_path / "out"
    copied = recover_files(result.files, dest, organise_by_category=True)

    assert len(copied) == len(result.files)
    assert all(p.exists() for p in copied)
    assert (dest / "Image").is_dir()


# ---------------------------------------------------------------------------
# macOS "Operation not permitted" parsing (no PhotoRec binary needed)
# ---------------------------------------------------------------------------

# Verbatim (ANSI-stripped) text from a real failed run against the internal
# APFS container disk3, captured in photorec.out - PhotoRec exited before
# ever writing photorec.log, so this has to be parsed from the streamed output.
_REAL_OPERATION_NOT_PERMITTED_OUTPUT = (
    "PhotoRec 7.2, Data Recovery Utility, February 2024\r\n"
    "Christophe GRENIER <grenier@cgsecurity.org>\r\n"
    "https://www.cgsecurity.org\r\n"
    "\r\n"
    "Unable to open file or device /dev/rdisk3: Operation not permitted\r\n"
)


def test_parse_blocked_device_matches_operation_not_permitted():
    device = _parse_blocked_device(_REAL_OPERATION_NOT_PERMITTED_OUTPUT)
    assert device == "/dev/rdisk3"


def test_parse_blocked_device_matches_permission_denied():
    text = "Unable to open file or device /dev/rdisk5: Permission denied\r\n"
    assert _parse_blocked_device(text) == "/dev/rdisk5"


def test_parse_blocked_device_returns_none_for_unrelated_text():
    assert _parse_blocked_device("PhotoRec exited normally.") is None


def test_access_denied_message_mentions_device_full_disk_access_and_filevault():
    message = _access_denied_message("/dev/rdisk3")
    assert "/dev/rdisk3" in message
    assert "Full Disk Access" in message
    assert "FileVault" in message
    assert "external drives" in message


def test_photorec_toggles_map_extensions_to_detector_families():
    from salvage.engine.photorec import _photorec_toggles

    # Extensions PhotoRec has no toggle for must ride their family, de-duplicated.
    assert _photorec_toggles(["jpg", "jpeg", "heic", "cr2", "dng"]) == ["jpg", "mov", "raw"]
    assert _photorec_toggles(["docx", "xlsx", "pdf"]) == ["zip", "pdf"]


def test_build_cmd_never_emits_unknown_toggles():
    from salvage.engine.photorec import PhotoRecEngine, _TOGGLE_BY_EXT

    cmd = PhotoRecEngine._build_cmd(ScanMode.DEEP, ["jpeg", "heic", "webp", "rtf", "csv"])
    emitted = {p for p in cmd.split(",")} - {"partition_none", "options", "wholespace", "freespace",
                                             "fileopt", "everything", "disable", "enable", "search"}
    assert emitted == {"jpg", "mov", "riff", "doc", "txt"}
    assert not (emitted & set(_TOGGLE_BY_EXT)), "raw extensions must be mapped away"
