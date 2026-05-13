"""
FileIndexManager – Layer 3

Understands records, types (relations), pages, the system catalog, and indexes.
ALL page access goes through BufferManager – never calls DiskSpaceManager directly.

System catalog
──────────────
Type metadata (field names, field types, primary key order) is persisted in the
catalog file (CATALOG_FILE_ID = "_catalog") via the BufferManager.
On startup, the catalog is loaded from disk so the engine survives restarts.

Page / record layout
────────────────────
See shared/constants.py for the exact byte-level layout.
  • Data page header   : HEADER_SIZE (16 B)
  • Slot bitmap        : bits 0..9 of header.slot_bitmap
  • Record slot i      : HEADER_SIZE + i * record_size  bytes
  • record_size        : sum of field widths (INT_SIZE per int, STR_SIZE per str)

Index strategies (config["index_strategy"])
────────────────────────────────────────────
  "heap_scan"   – no index; sequential scan of all data pages.
  "hash_index"  – static hash on primary key; equality lookups only.
                  Falls back to heap_scan for range queries.
  "bplus_tree"  – B+ tree on primary key; equality and range lookups.

Indexes are created when a type is created and updated on every insert/delete.
Index data is stored in dedicated pages accessed via BufferManager.

File naming convention
──────────────────────
  Data file   : "<type_name>.db"          (file_id = type_name)
  Hash index  : "<type_name>_hash.db"     (file_id = type_name + "_hash")
  B+ tree     : "<type_name>_bplus.db"    (file_id = type_name + "_bplus")
  Catalog     : "_catalog.db"             (file_id = CATALOG_FILE_ID)
"""

from typing import List, Tuple, Any, Optional

from shared.results import TypeResult, RecordResult, RecordOpResult, TypeInfo
from buffer_manager import BufferManager


class FileIndexManager:

    def __init__(self, config: dict, buffer: BufferManager):
        self.config = config
        self.buffer = buffer
        self.strategy: str = config["index_strategy"]
        self.page_size: int = config["page_size"]
        self.max_records_per_page: int = config["max_records_per_page"]

        # In-memory cache of type schemas loaded from the catalog.
        # key: type_name → TypeInfo
        self._type_cache: dict = {}

        # Cumulative stats (reset by reset_stats())
        self._pages_accessed: int = 0
        self._index_nodes_visited: int = 0
        self._records_scanned: int = 0
        self._records_returned: int = 0

        self._load_catalog()

    # ─── Catalog bootstrap ────────────────────────────────────────────────────

    def _load_catalog(self) -> None:
        """
        Read all type definitions from the catalog file into _type_cache.
        Called once at startup; safe to call on an empty / non-existent catalog.
        """
        raise NotImplementedError

    # ─── DDL ─────────────────────────────────────────────────────────────────

    def create_type(
        self,
        type_name: str,
        fields: List[Tuple[str, str]],
        primary_key_order: int,
    ) -> TypeResult:
        """
        Register a new relation with the given schema.

        fields       : list of (field_name, field_type) in declaration order;
                       field_type is "int" or "str".
        primary_key_order : 1-indexed position of the primary key field.

        Steps:
          1. Reject if type_name already exists (return failure TypeResult).
          2. Persist the schema to the catalog pages via BufferManager.
          3. Create the data file via BufferManager.new_page().
          4. Create and initialise the index structure (if not heap_scan).
          5. Update _type_cache.

        Returns TypeResult(success=True) on success.
        """
        raise NotImplementedError

    def type_exists(self, type_name: str) -> bool:
        """Return True if type_name is in the system catalog."""
        raise NotImplementedError

    def get_type_info(self, type_name: str) -> Optional[TypeInfo]:
        """
        Return the TypeInfo for type_name, or None if the type does not exist.
        Serves QueryProcessor (for parsing commands and formatting output).
        """
        raise NotImplementedError

    # ─── DML ─────────────────────────────────────────────────────────────────

    def create_record(self, type_name: str, values: List[Any]) -> RecordOpResult:
        """
        Insert a new record into type_name.

        values : field values in declaration order (already parsed to int/str).

        Steps:
          1. Reject if type does not exist.
          2. Reject if primary key already exists (duplicate check via index or scan).
          3. Find a data page with a free slot (scan header bitmaps).
             If none, call buffer.new_page(type_name) to extend the file.
          4. Serialise values → raw bytes; write into the slot; mark page dirty
             via buffer.write_page().
          5. Update the index (insert key → RID).

        Returns RecordOpResult with pages_accessed and index_nodes_visited.
        """
        raise NotImplementedError

    def delete_record(self, type_name: str, pk_value: Any) -> RecordOpResult:
        """
        Delete the record whose primary key equals pk_value.

        Steps:
          1. Reject if type does not exist.
          2. Locate the record via index (or heap scan).
          3. Clear the slot in the bitmap and zero the slot bytes; mark dirty.
          4. Remove key from the index.
          5. Return failure if record was not found.
        """
        raise NotImplementedError

    def search_record(self, type_name: str, pk_value: Any) -> RecordResult:
        """
        Find the single record whose primary key equals pk_value.

        Uses the active index strategy:
          heap_scan  – sequential scan of all data pages.
          hash_index – hash lookup O(1).
          bplus_tree – B+ tree equality lookup O(log n).

        Returns RecordResult with records=[] and status="failure" if not found.
        """
        raise NotImplementedError

    def range_search(
        self,
        type_name: str,
        field_name: str,
        low: Any,
        high: Any,
    ) -> RecordResult:
        """
        Return all records where field_name value is in [low, high] (inclusive).
        field_name must be an integer field; return failure otherwise.

        Strategy:
          bplus_tree – use B+ tree range scan (O(log n + k)).
          hash_index – fall back to heap_scan (hash doesn't support ranges).
          heap_scan  – sequential scan of all data pages.

        Records are returned in ascending order of field_name value.
        """
        raise NotImplementedError

    # ─── Stats ────────────────────────────────────────────────────────────────

    def get_stats(self) -> dict:
        """
        Return a snapshot dict consumed by QueryProcessor for stats/explain output.
        Keys: index_strategy, pages_accessed, index_nodes_visited,
              records_scanned, records_returned.
        """
        return {
            "index_strategy": self.strategy,
            "pages_accessed": self._pages_accessed,
            "index_nodes_visited": self._index_nodes_visited,
            "records_scanned": self._records_scanned,
            "records_returned": self._records_returned,
        }

    def reset_stats(self) -> None:
        self._pages_accessed = 0
        self._index_nodes_visited = 0
        self._records_scanned = 0
        self._records_returned = 0
