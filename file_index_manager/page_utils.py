"""
Low-level page helper functions used across FileIndexManager.

No I/O happens here — all bytes manipulation only.
All I/O goes through BufferManager.
"""

import struct
from shared.constants import (
    HEADER_FORMAT, HEADER_SIZE,
    INT_SIZE, STR_SIZE,
    BPLUS_INTERNAL_EXTRA_HEADER_SIZE, BPLUS_LEAF_EXTRA_HEADER_SIZE,
    RID_FORMAT, RID_SIZE,
)


def _validate_field_type(field_type: str) -> None:
    if field_type not in {"int", "str"}:
        raise ValueError(f"Unsupported field type: {field_type}")


def _pack_fixed_string(value, width: int) -> bytes:
    raw = str(value).encode("ascii")
    if len(raw) > width:
        raise ValueError(f"String value exceeds fixed width {width}: {value}")
    return struct.pack(f"={width}s", raw)


# ─── Page header ─────────────────────────────────────────────────────────────

def pack_header(page_no: int, num_records: int, slot_bitmap: int, page_type: int) -> bytes:
    return struct.pack(HEADER_FORMAT, page_no, num_records, slot_bitmap, page_type)


def unpack_header(data):
    """Returns (page_no, num_records, slot_bitmap, page_type)."""
    return struct.unpack_from(HEADER_FORMAT, data, 0)


def make_page(page_no: int, page_type: int, page_size: int) -> bytearray:
    """Return a zeroed page of page_size bytes with the standard header set."""
    page = bytearray(page_size)
    page[:HEADER_SIZE] = pack_header(page_no, 0, 0, page_type)
    return page


# ─── Slot bitmap ─────────────────────────────────────────────────────────────

def slot_is_set(bitmap: int, slot: int) -> bool:
    return bool(bitmap & (1 << slot))


def set_slot(bitmap: int, slot: int) -> int:
    return bitmap | (1 << slot)


def clear_slot(bitmap: int, slot: int) -> int:
    return bitmap & ~(1 << slot)


def find_free_slot(bitmap: int, max_slots: int) -> int:
    """Return index of first free (0) slot, or -1 if all occupied."""
    for i in range(max_slots):
        if not slot_is_set(bitmap, i):
            return i
    return -1


# ─── Record pack / unpack ─────────────────────────────────────────────────────

def record_size(fields) -> int:
    """Return fixed byte size of one record given its FieldInfo list."""
    total = 0
    for field in fields:
        _validate_field_type(field.type)
        total += INT_SIZE if field.type == "int" else STR_SIZE
    return total


def pack_record(values, fields) -> bytes:
    """Serialize a list of Python values to raw record bytes."""
    if len(values) != len(fields):
        raise ValueError(
            f"Record value count mismatch: expected {len(fields)}, got {len(values)}"
        )

    parts = []
    for val, field in zip(values, fields):
        _validate_field_type(field.type)
        if field.type == "int":
            parts.append(struct.pack("=i", int(val)))
        else:
            parts.append(_pack_fixed_string(val, STR_SIZE))
    return b"".join(parts)


def unpack_record(data, fields, offset: int = 0) -> list:
    """Deserialize raw bytes at offset into a list of Python values."""
    values = []
    pos = offset
    for field in fields:
        _validate_field_type(field.type)
        if field.type == "int":
            val = struct.unpack_from("=i", data, pos)[0]
            pos += INT_SIZE
        else:
            raw = struct.unpack_from(f"={STR_SIZE}s", data, pos)[0]
            val = raw.rstrip(b"\x00").decode("ascii")
            pos += STR_SIZE
        values.append(val)
    return values


# ─── B+ tree key helpers ──────────────────────────────────────────────────────

def key_size_for(pk_type: str) -> int:
    _validate_field_type(pk_type)
    return INT_SIZE if pk_type == "int" else STR_SIZE


def pack_key(key, pk_type: str) -> bytes:
    _validate_field_type(pk_type)
    if pk_type == "int":
        return struct.pack("=i", int(key))
    return _pack_fixed_string(key, STR_SIZE)


def unpack_key(data, pk_type: str, offset: int = 0):
    _validate_field_type(pk_type)
    if pk_type == "int":
        return struct.unpack_from("=i", data, offset)[0]
    raw = struct.unpack_from(f"={STR_SIZE}s", data, offset)[0]
    return raw.rstrip(b"\x00").decode("ascii")


def compare_keys(a, b, pk_type: str) -> int:
    """Return negative/0/positive like cmp(a, b)."""
    _validate_field_type(pk_type)
    if a < b:
        return -1
    if a > b:
        return 1
    return 0


# ─── RID helpers ─────────────────────────────────────────────────────────────

def pack_rid(page_id: int, slot_no: int) -> bytes:
    return struct.pack(RID_FORMAT, page_id, slot_no)


def unpack_rid(data, offset: int = 0):
    """Returns (page_id, slot_no)."""
    return struct.unpack_from(RID_FORMAT, data, offset)


# ─── B+ tree layout offsets ───────────────────────────────────────────────────
#
# Internal node content starts at HEADER_SIZE + BPLUS_INTERNAL_EXTRA_HEADER_SIZE = 20
# Layout used by this codebase: [child0][key0][child1][key1]...[childN]
#   child[i] at: INTERNAL_DATA + i * (4 + key_size)
#   key[i]   at: INTERNAL_DATA + i * (4 + key_size) + 4
#
# Leaf node content starts at HEADER_SIZE + BPLUS_LEAF_EXTRA_HEADER_SIZE = 24
# Entry[i]: at LEAF_DATA + i * (key_size + RID_SIZE)
#   key bytes first, then 5-byte RID

INTERNAL_DATA_OFFSET = HEADER_SIZE + BPLUS_INTERNAL_EXTRA_HEADER_SIZE   # 20
LEAF_DATA_OFFSET     = HEADER_SIZE + BPLUS_LEAF_EXTRA_HEADER_SIZE        # 24


def internal_child_offset(i: int, ks: int) -> int:
    return INTERNAL_DATA_OFFSET + i * (4 + ks)


def internal_key_offset(i: int, ks: int) -> int:
    return INTERNAL_DATA_OFFSET + i * (4 + ks) + 4


def leaf_entry_offset(i: int, ks: int) -> int:
    return LEAF_DATA_OFFSET + i * (ks + RID_SIZE)


def max_internal_keys(page_size: int, ks: int) -> int:
    """Maximum number of separator keys in an internal node."""
    available = page_size - INTERNAL_DATA_OFFSET - 4   # minus one child slot
    return available // (ks + 4)


def max_leaf_entries(page_size: int, ks: int) -> int:
    """Maximum (key, RID) pairs in a leaf node."""
    return (page_size - LEAF_DATA_OFFSET) // (ks + RID_SIZE)
