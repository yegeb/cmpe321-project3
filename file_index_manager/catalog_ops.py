"""
System catalog read/write helpers.

The catalog is stored in CATALOG_FILE_ID (_catalog.db).
Each page holds up to N entries (N = floor((page_size - 16) / 320)).
The standard page header's slot_bitmap tracks which slots are occupied.

Catalog entry layout (320 bytes):
  type_name  : 16s  (null-padded ASCII)
  num_fields : B
  pk_order   : B    (1-indexed)
  reserved   : 2x
  fields[12] : 12 × (field_name 20s + field_type B + pad 4x)  = 12 × 25B = 300B
"""

import struct
from typing import Optional

from shared.constants import (
    HEADER_SIZE,
    PAGE_TYPE_CATALOG,
    CATALOG_FILE_ID,
    CATALOG_TYPE_NAME_SIZE, CATALOG_FIELD_NAME_SIZE,
    CATALOG_MAX_FIELDS, CATALOG_ENTRY_SIZE,
    CATALOG_FIELD_TYPE_INT, CATALOG_FIELD_TYPE_STR,
)
from shared.results import FieldInfo, TypeInfo
from .page_utils import (
    pack_header, unpack_header,
    slot_is_set, set_slot, find_free_slot,
    make_page,
)

# ─── Catalog entry struct formats ─────────────────────────────────────────────

_ENTRY_HEADER_FMT  = f'={CATALOG_TYPE_NAME_SIZE}sBB2x'   # 20 bytes
_ENTRY_HEADER_SIZE = struct.calcsize(_ENTRY_HEADER_FMT)   # == 20

_FIELD_FMT  = f'={CATALOG_FIELD_NAME_SIZE}sB4x'           # 25 bytes
_FIELD_SIZE = struct.calcsize(_FIELD_FMT)                  # == 25

assert _ENTRY_HEADER_SIZE + CATALOG_MAX_FIELDS * _FIELD_SIZE == CATALOG_ENTRY_SIZE


def _entries_per_page(page_size: int) -> int:
    return (page_size - HEADER_SIZE) // CATALOG_ENTRY_SIZE


def _entry_offset(slot: int) -> int:
    return HEADER_SIZE + slot * CATALOG_ENTRY_SIZE


# ─── Serialization ────────────────────────────────────────────────────────────

def pack_type_entry(ti: TypeInfo) -> bytes:
    """Serialize TypeInfo → exactly CATALOG_ENTRY_SIZE bytes."""
    header = struct.pack(
        _ENTRY_HEADER_FMT,
        ti.name.encode('ascii'),
        len(ti.fields),
        ti.primary_key_order,
    )
    fields_bytes = bytearray()
    for i in range(CATALOG_MAX_FIELDS):
        if i < len(ti.fields):
            f = ti.fields[i]
            ftype = CATALOG_FIELD_TYPE_INT if f.type == "int" else CATALOG_FIELD_TYPE_STR
            fields_bytes += struct.pack(_FIELD_FMT, f.name.encode('ascii'), ftype)
        else:
            fields_bytes += b'\x00' * _FIELD_SIZE
    entry = header + bytes(fields_bytes)
    assert len(entry) == CATALOG_ENTRY_SIZE
    return entry


def unpack_type_entry(data, offset: int = 0) -> Optional[TypeInfo]:
    """Parse a catalog entry at offset. Returns None for empty/zeroed slots."""
    header_bytes = data[offset: offset + _ENTRY_HEADER_SIZE]
    name_raw, num_fields, pk_order = struct.unpack(_ENTRY_HEADER_FMT, header_bytes)
    name = name_raw.rstrip(b'\x00').decode('ascii')
    if not name:
        return None

    fields = []
    field_off = offset + _ENTRY_HEADER_SIZE
    for i in range(num_fields):
        field_bytes = data[field_off: field_off + _FIELD_SIZE]
        fname_raw, ftype = struct.unpack(_FIELD_FMT, field_bytes)
        fname = fname_raw.rstrip(b'\x00').decode('ascii')
        ftype_str = "int" if ftype == CATALOG_FIELD_TYPE_INT else "str"
        fields.append(FieldInfo(name=fname, type=ftype_str, order=i + 1))
        field_off += _FIELD_SIZE

    return TypeInfo(name=name, fields=fields, primary_key_order=pk_order)


# ─── Catalog I/O ─────────────────────────────────────────────────────────────

def load_catalog(buffer, page_size: int) -> dict:
    """
    Read every catalog page and return {type_name: TypeInfo}.
    Stops at the first page that cannot be fetched (file empty / end of file).
    """
    cache: dict = {}
    n_per_page = _entries_per_page(page_size)
    page_id = 0
    while True:
        result = buffer.get_page(CATALOG_FILE_ID, page_id)
        if result.status != "success":
            break
        data = result.data
        _, _, slot_bitmap, _ = unpack_header(data)
        for slot in range(n_per_page):
            if slot_is_set(slot_bitmap, slot):
                ti = unpack_type_entry(data, _entry_offset(slot))
                if ti:
                    cache[ti.name] = ti
        page_id += 1
    return cache


def save_type_to_catalog(buffer, page_size: int, ti: TypeInfo) -> bool:
    """
    Write ti into the first free catalog slot.
    Allocates a new catalog page if all existing pages are full.
    Returns True on success.
    """
    n_per_page = _entries_per_page(page_size)
    page_id = 0

    while True:
        result = buffer.get_page(CATALOG_FILE_ID, page_id)

        if result.status != "success":
            # Catalog page doesn't exist yet — allocate it.
            new_res = buffer.new_page(CATALOG_FILE_ID)
            if new_res.status != "success":
                return False
            new_pid = new_res.page_id
            page = make_page(new_pid, PAGE_TYPE_CATALOG, page_size)
            buffer.write_page(CATALOG_FILE_ID, new_pid, bytes(page))
            # Write entry into slot 0 of this brand-new page.
            page_data = bytearray(page)
            off = _entry_offset(0)
            page_data[off: off + CATALOG_ENTRY_SIZE] = pack_type_entry(ti)
            bitmap = set_slot(0, 0)
            page_data[:HEADER_SIZE] = pack_header(new_pid, 1, bitmap, PAGE_TYPE_CATALOG)
            buffer.write_page(CATALOG_FILE_ID, new_pid, bytes(page_data))
            return True

        data = bytearray(result.data)
        _, num_records, slot_bitmap, _ = unpack_header(data)
        slot = find_free_slot(slot_bitmap, n_per_page)

        if slot == -1:
            page_id += 1
            continue

        off = _entry_offset(slot)
        data[off: off + CATALOG_ENTRY_SIZE] = pack_type_entry(ti)
        slot_bitmap = set_slot(slot_bitmap, slot)
        num_records += 1
        data[:HEADER_SIZE] = pack_header(page_id, num_records, slot_bitmap, PAGE_TYPE_CATALOG)
        buffer.write_page(CATALOG_FILE_ID, page_id, bytes(data))
        return True
