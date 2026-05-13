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

from shared.results import (
    TypeResult,
    RecordResult,
    RecordOpResult,
    TypeInfo,
    FieldInfo,
    QueryPlanResult,
)
from shared.constants import HEADER_SIZE, PAGE_TYPE_DATA
from buffer_manager import BufferManager

from .catalog_ops import load_catalog, save_type_to_catalog
from .page_utils import (
    pack_header, unpack_header,
    record_size, pack_record, unpack_record,
    slot_is_set, set_slot, clear_slot,
    make_page,
)
from .heap_scan import (
    heap_search, heap_range, heap_delete,
    heap_find_free_slot, heap_count_pages,
)
from .hash_index import hash_init, hash_search, hash_insert, hash_delete
from .bplus_tree import bplus_init, bplus_search, bplus_insert, bplus_delete, bplus_range


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
        self._type_cache = load_catalog(self.buffer, self.page_size)

    # ─── Internal helpers ─────────────────────────────────────────────────────

    def _index_file_id(self, type_name: str) -> str:
        if self.strategy == "hash_index":
            return type_name + "_hash"
        if self.strategy == "bplus_tree":
            return type_name + "_bplus"
        return ""   # heap_scan has no index file

    def _pk_type(self, ti: TypeInfo) -> str:
        return ti.primary_key_field.type

    def _write_record_to_slot(self, type_name: str, page_id: int, slot: int,
                              data: bytearray, values: list, fields) -> None:
        """Serialise values into the correct slot in data, update header, write page."""
        rec_size = record_size(fields)
        offset = HEADER_SIZE + slot * rec_size
        data[offset: offset + rec_size] = pack_record(values, fields)

        _, num_records, slot_bitmap, _ = unpack_header(data)
        slot_bitmap = set_slot(slot_bitmap, slot)
        num_records += 1
        data[:HEADER_SIZE] = pack_header(page_id, num_records, slot_bitmap, PAGE_TYPE_DATA)
        self.buffer.write_page(type_name, page_id, bytes(data))

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
        if type_name in self._type_cache:
            return TypeResult(success=False, type_name=type_name,
                              status="failure",
                              error_msg=f"Type '{type_name}' already exists.")

        # Build TypeInfo
        field_infos = [
            FieldInfo(name=fname, type=ftype, order=i + 1)
            for i, (fname, ftype) in enumerate(fields)
        ]
        ti = TypeInfo(name=type_name, fields=field_infos,
                      primary_key_order=primary_key_order)

        # Persist to catalog
        ok = save_type_to_catalog(self.buffer, self.page_size, ti)
        if not ok:
            return TypeResult(success=False, type_name=type_name,
                              status="failure",
                              error_msg="Failed to write catalog entry.")

        # Allocate first data page
        data_res = self.buffer.new_page(type_name)
        if data_res.status != "success":
            return TypeResult(success=False, type_name=type_name,
                              status="failure",
                              error_msg="Failed to allocate data page.")
        first_page = make_page(data_res.page_id, PAGE_TYPE_DATA, self.page_size)
        self.buffer.write_page(type_name, data_res.page_id, bytes(first_page))

        # Create index structure
        if self.strategy == "hash_index":
            hash_init(self.buffer, self._index_file_id(type_name), self.page_size)
        elif self.strategy == "bplus_tree":
            bplus_init(self.buffer, self._index_file_id(type_name), self.page_size)

        # Update in-memory cache
        self._type_cache[type_name] = ti

        return TypeResult(success=True, type_name=type_name, status="success")

    def type_exists(self, type_name: str) -> bool:
        """Return True if type_name is in the system catalog."""
        return type_name in self._type_cache

    def get_type_info(self, type_name: str) -> Optional[TypeInfo]:
        """
        Return the TypeInfo for type_name, or None if the type does not exist.
        Serves QueryProcessor (for parsing commands and formatting output).
        """
        return self._type_cache.get(type_name)

    # ─── DML ─────────────────────────────────────────────────────────────────

    def create_record(self, type_name: str, values: List[Any]) -> RecordOpResult:
        """
        Insert a new record into type_name.

        values : field values in declaration order (already parsed to int/str).
        """
        if type_name not in self._type_cache:
            return RecordOpResult(success=False, status="failure",
                                  error_msg=f"Type '{type_name}' does not exist.")

        ti = self._type_cache[type_name]
        pk_idx = ti.primary_key_order - 1
        pk_value = values[pk_idx]

        # Duplicate check
        if self.strategy == "hash_index":
            rid, nv = hash_search(self.buffer, self._index_file_id(type_name),
                                   pk_value, self._pk_type(ti), self.page_size)
            self._index_nodes_visited += nv
            if rid is not None:
                return RecordOpResult(success=False, status="failure",
                                      error_msg="Duplicate primary key.")
        elif self.strategy == "bplus_tree":
            rid, nv = bplus_search(self.buffer, self._index_file_id(type_name),
                                    pk_value, self._pk_type(ti), self.page_size)
            self._index_nodes_visited += nv
            if rid is not None:
                return RecordOpResult(success=False, status="failure",
                                      error_msg="Duplicate primary key.")
        else:
            found, pa, _ = heap_search(self.buffer, type_name, pk_value,
                                        ti.primary_key_order, ti.fields, self.page_size)
            self._pages_accessed += pa
            if found:
                return RecordOpResult(success=False, status="failure",
                                      error_msg="Duplicate primary key.")

        # Find a free slot
        page_id, slot, data, pa = heap_find_free_slot(
            self.buffer, type_name, self.max_records_per_page, self.page_size)
        self._pages_accessed += pa

        if page_id == -1:
            return RecordOpResult(success=False, status="failure",
                                  error_msg="Failed to allocate slot.")

        # Write record
        self._write_record_to_slot(type_name, page_id, slot, data, values, ti.fields)
        self._pages_accessed += 1

        # Update index
        rid = (page_id, slot)
        if self.strategy == "hash_index":
            nv = hash_insert(self.buffer, self._index_file_id(type_name),
                              pk_value, rid, self._pk_type(ti), self.page_size)
            self._index_nodes_visited += nv
        elif self.strategy == "bplus_tree":
            nv = bplus_insert(self.buffer, self._index_file_id(type_name),
                               pk_value, rid, self._pk_type(ti), self.page_size)
            self._index_nodes_visited += nv

        return RecordOpResult(success=True, status="success",
                              pages_accessed=pa + 1,
                              index_nodes_visited=self._index_nodes_visited)

    def delete_record(self, type_name: str, pk_value: Any) -> RecordOpResult:
        """
        Delete the record whose primary key equals pk_value.
        """
        if type_name not in self._type_cache:
            return RecordOpResult(success=False, status="failure",
                                  error_msg=f"Type '{type_name}' does not exist.")

        ti = self._type_cache[type_name]

        # Locate via index or heap scan, then delete from data file
        if self.strategy == "hash_index":
            rid, nv = hash_search(self.buffer, self._index_file_id(type_name),
                                   pk_value, self._pk_type(ti), self.page_size)
            self._index_nodes_visited += nv
            if rid is None:
                return RecordOpResult(success=False, status="failure",
                                      error_msg="Record not found.")
            found, _, _, pa, _ = self._delete_by_rid(type_name, rid, ti)
            self._pages_accessed += pa
            nv2 = 0
            if found:
                nv2 = hash_delete(self.buffer, self._index_file_id(type_name),
                                   pk_value, self._pk_type(ti), self.page_size)
                self._index_nodes_visited += nv2
            return RecordOpResult(success=found, status="success" if found else "failure",
                                  pages_accessed=pa, index_nodes_visited=nv + nv2)

        elif self.strategy == "bplus_tree":
            rid, nv = bplus_search(self.buffer, self._index_file_id(type_name),
                                    pk_value, self._pk_type(ti), self.page_size)
            self._index_nodes_visited += nv
            if rid is None:
                return RecordOpResult(success=False, status="failure",
                                      error_msg="Record not found.")
            found, _, _, pa, _ = self._delete_by_rid(type_name, rid, ti)
            self._pages_accessed += pa
            nv2 = 0
            if found:
                nv2 = bplus_delete(self.buffer, self._index_file_id(type_name),
                                    pk_value, self._pk_type(ti), self.page_size)
                self._index_nodes_visited += nv2
            return RecordOpResult(success=found, status="success" if found else "failure",
                                  pages_accessed=pa, index_nodes_visited=nv + nv2)

        else:   # heap_scan
            found, dp_id, slot, pa, rs = heap_delete(
                self.buffer, type_name, pk_value,
                ti.primary_key_order, ti.fields,
                self.max_records_per_page, self.page_size)
            self._pages_accessed += pa
            self._records_scanned += rs
            return RecordOpResult(success=found,
                                  status="success" if found else "failure",
                                  pages_accessed=pa)

    def _delete_by_rid(self, type_name: str, rid: tuple, ti: TypeInfo):
        """Delete the record at (page_id, slot_no) directly without scanning."""
        page_id, slot_no = rid
        result = self.buffer.get_page(type_name, page_id)
        if result.status != "success":
            return False, -1, -1, 1, 0
        data = bytearray(result.data)
        _, num_records, slot_bitmap, _ = unpack_header(data)
        if not slot_is_set(slot_bitmap, slot_no):
            return False, -1, -1, 1, 0
        rec_size = record_size(ti.fields)
        offset = HEADER_SIZE + slot_no * rec_size
        data[offset: offset + rec_size] = b'\x00' * rec_size
        slot_bitmap = clear_slot(slot_bitmap, slot_no)
        num_records -= 1
        data[:HEADER_SIZE] = pack_header(page_id, num_records, slot_bitmap, PAGE_TYPE_DATA)
        self.buffer.write_page(type_name, page_id, bytes(data))
        return True, page_id, slot_no, 1, 0

    def search_record(self, type_name: str, pk_value: Any) -> RecordResult:
        """
        Find the single record whose primary key equals pk_value.
        """
        if type_name not in self._type_cache:
            return RecordResult(records=[], pages_accessed=0,
                                index_nodes_visited=0, status="failure",
                                error_msg=f"Type '{type_name}' does not exist.")

        ti = self._type_cache[type_name]

        if self.strategy == "hash_index":
            rid, nv = hash_search(self.buffer, self._index_file_id(type_name),
                                   pk_value, self._pk_type(ti), self.page_size)
            self._index_nodes_visited += nv
            if rid is None:
                return RecordResult(records=[], pages_accessed=0,
                                    index_nodes_visited=nv, status="failure",
                                    error_msg="Record not found.")
            values, pa = self._fetch_by_rid(type_name, rid, ti)
            self._pages_accessed += pa
            self._records_scanned += 1
            self._records_returned += 1 if values else 0
            return RecordResult(records=[values] if values else [],
                                pages_accessed=pa, index_nodes_visited=nv,
                                status="success" if values else "failure")

        elif self.strategy == "bplus_tree":
            rid, nv = bplus_search(self.buffer, self._index_file_id(type_name),
                                    pk_value, self._pk_type(ti), self.page_size)
            self._index_nodes_visited += nv
            if rid is None:
                return RecordResult(records=[], pages_accessed=0,
                                    index_nodes_visited=nv, status="failure",
                                    error_msg="Record not found.")
            values, pa = self._fetch_by_rid(type_name, rid, ti)
            self._pages_accessed += pa
            self._records_scanned += 1
            self._records_returned += 1 if values else 0
            return RecordResult(records=[values] if values else [],
                                pages_accessed=pa, index_nodes_visited=nv,
                                status="success" if values else "failure")

        else:   # heap_scan
            found, pa, rs = heap_search(self.buffer, type_name, pk_value,
                                         ti.primary_key_order, ti.fields, self.page_size)
            self._pages_accessed += pa
            self._records_scanned += rs
            self._records_returned += len(found)
            status = "success" if found else "failure"
            return RecordResult(records=found, pages_accessed=pa,
                                index_nodes_visited=0, status=status)

    def _fetch_by_rid(self, type_name: str, rid: tuple, ti: TypeInfo):
        """Read the record at (page_id, slot_no) by RID. Returns (values_list, pages_accessed)."""
        page_id, slot_no = rid
        result = self.buffer.get_page(type_name, page_id)
        if result.status != "success":
            return None, 1
        data = result.data
        rec_size = record_size(ti.fields)
        offset = HEADER_SIZE + slot_no * rec_size
        values = unpack_record(data, ti.fields, offset)
        return values, 1

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
        """
        if type_name not in self._type_cache:
            return RecordResult(records=[], pages_accessed=0,
                                index_nodes_visited=0, status="failure",
                                error_msg=f"Type '{type_name}' does not exist.")

        ti = self._type_cache[type_name]
        field = ti.field_by_name(field_name)
        if field is None:
            return RecordResult(records=[], pages_accessed=0,
                                index_nodes_visited=0, status="failure",
                                error_msg=f"Field '{field_name}' not found.")
        if field.type != "int":
            return RecordResult(records=[], pages_accessed=0,
                                index_nodes_visited=0, status="failure",
                                error_msg=f"Field '{field_name}' is not an integer field.")

        # B+ tree range: only if field_name is the primary key
        if (self.strategy == "bplus_tree"
                and field_name == ti.primary_key_field.name):
            rids, nv = bplus_range(self.buffer, self._index_file_id(type_name),
                                    low, high, self._pk_type(ti), self.page_size)
            self._index_nodes_visited += nv
            records = []
            pa = 0
            for rid in rids:
                values, p = self._fetch_by_rid(type_name, rid, ti)
                pa += p
                if values is not None:
                    records.append(values)
            records.sort(key=lambda row: row[field.order - 1])
            self._pages_accessed += pa
            self._records_scanned += len(rids)
            self._records_returned += len(records)
            return RecordResult(records=records, pages_accessed=pa,
                                index_nodes_visited=nv,
                                status="success" if records else "failure")

        # Fall back to heap scan (hash_index, or bplus on non-pk field, or heap_scan)
        matched, pa, rs = heap_range(self.buffer, type_name, field_name,
                                      low, high, ti.fields, self.page_size)
        self._pages_accessed += pa
        self._records_scanned += rs
        self._records_returned += len(matched)
        return RecordResult(records=matched, pages_accessed=pa,
                            index_nodes_visited=0,
                            status="success" if matched else "failure")

    def estimate_command(self, command_tokens: List[str]) -> QueryPlanResult:
        """
        Return an execution-plan estimate for QueryProcessor.explain().

        command_tokens is the tokenized inner DML command without "explain".
        Examples:
          ["search", "record", "house", "Atreides"]
          ["range_search", "house", "wealth", "4000", "9000"]
        """
        if not command_tokens:
            return QueryPlanResult(strategy="heap_scan", estimated_io=0,
                                   status="failure", error_msg="Empty command.")

        cmd = command_tokens[0].lower()

        if cmd == "create":
            return QueryPlanResult(strategy="heap_scan", estimated_io=1,
                                   estimated_pages_scanned=1)

        if cmd in ("search", "delete") and len(command_tokens) >= 4:
            type_name = command_tokens[2]
            ti = self._type_cache.get(type_name)
            if ti is None:
                return QueryPlanResult(strategy="heap_scan", estimated_io=0,
                                       status="failure",
                                       error_msg=f"Unknown type '{type_name}'.")
            page_count = heap_count_pages(self.buffer, type_name)

            if self.strategy == "heap_scan":
                est_io = self.buffer.estimate_data_page_reads(type_name, page_count)
                return QueryPlanResult(strategy="heap_scan",
                                       estimated_io=est_io,
                                       estimated_pages_scanned=page_count)

            elif self.strategy == "hash_index":
                # One directory read + ~1 bucket page
                return QueryPlanResult(strategy="hash_index",
                                       estimated_io=2,
                                       estimated_index_nodes=2)

            else:   # bplus_tree
                # Tree height ≈ log(page_count+1) / log(order)
                from math import log, ceil
                from .page_utils import key_size_for, max_internal_keys
                ks = key_size_for(self._pk_type(ti))
                order = max_internal_keys(self.page_size, ks) + 1
                height = max(1, ceil(log(max(1, page_count * self.max_records_per_page) + 1,
                                        max(2, order))))
                return QueryPlanResult(strategy="bplus_tree",
                                       estimated_io=height + 1,
                                       estimated_index_nodes=height,
                                       estimated_pages_scanned=1)

        if cmd == "range_search" and len(command_tokens) >= 5:
            type_name = command_tokens[1]
            field_name = command_tokens[2]
            ti = self._type_cache.get(type_name)
            if ti is None:
                return QueryPlanResult(strategy="heap_scan", estimated_io=0,
                                       status="failure",
                                       error_msg=f"Unknown type '{type_name}'.")
            page_count = heap_count_pages(self.buffer, type_name)

            use_bplus = (self.strategy == "bplus_tree"
                         and field_name == ti.primary_key_field.name)
            if use_bplus:
                from math import log, ceil
                from .page_utils import key_size_for, max_internal_keys
                ks = key_size_for(self._pk_type(ti))
                order = max_internal_keys(self.page_size, ks) + 1
                height = max(1, ceil(log(max(1, page_count * self.max_records_per_page) + 1,
                                        max(2, order))))
                return QueryPlanResult(strategy="bplus_tree",
                                       estimated_io=height + page_count,
                                       estimated_index_nodes=height,
                                       estimated_pages_scanned=page_count)
            else:
                est_io = self.buffer.estimate_data_page_reads(type_name, page_count)
                return QueryPlanResult(strategy="heap_scan",
                                       estimated_io=est_io,
                                       estimated_pages_scanned=page_count)

        return QueryPlanResult(strategy="heap_scan", estimated_io=0,
                               status="failure", error_msg="Unrecognised command.")

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
