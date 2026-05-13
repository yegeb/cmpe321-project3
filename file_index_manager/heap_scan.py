"""
Heap scan implementations for FileIndexManager.

All three DML operations (search, range, delete) have a fallback
heap scan path that is used when:
  - index_strategy == "heap_scan"
  - hash_index is asked for a range query
  - bplus_tree is asked for range on a non-primary-key field
"""

from shared.constants import HEADER_SIZE, PAGE_TYPE_DATA
from .page_utils import (
    unpack_header, pack_header,
    slot_is_set, clear_slot,
    record_size, unpack_record,
    find_free_slot,
    make_page,
)


def heap_search(buffer, type_name: str, pk_value, pk_field_order: int, fields,
                page_size: int):
    """
    Sequential scan for a record matching pk_value on the primary key field.

    Returns (records_list, pages_accessed, records_scanned) where
    records_list is either a single-element list [[v1, v2, ...]] or [].
    pk_field_order is 1-indexed.
    """
    rec_size = record_size(fields)
    pk_idx = pk_field_order - 1    # 0-indexed
    pages_accessed = 0
    records_scanned = 0
    page_id = 0

    while True:
        result = buffer.get_page(type_name, page_id)
        if result.status != "success":
            break
        pages_accessed += 1
        data = result.data
        _, _, slot_bitmap, _ = unpack_header(data)

        for slot in range(10):   # max 10 slots per data page
            if not slot_is_set(slot_bitmap, slot):
                continue
            records_scanned += 1
            offset = HEADER_SIZE + slot * rec_size
            values = unpack_record(data, fields, offset)
            if values[pk_idx] == pk_value:
                return [values], pages_accessed, records_scanned

        page_id += 1

    return [], pages_accessed, records_scanned


def heap_range(buffer, type_name: str, field_name: str, low, high, fields,
               page_size: int):
    """
    Sequential scan returning all records where field_name value is in [low, high].
    field_name must be an int field (enforced by caller).

    Returns (records_list, pages_accessed, records_scanned).
    Records are returned in ascending order of field_name value.
    """
    field_idx = next(i for i, f in enumerate(fields) if f.name == field_name)
    rec_size = record_size(fields)
    pages_accessed = 0
    records_scanned = 0
    matched = []
    page_id = 0

    while True:
        result = buffer.get_page(type_name, page_id)
        if result.status != "success":
            break
        pages_accessed += 1
        data = result.data
        _, _, slot_bitmap, _ = unpack_header(data)

        for slot in range(10):
            if not slot_is_set(slot_bitmap, slot):
                continue
            records_scanned += 1
            offset = HEADER_SIZE + slot * rec_size
            values = unpack_record(data, fields, offset)
            val = values[field_idx]
            if low <= val <= high:
                matched.append(values)

        page_id += 1

    matched.sort(key=lambda row: row[field_idx])
    return matched, pages_accessed, records_scanned


def heap_delete(buffer, type_name: str, pk_value, pk_field_order: int, fields,
                max_records_per_page: int, page_size: int):
    """
    Sequential scan to find and delete the record with the given primary key.

    Returns (found: bool, page_id: int, slot_no: int, pages_accessed: int, records_scanned: int).
    page_id and slot_no are the location of the deleted record (useful for index cleanup).
    """
    rec_size = record_size(fields)
    pk_idx = pk_field_order - 1
    pages_accessed = 0
    records_scanned = 0
    page_id = 0

    while True:
        result = buffer.get_page(type_name, page_id)
        if result.status != "success":
            break
        pages_accessed += 1
        data = bytearray(result.data)
        _, num_records, slot_bitmap, _ = unpack_header(data)

        found_slot = -1
        for slot in range(max_records_per_page):
            if not slot_is_set(slot_bitmap, slot):
                continue
            records_scanned += 1
            offset = HEADER_SIZE + slot * rec_size
            values = unpack_record(data, fields, offset)
            if values[pk_idx] == pk_value:
                found_slot = slot
                break

        if found_slot != -1:
            # Zero out the slot bytes
            offset = HEADER_SIZE + found_slot * rec_size
            data[offset: offset + rec_size] = b'\x00' * rec_size
            # Update bitmap and count
            slot_bitmap = clear_slot(slot_bitmap, found_slot)
            num_records -= 1
            data[:HEADER_SIZE] = pack_header(page_id, num_records, slot_bitmap, PAGE_TYPE_DATA)
            buffer.write_page(type_name, page_id, bytes(data))
            return True, page_id, found_slot, pages_accessed, records_scanned

        page_id += 1

    return False, -1, -1, pages_accessed, records_scanned


def heap_find_free_slot(buffer, type_name: str, max_records_per_page: int,
                        page_size: int):
    """
    Find the first data page that has a free slot.
    If all pages are full, allocate a new page.

    Returns (page_id, slot_no, data_bytearray, pages_accessed).
    Caller must write the record and call buffer.write_page().
    """
    pages_accessed = 0
    page_id = 0

    while True:
        result = buffer.get_page(type_name, page_id)
        if result.status != "success":
            # No more pages — allocate a new one.
            new_res = buffer.new_page(type_name)
            if new_res.status != "success":
                return -1, -1, None, pages_accessed
            new_pid = new_res.page_id
            page = make_page(new_pid, PAGE_TYPE_DATA, page_size)
            buffer.write_page(type_name, new_pid, bytes(page))
            return new_pid, 0, bytearray(page), pages_accessed

        pages_accessed += 1
        data = bytearray(result.data)
        _, _, slot_bitmap, _ = unpack_header(data)
        slot = find_free_slot(slot_bitmap, max_records_per_page)
        if slot != -1:
            return page_id, slot, data, pages_accessed
        page_id += 1


def heap_count_pages(buffer, type_name: str) -> int:
    """Return number of data pages currently in type_name's file."""
    count = 0
    while True:
        result = buffer.get_page(type_name, count)
        if result.status != "success":
            break
        count += 1
    return count


def heap_pk_exists(buffer, type_name: str, pk_value, pk_field_order: int,
                   fields, page_size: int) -> bool:
    """Quick duplicate-key check via heap scan."""
    found, _, _ = heap_search(buffer, type_name, pk_value, pk_field_order, fields, page_size)
    return len(found) > 0
