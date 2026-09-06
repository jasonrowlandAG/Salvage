"""Pure-Python SQLite deleted-row recovery.

SQLite never zeroes a cell's bytes when a row is deleted: it either shrinks the
cell-pointer array (leaving the old cell bytes sitting in the page's now
"unallocated" gap) or links the freed space into the page's freeblock chain
(overwriting only the first 4 bytes with next/size). A page whose every cell is
deleted is queued on the freelist and its content is often left untouched until
reused. This module walks all three: per-page unallocated gaps, per-page
freeblock chains, and the freelist trunk/leaf chain — parsing anything that
looks like a table b-tree leaf cell and scoring it against a table's live
schema (via PRAGMA table_info) so callers can tell real remnants from noise.

No support for overflow pages: a candidate whose payload doesn't fit entirely
on the page it starts on is rejected. That's fine for the short deleted rows
this is meant to find (message/contact/note text rarely needs overflow) and
keeps the parser simple.

Freeblock caveat (confirmed empirically, not just from the file-format spec):
SQLite stamps a freed cell's own first 4 bytes with [next_freeblock, size]
the moment it's deleted, whether or not it later gets coalesced with a
neighbour. Those 4 bytes (payload-length + rowid + the start of the record
header) are gone for good; only the record's tail survives. That tail isn't
byte-aligned to a known column boundary without also knowing the original
varint widths, which the clobber destroys along with everything else — so
this walker does not attempt a "headerless" reconstruction of freeblock
interiors. What it does reliably recover: deleted cells whose space SQLite
reclaimed straight into the page's unallocated gap without ever going
through a freeblock (common for the most-recently-inserted row on a page),
and whole leaf pages that landed on the freelist before being reused.
"""

from __future__ import annotations

import sqlite3
import struct
from pathlib import Path

_HEADER_SIZE = 100
_MAX_ROWID = 2**63 - 1

_SERIAL_FIXED_SIZE = {0: 0, 1: 1, 2: 2, 3: 3, 4: 4, 5: 6, 6: 8, 7: 8, 8: 0, 9: 0}


def _serial_type_size(serial_type: int) -> int:
    if serial_type in _SERIAL_FIXED_SIZE:
        return _SERIAL_FIXED_SIZE[serial_type]
    if serial_type < 12:
        raise ValueError(f"reserved serial type {serial_type}")
    return (serial_type - 12) // 2 if serial_type % 2 == 0 else (serial_type - 13) // 2


def _decode_value(data: bytes, offset: int, serial_type: int):
    size = _serial_type_size(serial_type)
    if serial_type == 0:
        return None
    if serial_type == 8:
        return 0
    if serial_type == 9:
        return 1
    raw = data[offset : offset + size]
    if serial_type <= 6:
        return int.from_bytes(raw, "big", signed=True)
    if serial_type == 7:
        return struct.unpack(">d", raw)[0]
    if serial_type % 2 == 0:
        return bytes(raw)
    return raw.decode("utf-8", errors="replace")


def _read_varint(data: bytes, offset: int) -> tuple[int, int]:
    value = 0
    for i in range(9):
        if offset + i >= len(data):
            raise ValueError("varint runs past buffer")
        b = data[offset + i]
        if i == 8:
            return (value << 8) | b, offset + 9
        value = (value << 7) | (b & 0x7F)
        if not (b & 0x80):
            return value, offset + i + 1
    raise ValueError("unreachable")  # pragma: no cover


def _try_parse_cell(
    page: bytes, offset: int, page_size: int
) -> tuple[int, list, int] | None:
    """Parse a table-leaf cell at `offset`: [payload_len varint][rowid varint][record].

    Returns (rowid, column_values, bytes_consumed) or None. Rejects anything
    needing an overflow page, and any record whose header/body arithmetic
    doesn't add up exactly — that arithmetic check is what keeps false
    positives from random bytes low.
    """
    try:
        payload_len, o = _read_varint(page, offset)
        if not (0 < payload_len < page_size):
            return None
        rowid, o = _read_varint(page, o)
        if not (0 < rowid <= _MAX_ROWID):
            return None
        body_start = o
        if body_start + payload_len > page_size:
            return None  # would spill into an overflow page
        header_len, ho = _read_varint(page, body_start)
        if header_len <= 0 or body_start + header_len > page_size:
            return None
        serial_types = []
        pos = ho
        header_end = body_start + header_len
        while pos < header_end:
            st, pos = _read_varint(page, pos)
            serial_types.append(st)
        if pos != header_end:
            return None
        data_pos = header_end
        payload_end = body_start + payload_len
        values = []
        for st in serial_types:
            size = _serial_type_size(st)
            if data_pos + size > payload_end:
                return None
            values.append(_decode_value(page, data_pos, st))
            data_pos += size
        if data_pos != payload_end:
            return None
        return rowid, values, payload_end - offset
    except (ValueError, IndexError, struct.error):
        return None


def _leaf_scan_regions(page: bytes, page_size: int, header_off: int) -> list[tuple[int, int]]:
    """(start, end) byte ranges worth scanning: the unallocated gap, plus every
    freeblock's body. Empty if `page` isn't a table-leaf b-tree page (0x0D)."""
    if len(page) <= header_off or page[header_off] != 0x0D:
        return []
    first_freeblock = struct.unpack(">H", page[header_off + 1 : header_off + 3])[0]
    num_cells = struct.unpack(">H", page[header_off + 3 : header_off + 5])[0]
    cell_content_start = struct.unpack(">H", page[header_off + 5 : header_off + 7])[0]
    if cell_content_start == 0:
        cell_content_start = 65536
    ptr_array_end = header_off + 8 + num_cells * 2
    regions = []
    if ptr_array_end < cell_content_start <= page_size:
        regions.append((ptr_array_end, cell_content_start))
    fb = first_freeblock
    seen: set[int] = set()
    while fb != 0 and fb not in seen and fb + 4 <= page_size:
        seen.add(fb)
        next_fb, size = struct.unpack(">HH", page[fb : fb + 4])
        if size >= 4:
            regions.append((fb, min(fb + size, page_size)))
        fb = next_fb
    return regions


class _PageStore:
    def __init__(self, f, page_size: int, page_count: int) -> None:
        self._f = f
        self.page_size = page_size
        self.page_count = page_count

    def read(self, page_no: int) -> bytes:
        self._f.seek((page_no - 1) * self.page_size)
        return self._f.read(self.page_size)


def _read_layout(f) -> tuple[int, int, int]:
    f.seek(0)
    header = f.read(_HEADER_SIZE)
    page_size = struct.unpack(">H", header[16:18])[0]
    if page_size == 1:
        page_size = 65536
    page_count = struct.unpack(">I", header[28:32])[0]
    first_trunk = struct.unpack(">I", header[32:36])[0]
    return page_size, page_count, first_trunk


def _scan_page_for_candidates(page: bytes, page_size: int, offset: int, end: int, out: list):
    while offset < end:
        parsed = _try_parse_cell(page, offset, page_size)
        if parsed is None:
            offset += 1
            continue
        rowid, values, consumed = parsed
        out.append((rowid, values))
        offset += max(consumed, 1)


def _type_family(decl_type: str | None) -> str:
    t = (decl_type or "").upper()
    if "INT" in t:
        return "INTEGER"
    if any(k in t for k in ("CHAR", "CLOB", "TEXT")):
        return "TEXT"
    if any(k in t for k in ("REAL", "FLOA", "DOUB")):
        return "REAL"
    if "BLOB" in t or not t:
        return "BLOB"
    return "NUMERIC"


def _score(values: list, families: list[str]) -> float:
    considered = min(len(values), len(families))
    if considered == 0:
        return 0.0
    matched = 0
    for value, family in zip(values[:considered], families[:considered]):
        if value is None:
            matched += 1  # any column can legitimately be NULL
        elif family == "INTEGER":
            matched += isinstance(value, int)
        elif family == "TEXT":
            matched += isinstance(value, str)
        elif family == "REAL":
            matched += isinstance(value, (int, float))
        elif family == "BLOB":
            matched += isinstance(value, (bytes, bytearray))
        else:  # NUMERIC
            matched += isinstance(value, (int, float, str))
    return matched / considered


def recover_records(db: Path, table: str, min_match: float = 0.8) -> list[tuple]:
    """Scan `db` for deleted rows that look like they belonged to `table`.

    Uses the table's *current* schema (PRAGMA table_info) purely to score
    candidates by column-type-family plausibility — deleted rows aren't
    assumed to still exist anywhere in the live schema. Returns column-value
    tuples aligned to that schema (shorter records are right-padded with
    None, matching SQLite's own "missing trailing columns are NULL" rule).
    """
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        table_info = conn.execute(f"PRAGMA table_info('{table}')").fetchall()
    finally:
        conn.close()
    if not table_info:
        raise ValueError(f"no such table: {table}")
    families = [_type_family(row[2]) for row in table_info]
    ncols = len(families)

    candidates: list[tuple[int, list]] = []
    with open(db, "rb") as f:
        page_size, page_count, first_trunk = _read_layout(f)
        store = _PageStore(f, page_size, page_count)

        for page_no in range(1, page_count + 1):
            page = store.read(page_no)
            if len(page) < page_size:
                continue
            header_off = _HEADER_SIZE if page_no == 1 else 0
            for start, end in _leaf_scan_regions(page, page_size, header_off):
                _scan_page_for_candidates(page, page_size, start, end, candidates)

        trunk = first_trunk
        visited: set[int] = set()
        while trunk and trunk not in visited and 1 <= trunk <= page_count:
            visited.add(trunk)
            tpage = store.read(trunk)
            if len(tpage) < 8:
                break
            next_trunk, leaf_count = struct.unpack(">II", tpage[0:8])
            for i in range(leaf_count):
                off = 8 + i * 4
                if off + 4 > len(tpage):
                    break
                (leaf_no,) = struct.unpack(">I", tpage[off : off + 4])
                if not (1 <= leaf_no <= page_count):
                    continue
                leaf = store.read(leaf_no)
                # Freelist leaf pages carry no page-type byte of their own —
                # whatever table they last held, their whole body is fair game.
                _scan_page_for_candidates(leaf, page_size, 0, len(leaf), candidates)
            trunk = next_trunk

    results: list[tuple] = []
    seen: set[tuple] = set()
    for rowid, values in candidates:
        score = _score(values, families)
        if score < min_match:
            continue
        row = tuple(values[:ncols]) + (None,) * max(0, ncols - len(values))
        dedupe_key = (rowid, row)
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        results.append(row)
    return results
