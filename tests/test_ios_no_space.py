"""A backup refused for lack of space must name the side that is actually short.

Observed: iPhone 256 GB with 181 GB free, Mac drive 14 GB free. The device answers
"ErrorCode 105: Insufficient free disk space on drive to back up", which the old code
did not recognise (it looked for "No space left on device") and which, once matched,
would have blamed the iPhone's storage.
"""
from pathlib import Path

from salvage.engine import ios


def _patch(monkeypatch, tmp_path, dest_free, capacity, phone_free):
    class U:  # shutil.disk_usage result
        free = dest_free
    monkeypatch.setattr(ios.shutil, "disk_usage", lambda _p: U)
    vals = {"TotalDataCapacity": str(int(capacity)), "AmountDataAvailable": str(int(phone_free))}
    monkeypatch.setattr(ios, "_ideviceinfo_value", lambda udid, key, domain=None: vals.get(key))


def test_device_message_is_recognised():
    for text in ("ErrorCode 105: Insufficient free disk space on drive to back up (MBErrorDomain/105)",
                 "Backup Failed (MBErrorDomain/105)"):
        assert any(m in text for m in ios._INSUFFICIENT_SPACE)


def test_full_mac_drive_is_blamed_not_the_phone(monkeypatch, tmp_path):
    _patch(monkeypatch, tmp_path, dest_free=14e9, capacity=247e9, phone_free=181e9)
    msg = ios._no_space_message("udid", tmp_path)
    assert "14.0 GB" in msg and "66.0 GB" in msg
    assert "drive with more room" in msg
    assert "iPhone itself is nearly full" not in msg


def test_nearly_full_phone_is_blamed_when_the_mac_has_room(monkeypatch, tmp_path):
    _patch(monkeypatch, tmp_path, dest_free=500e9, capacity=247e9, phone_free=1e9)
    msg = ios._no_space_message("udid", tmp_path)
    assert "iPhone itself is nearly full" in msg


def test_message_survives_an_unreadable_device(monkeypatch, tmp_path):
    class U:
        free = 5e9
    monkeypatch.setattr(ios.shutil, "disk_usage", lambda _p: U)
    monkeypatch.setattr(ios, "_ideviceinfo_value", lambda *a, **k: None)
    assert "5.0 GB free" in ios._no_space_message("udid", tmp_path)


def test_devices_own_wording_is_quoted_alongside_the_numbers(monkeypatch, tmp_path):
    _patch(monkeypatch, tmp_path, dest_free=14e9, capacity=247e9, phone_free=181e9)
    msg = ios._no_space_message("udid", tmp_path, "Insufficient free disk space on drive to back up (error 105)")
    assert "The iPhone reported: Insufficient free disk space on drive to back up (error 105)." in msg
    assert "14.0 GB" in msg


def test_unreadable_phone_storage_is_not_mistaken_for_a_full_phone(monkeypatch, tmp_path):
    class U:
        free = 15.9e9
    monkeypatch.setattr(ios.shutil, "disk_usage", lambda _p: U)
    monkeypatch.setattr(ios, "_ideviceinfo_value", lambda *a, **k: None)
    assert "nearly full" not in ios._no_space_message("udid", tmp_path)
