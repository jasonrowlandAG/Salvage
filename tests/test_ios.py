from __future__ import annotations

import hashlib
import plistlib
import sqlite3
import struct
from pathlib import Path

import pytest
from Crypto.Cipher import AES

from salvage.engine import ios
from salvage.engine.ios import (
    BackupKeyBag,
    BackupPasswordError,
    BackupReader,
    _aes_key_unwrap,
    _PROGRESS_RE,
    _manifest_file_size,
    _parse_size,
)
from salvage.engine.ios_fixtures import build_synthetic_encrypted_backup

# ---------------------------------------------------------------------------
# Progress-line regex (lines captured from a real idevicebackup2 run)
# ---------------------------------------------------------------------------


def test_progress_regex_matches_byte_progress_line():
    line = "[========                                          ]  16% (103.6 KB/661.8 KB)     "
    m = _PROGRESS_RE.search(line)
    assert m is not None
    assert m.group(1) == "16"
    assert m.group(2) == "103.6 KB"
    assert m.group(3) == "661.8 KB"


def test_progress_regex_matches_full_progress_line():
    line = "[==================================================] 100% (14.1 MB/13.1 MB)     "
    m = _PROGRESS_RE.search(line)
    assert m is not None
    assert m.group(1) == "100"


def test_progress_regex_matches_finished_variant_with_no_byte_counts():
    line = "[============                                      ]  22% Finished"
    m = _PROGRESS_RE.search(line)
    assert m is not None
    assert m.group(1) == "22"
    assert m.group(2) is None
    assert m.group(3) is None


def test_parse_size_converts_units_to_bytes():
    assert _parse_size("1.0 KB") == 1024
    assert _parse_size("1.0 MB") == 1024**2
    assert _parse_size("103.6 KB") == int(103.6 * 1024)
    assert _parse_size("14.1 MB") == int(14.1 * 1024**2)


# ---------------------------------------------------------------------------
# Manifest.db parsing (synthetic fixture, no real device needed)
# ---------------------------------------------------------------------------


def _make_file_blob(size: int) -> bytes:
    """Approximate the NSKeyedArchiver plist libimobiledevice stores in Files.file."""
    blob = {
        "$archiver": "NSKeyedArchiver",
        "$version": 100000,
        "$objects": [
            "$null",
            {"Size": size, "Mode": 33188, "Birth": 700000000, "LastModified": 700000001},
            {"$classname": "MBFile"},
        ],
        "$top": {"root": plistlib.UID(1)},
    }
    return plistlib.dumps(blob, fmt=plistlib.FMT_BINARY)


def _write_payload(backup_dir: Path, file_id: str, content: bytes) -> None:
    shard = backup_dir / file_id[:2]
    shard.mkdir(parents=True, exist_ok=True)
    (shard / file_id).write_bytes(content)


def _build_synthetic_backup(tmp_path: Path) -> Path:
    backup_dir = tmp_path / "00008101-001C60C12E52001E"
    backup_dir.mkdir(parents=True)

    info_plist = {
        "Device Name": "iPhone (6)",
        "Product Type": "iPhone13,2",
        "Product Version": "18.7.8",
        "Last Backup Date": "2026-09-06T00:00:00Z",
    }
    (backup_dir / "Info.plist").write_bytes(plistlib.dumps(info_plist))
    (backup_dir / "Manifest.plist").write_bytes(plistlib.dumps({"IsEncrypted": False}))

    manifest_db = backup_dir / "Manifest.db"
    conn = sqlite3.connect(manifest_db)
    conn.execute(
        "CREATE TABLE Files (fileID TEXT PRIMARY KEY, domain TEXT, relativePath TEXT, "
        "flags INTEGER, file BLOB)"
    )

    sms_content = b"sqlite-sms-payload"
    sms_id = "aa" * 20
    conn.execute(
        "INSERT INTO Files VALUES (?, ?, ?, ?, ?)",
        (sms_id, "HomeDomain", "Library/SMS/sms.db", 1, _make_file_blob(len(sms_content))),
    )
    _write_payload(backup_dir, sms_id, sms_content)

    # a WAL sidecar for sms.db, so extract_known should pick it up too
    wal_content = b"wal-bytes"
    wal_id = "bb" * 20
    conn.execute(
        "INSERT INTO Files VALUES (?, ?, ?, ?, ?)",
        (wal_id, "HomeDomain", "Library/SMS/sms.db-wal", 1, _make_file_blob(len(wal_content))),
    )
    _write_payload(backup_dir, wal_id, wal_content)

    # a directory entry (flags=2) that must NOT show up as a BackupFile
    conn.execute(
        "INSERT INTO Files VALUES (?, ?, ?, ?, ?)",
        ("cc" * 20, "HomeDomain", "Library/SMS", 2, None),
    )

    conn.commit()
    conn.close()
    return backup_dir


def test_backup_reader_info_reads_info_and_manifest_plist(tmp_path):
    backup_dir = _build_synthetic_backup(tmp_path)
    reader = BackupReader(backup_dir)
    try:
        info = reader.info()
        assert info["device_name"] == "iPhone (6)"
        assert info["product_type"] == "iPhone13,2"
        assert info["product_version"] == "18.7.8"
        assert info["is_encrypted"] is False
    finally:
        reader.close()


def test_backup_reader_find_filters_by_domain_and_flags(tmp_path):
    backup_dir = _build_synthetic_backup(tmp_path)
    reader = BackupReader(backup_dir)
    try:
        results = reader.find(domain="HomeDomain")
        paths = {r.relative_path for r in results}
        assert paths == {"Library/SMS/sms.db", "Library/SMS/sms.db-wal"}

        sms = next(r for r in results if r.relative_path == "Library/SMS/sms.db")
        assert sms.size == len(b"sqlite-sms-payload")
        assert sms.path_on_disk.exists()
    finally:
        reader.close()


def test_backup_reader_extract_known_copies_file_and_wal_sidecar(tmp_path):
    backup_dir = _build_synthetic_backup(tmp_path)
    reader = BackupReader(backup_dir)
    try:
        dest_dir = tmp_path / "extracted"
        result = reader.extract_known("sms", dest_dir)
        assert result == dest_dir / "sms.db"
        assert result.read_bytes() == b"sqlite-sms-payload"
        assert (dest_dir / "sms.db-wal").read_bytes() == b"wal-bytes"
    finally:
        reader.close()


def test_backup_reader_extract_known_returns_none_for_missing_key(tmp_path):
    backup_dir = _build_synthetic_backup(tmp_path)
    reader = BackupReader(backup_dir)
    try:
        assert reader.extract_known("notes", tmp_path / "out") is None
    finally:
        reader.close()


def test_backup_reader_raises_without_manifest_db(tmp_path):
    with pytest.raises(FileNotFoundError):
        BackupReader(tmp_path)


# ---------------------------------------------------------------------------
# Encrypted-backup decryption: BackupKeyBag / RFC 3394 key unwrap / AES-CBC
# ---------------------------------------------------------------------------


def test_aes_key_unwrap_matches_rfc3394_test_vector():
    # From RFC 3394 §4.1 ("Wrap 128 bits of Key Data with a 128-bit KEK") — an
    # independent check of _aes_key_unwrap against the published spec, not just
    # self-consistency with our own wrap helper.
    kek = bytes.fromhex("000102030405060708090A0B0C0D0E0F")
    wrapped = bytes.fromhex("1FA68B0A8112B447AEF34BD8FB5A7B829D3E862371D2CFE5"[:48])
    expected = bytes.fromhex("00112233445566778899AABBCCDDEEFF"[:32])
    assert _aes_key_unwrap(kek, wrapped) == expected


def test_aes_key_unwrap_raises_on_wrong_key():
    kek = bytes.fromhex("000102030405060708090A0B0C0D0E0F")
    wrapped = bytes.fromhex("1FA68B0A8112B447AEF34BD8FB5A7B829D3E862371D2CFE5"[:48])
    wrong_kek = b"\x00" * 16
    with pytest.raises(ValueError):
        _aes_key_unwrap(wrong_kek, wrapped)


def _tlv(tag: str, value: bytes) -> bytes:
    return tag.encode("ascii") + struct.pack(">I", len(value)) + value


def _build_minimal_keybag(password: str, class_num: int = 4) -> tuple[bytes, bytes]:
    """A from-scratch, hand-rolled keybag (not sharing code with ios.py's decrypt
    path, nor with ios_fixtures.py's fuller fixture) — wraps one class key with
    the passcode key derived exactly as BackupKeyBag.derive_class_keys expects.
    Returns (keybag_bytes, class_key).
    """
    dpsl = b"unit-test-dpsl-1"
    salt = b"unit-test-salt-2"
    dpic = 100
    iterations = 100
    intermediate = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), dpsl, dpic, dklen=32)
    passcode_key = hashlib.pbkdf2_hmac("sha1", intermediate, salt, iterations, dklen=32)

    class_key = b"\x11" * 32

    # RFC 3394 wrap, written independently of ios._aes_key_unwrap (this is the
    # "encrypt helper" side — wrap is unwrap run forward).
    n = len(class_key) // 8
    r = [class_key[8 * i : 8 * i + 8] for i in range(n)]
    a = b"\xa6" * 8
    cipher = AES.new(passcode_key, AES.MODE_ECB)
    for j in range(6):
        for i in range(1, n + 1):
            block = cipher.encrypt(a + r[i - 1])
            t = n * j + i
            a = (int.from_bytes(block[:8], "big") ^ t).to_bytes(8, "big")
            r[i - 1] = block[8:]
    wpky = a + b"".join(r)

    keybag = b"".join(
        _tlv(tag, value)
        for tag, value in [
            ("TYPE", struct.pack(">I", 0)),
            ("UUID", b"\x01" * 16),
            ("SALT", salt),
            ("ITER", struct.pack(">I", iterations)),
            ("DPIC", struct.pack(">I", dpic)),
            ("DPSL", dpsl),
            ("UUID", b"\x02" * 16),
            ("CLAS", struct.pack(">I", class_num)),
            ("WRAP", struct.pack(">I", 2)),
            ("WPKY", wpky),
        ]
    )
    return keybag, class_key


def test_backup_keybag_derives_correct_class_key_with_right_password():
    keybag_bytes, class_key = _build_minimal_keybag("hunter2")
    keybag = BackupKeyBag.parse(keybag_bytes)
    class_keys = keybag.derive_class_keys("hunter2")
    assert class_keys == {4: class_key}


def test_backup_keybag_wrong_password_raises():
    keybag_bytes, _class_key = _build_minimal_keybag("hunter2")
    keybag = BackupKeyBag.parse(keybag_bytes)
    with pytest.raises(BackupPasswordError):
        keybag.derive_class_keys("wrong-password")


# ---------------------------------------------------------------------------
# Full encrypted-backup round trip (BackupReader against a synthetic backup
# built the same way salvage.engine.fake's fake encrypted device is)
# ---------------------------------------------------------------------------

_ENC_PASSWORD = "test1234"


def test_backup_reader_decrypts_encrypted_backup(tmp_path):
    backup_dir = build_synthetic_encrypted_backup(tmp_path, "ENCUDID", _ENC_PASSWORD)
    reader = BackupReader(backup_dir, password=_ENC_PASSWORD)
    try:
        assert reader.is_encrypted is True
        assert reader.needs_password is False
        assert reader.info()["is_encrypted"] is True

        dest_dir = tmp_path / "extracted"
        sms_path = reader.extract_known("sms", dest_dir)
        assert sms_path is not None
        assert sms_path.read_bytes().startswith(b"SQLite format 3\x00")

        calls_path = reader.extract_known("call_history", dest_dir)
        assert calls_path is not None
        assert calls_path.read_bytes().startswith(b"SQLite format 3\x00")

        safari_path = reader.extract_known("safari_history", dest_dir)
        assert safari_path is not None
        assert safari_path.read_bytes().startswith(b"SQLite format 3\x00")
    finally:
        reader.close()


def test_backup_reader_extracted_call_history_and_safari_history_parse_correctly(tmp_path):
    from salvage.engine.ios_parsers import parse_call_history, parse_safari_history

    backup_dir = build_synthetic_encrypted_backup(tmp_path, "ENCUDID2", _ENC_PASSWORD)
    reader = BackupReader(backup_dir, password=_ENC_PASSWORD)
    try:
        dest_dir = tmp_path / "extracted2"
        calls = parse_call_history(reader.extract_known("call_history", dest_dir))
        assert len(calls) == 3
        assert calls[0].address == "+61400000005"
        assert calls[0].outgoing is True

        visits = parse_safari_history(reader.extract_known("safari_history", dest_dir))
        assert len(visits) == 2
        assert {v.url for v in visits} == {
            "https://example.com/",
            "https://www.assemblygrowth.com/",
        }
    finally:
        reader.close()


def test_backup_reader_wrong_password_raises_backup_password_error(tmp_path):
    backup_dir = build_synthetic_encrypted_backup(tmp_path, "ENCUDID3", _ENC_PASSWORD)
    with pytest.raises(BackupPasswordError):
        BackupReader(backup_dir, password="not-the-password")


def test_backup_reader_no_password_leaves_reader_locked(tmp_path):
    backup_dir = build_synthetic_encrypted_backup(tmp_path, "ENCUDID4", _ENC_PASSWORD)
    reader = BackupReader(backup_dir)
    try:
        assert reader.is_encrypted is True
        assert reader.needs_password is True
        with pytest.raises(BackupPasswordError):
            reader.find()
    finally:
        reader.close()


def test_backup_reader_caches_decrypted_manifest_db(tmp_path):
    backup_dir = build_synthetic_encrypted_backup(tmp_path, "ENCUDID5", _ENC_PASSWORD)
    reader = BackupReader(backup_dir, password=_ENC_PASSWORD)
    reader.close()

    cache_path = backup_dir.parent / "salvage_cache" / backup_dir.name / "Manifest.decrypted.db"
    assert cache_path.exists()
    assert cache_path.read_bytes().startswith(b"SQLite format 3\x00")

    # Reopening reuses the cache rather than re-deriving from scratch; still readable.
    reader2 = BackupReader(backup_dir, password=_ENC_PASSWORD)
    try:
        assert reader2.find(domain="HomeDomain", relative_path_like="Library/SMS/sms.db")
    finally:
        reader2.close()


def test_manifest_file_size_parses_synthetic_blob():
    assert _manifest_file_size(_make_file_blob(42)) == 42


def test_manifest_file_size_returns_none_for_garbage():
    assert _manifest_file_size(b"not a plist") is None
    assert _manifest_file_size(None) is None


# ---------------------------------------------------------------------------
# AFC ls/info output parsing helpers
# ---------------------------------------------------------------------------


def test_strip_afc_chrome_removes_ansi_and_prompts():
    raw = (
        "\x1b[0;30m\x1b[47mafc:\x1b[m\x1b[1;33m\x1b[44m/\x1b[m > "
        "-rw-r--r--    1 mobile mobile    1118573 22 Mar 2025 13:21:26 IMG_5716.PNG\n"
        "\x1b[0;30m\x1b[47mafc:\x1b[m\x1b[1;33m\x1b[44m/\x1b[m > "
    )
    lines = ios._strip_afc_chrome(raw)
    assert lines == ["-rw-r--r--    1 mobile mobile    1118573 22 Mar 2025 13:21:26 IMG_5716.PNG"]


def test_afc_ls_regex_parses_real_file_and_directory_lines():
    file_line = "-rw-r--r--    1 mobile mobile    1118573 22 Mar 2025 13:21:26 IMG_5716.PNG"
    dir_line = "drwxr-xr-x    2 mobile mobile        128 06 Sep 2026 08:29:25 109APPLE"

    m = ios._AFC_LS_RE.match(file_line)
    assert m is not None
    kind, size, mtime_str, name = m.groups()
    assert kind == "-"
    assert size == "1118573"
    assert name == "IMG_5716.PNG"

    m = ios._AFC_LS_RE.match(dir_line)
    assert m is not None
    assert m.group(1) == "d"
    assert m.group(4) == "109APPLE"


# ---------------------------------------------------------------------------
# Real-device tests — skipped unless a device is actually attached
# ---------------------------------------------------------------------------

_have_device = bool(ios.list_ios_devices())

pytestmark_device = pytest.mark.skipif(not _have_device, reason="no iOS device attached via USB")


@pytestmark_device
def test_list_ios_devices_reports_real_device():
    devices = ios.list_ios_devices()
    assert devices
    assert all(d.udid and d.name and d.product_type and d.ios_version for d in devices)
