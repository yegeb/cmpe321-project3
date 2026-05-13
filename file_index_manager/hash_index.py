"""
Static hash index on the primary key field.

File layout  (<type_name>_hash.db):
  Page 0      — directory page
  Pages 1..64 — one bucket page per hash slot (no overflow at init)

Directory page (after 16B header):
  num_buckets : 4B 'I'
  bucket_ids  : num_buckets × 4B 'I'   (page_id of each bucket)

Bucket page (after 16B header):
  num_entries : 2B 'H'
  next_bucket : 4B 'I'   overflow chain pointer; NULL_PAGE_ID = no overflow
  reserved    : 2B
  entries[]   : (key_bytes + rid_bytes) × num_entries

Key is always the primary key field of the relation.
Equality lookup only; range queries fall back to heap scan.
"""

import struct
from shared.constants import (
    HEADER_SIZE,
    PAGE_TYPE_HASH_DIR, PAGE_TYPE_HASH_BUCKET,
    HASH_DIR_EXTRA_HEADER_FORMAT,
    HASH_BUCKET_EXTRA_HEADER_FORMAT, HASH_BUCKET_EXTRA_HEADER_SIZE,
    NUM_HASH_BUCKETS, NULL_PAGE_ID,
    RID_SIZE,
)
from .page_utils import (
    make_page,
    pack_key, unpack_key, key_size_for,
    pack_rid, unpack_rid,
)

# Offsets inside a bucket page
_BUCKET_ENTRIES_OFFSET = HEADER_SIZE + HASH_BUCKET_EXTRA_HEADER_SIZE   # 24
_DIR_ENTRIES_OFFSET    = HEADER_SIZE + struct.calcsize(HASH_DIR_EXTRA_HEADER_FORMAT)   # 20


def _hash_key(key, num_buckets: int) -> int:
    """Map a primary key value to a bucket index."""
    return hash(key) % num_buckets


def _max_bucket_entries(page_size: int, ks: int) -> int:
    return (page_size - _BUCKET_ENTRIES_OFFSET) // (ks + RID_SIZE)


# ─── Initialization ───────────────────────────────────────────────────────────

def hash_init(buffer, file_id: str, page_size: int) -> None:
    """
    Create the directory page + NUM_HASH_BUCKETS bucket pages.
    Called once when a new type is created with hash_index strategy.
    """
    # Allocate directory page (will be page 0)
    dir_res = buffer.new_page(file_id)
    assert dir_res.page_id == 0, "Directory page must be page 0"

    # Allocate bucket pages 1..NUM_HASH_BUCKETS
    bucket_page_ids = []
    for _ in range(NUM_HASH_BUCKETS):
        bkt_res = buffer.new_page(file_id)
        bucket_page_ids.append(bkt_res.page_id)
        bkt_page = make_page(bkt_res.page_id, PAGE_TYPE_HASH_BUCKET, page_size)
        # Write bucket header: num_entries=0, next_bucket=NULL_PAGE_ID
        struct.pack_into(HASH_BUCKET_EXTRA_HEADER_FORMAT, bkt_page, HEADER_SIZE,
                         0, NULL_PAGE_ID)
        buffer.write_page(file_id, bkt_res.page_id, bytes(bkt_page))

    # Build directory page
    dir_page = make_page(0, PAGE_TYPE_HASH_DIR, page_size)
    struct.pack_into(HASH_DIR_EXTRA_HEADER_FORMAT, dir_page, HEADER_SIZE, NUM_HASH_BUCKETS)
    for i, bpid in enumerate(bucket_page_ids):
        struct.pack_into('=I', dir_page, _DIR_ENTRIES_OFFSET + i * 4, bpid)
    buffer.write_page(file_id, 0, bytes(dir_page))


# ─── Directory helpers ────────────────────────────────────────────────────────

def _get_bucket_page_id(buffer, file_id: str, bucket_idx: int) -> int:
    """Read the directory to find which page_id holds bucket_idx."""
    dir_res = buffer.get_page(file_id, 0)
    data = dir_res.data
    return struct.unpack_from('=I', data, _DIR_ENTRIES_OFFSET + bucket_idx * 4)[0]


# ─── Lookup ───────────────────────────────────────────────────────────────────

def hash_search(buffer, file_id: str, pk_value, pk_type: str,
                page_size: int):
    """
    Look up pk_value in the hash index.

    Returns ((data_page_id, slot_no), nodes_visited) if found,
    or (None, nodes_visited) if not found.
    nodes_visited counts the bucket/overflow pages examined.
    """
    ks = key_size_for(pk_type)
    bucket_idx = _hash_key(pk_value, NUM_HASH_BUCKETS)
    page_id = _get_bucket_page_id(buffer, file_id, bucket_idx)
    nodes_visited = 1   # directory read counted separately; start bucket chain

    while page_id != NULL_PAGE_ID:
        result = buffer.get_page(file_id, page_id)
        if result.status != "success":
            break
        nodes_visited += 1
        data = result.data
        num_entries, next_bucket = struct.unpack_from(
            HASH_BUCKET_EXTRA_HEADER_FORMAT, data, HEADER_SIZE)

        for i in range(num_entries):
            off = _BUCKET_ENTRIES_OFFSET + i * (ks + RID_SIZE)
            k = unpack_key(data, pk_type, off)
            if k == pk_value:
                rid = unpack_rid(data, off + ks)
                return rid, nodes_visited

        page_id = next_bucket

    return None, nodes_visited


# ─── Insert ───────────────────────────────────────────────────────────────────

def hash_insert(buffer, file_id: str, pk_value, rid: tuple,
                pk_type: str, page_size: int) -> int:
    """
    Insert pk_value → rid into the hash index.
    Allocates overflow pages when a bucket is full.
    Returns nodes_visited.
    """
    ks = key_size_for(pk_type)
    max_entries = _max_bucket_entries(page_size, ks)
    bucket_idx = _hash_key(pk_value, NUM_HASH_BUCKETS)
    page_id = _get_bucket_page_id(buffer, file_id, bucket_idx)
    nodes_visited = 1

    # Walk the overflow chain to find a page with space (or the last page)
    prev_page_id = NULL_PAGE_ID
    while page_id != NULL_PAGE_ID:
        result = buffer.get_page(file_id, page_id)
        if result.status != "success":
            break
        nodes_visited += 1
        data = bytearray(result.data)
        num_entries, next_bucket = struct.unpack_from(
            HASH_BUCKET_EXTRA_HEADER_FORMAT, data, HEADER_SIZE)

        if num_entries < max_entries:
            # Insert here
            off = _BUCKET_ENTRIES_OFFSET + num_entries * (ks + RID_SIZE)
            data[off: off + ks] = pack_key(pk_value, pk_type)
            data[off + ks: off + ks + RID_SIZE] = pack_rid(*rid)
            num_entries += 1
            struct.pack_into(HASH_BUCKET_EXTRA_HEADER_FORMAT, data, HEADER_SIZE,
                             num_entries, next_bucket)
            buffer.write_page(file_id, page_id, bytes(data))
            return nodes_visited

        prev_page_id = page_id
        page_id = next_bucket

    # All pages in chain are full — allocate overflow page
    ovf_res = buffer.new_page(file_id)
    ovf_pid = ovf_res.page_id
    ovf_page = make_page(ovf_pid, PAGE_TYPE_HASH_BUCKET, page_size)
    # Write the new entry
    off = _BUCKET_ENTRIES_OFFSET
    ovf_page[off: off + ks] = pack_key(pk_value, pk_type)
    ovf_page[off + ks: off + ks + RID_SIZE] = pack_rid(*rid)
    struct.pack_into(HASH_BUCKET_EXTRA_HEADER_FORMAT, ovf_page, HEADER_SIZE,
                     1, NULL_PAGE_ID)
    buffer.write_page(file_id, ovf_pid, bytes(ovf_page))

    # Link previous tail to new overflow page
    if prev_page_id != NULL_PAGE_ID:
        prev_res = buffer.get_page(file_id, prev_page_id)
        prev_data = bytearray(prev_res.data)
        ne, _ = struct.unpack_from(HASH_BUCKET_EXTRA_HEADER_FORMAT, prev_data, HEADER_SIZE)
        struct.pack_into(HASH_BUCKET_EXTRA_HEADER_FORMAT, prev_data, HEADER_SIZE, ne, ovf_pid)
        buffer.write_page(file_id, prev_page_id, bytes(prev_data))

    return nodes_visited + 1


# ─── Delete ───────────────────────────────────────────────────────────────────

def hash_delete(buffer, file_id: str, pk_value, pk_type: str,
                page_size: int) -> int:
    """
    Remove pk_value from the hash index.
    Uses swap-with-last to fill the gap (no compaction of overflow pages).
    Returns nodes_visited.
    """
    ks = key_size_for(pk_type)
    bucket_idx = _hash_key(pk_value, NUM_HASH_BUCKETS)
    page_id = _get_bucket_page_id(buffer, file_id, bucket_idx)
    nodes_visited = 1

    while page_id != NULL_PAGE_ID:
        result = buffer.get_page(file_id, page_id)
        if result.status != "success":
            break
        nodes_visited += 1
        data = bytearray(result.data)
        num_entries, next_bucket = struct.unpack_from(
            HASH_BUCKET_EXTRA_HEADER_FORMAT, data, HEADER_SIZE)

        for i in range(num_entries):
            off = _BUCKET_ENTRIES_OFFSET + i * (ks + RID_SIZE)
            k = unpack_key(data, pk_type, off)
            if k == pk_value:
                # Swap with last entry to fill gap
                last_off = _BUCKET_ENTRIES_OFFSET + (num_entries - 1) * (ks + RID_SIZE)
                if i != num_entries - 1:
                    data[off: off + ks + RID_SIZE] = data[last_off: last_off + ks + RID_SIZE]
                # Zero out last slot
                data[last_off: last_off + ks + RID_SIZE] = b'\x00' * (ks + RID_SIZE)
                num_entries -= 1
                struct.pack_into(HASH_BUCKET_EXTRA_HEADER_FORMAT, data, HEADER_SIZE,
                                 num_entries, next_bucket)
                buffer.write_page(file_id, page_id, bytes(data))
                return nodes_visited

        page_id = next_bucket

    return nodes_visited
