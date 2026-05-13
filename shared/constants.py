"""
Page layout constants shared across all layers.

DiskSpaceManager and BufferManager treat page bytes as opaque blobs —
they do NOT need to parse page content. These constants are primarily
used by FileIndexManager when it reads/writes page content.

All multi-byte integers are stored little-endian ('=' prefix in struct format).

Page size comes from config["page_size"] (default 4096). These constants
describe the layout within a page, independent of page_size.
"""

import struct

# ─── Field byte widths (for data records) ─────────────────────────────────────

INT_SIZE = 4    # struct 'i' – signed 32-bit integer
STR_SIZE = 32   # struct '32s' – null-padded ASCII, effective max 31 printable chars

# ─── Universal page header (16 bytes, every page starts with this) ────────────
#
#  Offset  0 │ page_no     │ 4B │ 'I' │ page number within its file (0-indexed)
#  Offset  4 │ num_records │ 2B │ 'H' │ occupied slot / entry count
#  Offset  6 │ slot_bitmap │ 2B │ 'H' │ bit i=1 → slot i is occupied (bits 0-9)
#  Offset  8 │ page_type   │ 1B │ 'B' │ see PAGE_TYPE_* below
#  Offset  9 │ reserved    │ 7B │     │ must be zero
#
HEADER_FORMAT = '=IHHBxxxxxxx'           # 4+2+2+1+7 = 16 bytes
HEADER_SIZE   = struct.calcsize(HEADER_FORMAT)   # == 16

assert HEADER_SIZE == 16, "Header must be exactly 16 bytes"

# ─── page_type values ─────────────────────────────────────────────────────────

PAGE_TYPE_DATA           = 0   # regular data page (records)
PAGE_TYPE_BPLUS_INTERNAL = 1   # B+ tree internal node
PAGE_TYPE_BPLUS_LEAF     = 2   # B+ tree leaf node
PAGE_TYPE_HASH_DIR       = 3   # hash index directory page
PAGE_TYPE_HASH_BUCKET    = 4   # hash index bucket (+ overflow) page
PAGE_TYPE_CATALOG        = 5   # system catalog page

# ─── B+ tree internal node (bytes after HEADER_SIZE) ─────────────────────────
#
#  Offset 16 │ num_keys │ 2B │ 'H' │ number of separator keys in this node
#  Offset 18 │ is_root  │ 1B │ 'B' │ 1 if this node is the tree root, else 0
#  Offset 19 │ reserved │ 1B │     │ padding
#  Offset 20 │ keys[]   │ num_keys × key_size bytes
#             │ children │ (num_keys + 1) × 4 bytes  (page_ids, 'I' each)
#
#  key_size = INT_SIZE if primary key is int, else STR_SIZE.
#  Maximum order (fanout) with int key and 4096-byte pages:
#    usable = 4096 - 16 - 4 = 4076 bytes
#    max_keys such that: max_keys * INT_SIZE + (max_keys + 1) * 4 <= 4076
#    → max_keys = 508   (order-509 B+ tree)
#  With str key:
#    max_keys * STR_SIZE + (max_keys + 1) * 4 <= 4076
#    → max_keys = 113   (order-114 B+ tree)
#
BPLUS_INTERNAL_EXTRA_HEADER_FORMAT = '=HBx'   # 4 bytes (2+1+1)
BPLUS_INTERNAL_EXTRA_HEADER_SIZE   = struct.calcsize(BPLUS_INTERNAL_EXTRA_HEADER_FORMAT)

# ─── B+ tree leaf node (bytes after HEADER_SIZE) ─────────────────────────────
#
#  Offset 16 │ num_entries │ 2B │ 'H' │ number of (key, RID) pairs stored
#  Offset 18 │ next_leaf   │ 4B │ 'I' │ page_id of next leaf; NULL_PAGE_ID if none
#  Offset 22 │ reserved    │ 2B │     │ padding
#  Offset 24 │ entries[]   │ num_entries × (key_size + RID_SIZE) bytes
#
BPLUS_LEAF_EXTRA_HEADER_FORMAT = '=HIxx'   # 8 bytes (2+4+2)
BPLUS_LEAF_EXTRA_HEADER_SIZE   = struct.calcsize(BPLUS_LEAF_EXTRA_HEADER_FORMAT)

# ─── RID – Record ID stored inside index pages ────────────────────────────────
#
#  page_id  │ 4B │ 'I' │ data page that holds the record
#  slot_no  │ 1B │ 'B' │ slot index within that page (0-indexed)
#
#  Total: 5 bytes per RID.
#
RID_FORMAT = '=IB'
RID_SIZE   = struct.calcsize(RID_FORMAT)   # == 5

# ─── Hash index directory page (bytes after HEADER_SIZE) ──────────────────────
#
#  Offset 16 │ num_buckets   │ 4B │ 'I' │ total bucket count (fixed at creation)
#  Offset 20 │ bucket_ids[]  │ num_buckets × 4 bytes  (page_id of each bucket)
#
#  Static hash: hash(pk) % num_buckets → bucket index → bucket page_id.
#
HASH_DIR_EXTRA_HEADER_FORMAT = '=I'   # 4 bytes; bucket_ids array follows
NUM_HASH_BUCKETS = 64                 # chosen at type-creation time, constant

# ─── Hash bucket / overflow page (bytes after HEADER_SIZE) ────────────────────
#
#  Offset 16 │ num_entries │ 2B │ 'H' │ (key, RID) pairs in this page
#  Offset 18 │ next_bucket │ 4B │ 'I' │ overflow page_id; NULL_PAGE_ID if none
#  Offset 22 │ reserved    │ 2B │     │ padding
#  Offset 24 │ entries[]   │ num_entries × (key_size + RID_SIZE) bytes
#
HASH_BUCKET_EXTRA_HEADER_FORMAT = '=HIxx'   # 8 bytes
HASH_BUCKET_EXTRA_HEADER_SIZE   = struct.calcsize(HASH_BUCKET_EXTRA_HEADER_FORMAT)

# ─── System catalog page (bytes after HEADER_SIZE) ────────────────────────────
#
#  The catalog file (CATALOG_FILE_ID) stores one TypeCatalogEntry per slot.
#  Layout of one entry:
#
#  type_name      │ 16B │ '16s'          │ null-padded, max 15 printable chars
#  num_fields     │  1B │ 'B'            │ number of fields (≥ 6)
#  pk_order       │  1B │ 'B'            │ primary key position, 1-indexed
#  reserved       │  2B │               │ padding
#  fields[]       │ 25B each × 12 slots │ field_name (20B '20s') + type (1B 'B') + pad (4B)
#                                          type byte: 0 = int, 1 = str
#
#  Total entry size = 16 + 1 + 1 + 2 + 12 × 25 = 320 bytes
#  Entries per 4096-byte page (with 16B header) = (4096 - 16) / 320 = 12 entries/page
#
CATALOG_TYPE_NAME_SIZE  = 16
CATALOG_FIELD_NAME_SIZE = 20
CATALOG_MAX_FIELDS      = 12
CATALOG_FIELD_SLOT_SIZE = 25   # field_name (20B) + type (1B) + pad (4B)
CATALOG_ENTRY_SIZE      = (
    CATALOG_TYPE_NAME_SIZE + 1 + 1 + 2
    + CATALOG_MAX_FIELDS * CATALOG_FIELD_SLOT_SIZE
)   # == 320
CATALOG_FIELD_TYPE_INT  = 0
CATALOG_FIELD_TYPE_STR  = 1

CATALOG_FILE_ID = "_catalog"   # file_id passed to DiskSpaceManager / BufferManager

# ─── Sentinel ────────────────────────────────────────────────────────────────

NULL_PAGE_ID = 0xFFFFFFFF   # "no page" – used in tree / overflow chain pointers
