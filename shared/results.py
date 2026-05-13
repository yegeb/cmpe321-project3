from __future__ import annotations
from dataclasses import dataclass
from typing import Optional, Any, List


# ─── DiskSpaceManager results ─────────────────────────────────────────────────

@dataclass
class PageResult:
    """DiskSpaceManager.read_page() return value."""
    data: bytes           # raw page bytes, exactly page_size bytes
    page_id: int          # page number within the file
    file_id: str          # relation / index file identifier
    io_performed: bool    # True if an actual disk read happened
    status: str           # "success" | "error"
    error_msg: str = ""


@dataclass
class WriteResult:
    """DiskSpaceManager.write_page() return value."""
    success: bool
    status: str           # "success" | "error"
    page_id: int
    file_id: str
    old_data: bytes       # page content before the write
    new_data: bytes       # page content after the write
    error_msg: str = ""


@dataclass
class AllocResult:
    """DiskSpaceManager.allocate_page() return value."""
    success: bool
    status: str           # "success" | "error"
    page_id: int          # newly allocated page number (0-indexed within file)
    file_id: str
    error_msg: str = ""


# ─── BufferManager results ────────────────────────────────────────────────────

@dataclass
class BufferResult:
    """
    BufferManager.get_page() / write_page() / new_page() return value.

    FileIndexManager receives this when it requests any page access.
    'data' holds the current page bytes (page_size bytes).
    After modifying data, the caller must call buffer.write_page() with the
    updated bytes so the buffer pool marks the frame dirty.
    """
    data: bytes                      # current raw page bytes
    page_id: int
    file_id: str
    cache_hit: bool                  # True if served from pool without disk I/O
    evicted_page_id: Optional[int]   # page that was evicted to make room; None if no eviction
    evicted_file_id: Optional[str]
    dirty_writeback: bool            # True if the evicted page was dirty and written to disk
    status: str                      # "success" | "error"
    error_msg: str = ""


# ─── FileIndexManager helper types ───────────────────────────────────────────

@dataclass
class FieldInfo:
    """Describes one field of a type (relation)."""
    name: str
    type: str    # "int" or "str"
    order: int   # 1-indexed position in the type definition


@dataclass
class TypeInfo:
    """Full schema of a type as returned by FileIndexManager.get_type_info()."""
    name: str
    fields: List[FieldInfo]
    primary_key_order: int   # 1-indexed; points into fields list

    @property
    def primary_key_field(self) -> FieldInfo:
        return self.fields[self.primary_key_order - 1]

    def field_by_name(self, name: str) -> Optional[FieldInfo]:
        for f in self.fields:
            if f.name == name:
                return f
        return None


# ─── FileIndexManager results ─────────────────────────────────────────────────

@dataclass
class TypeResult:
    """FileIndexManager.create_type() return value."""
    success: bool
    type_name: str
    status: str        # "success" | "failure"
    error_msg: str = ""


@dataclass
class RecordOpResult:
    """
    FileIndexManager.create_record() / delete_record() return value.
    Carries enough metadata for QueryProcessor to populate explain output.
    """
    success: bool
    status: str              # "success" | "failure"
    pages_accessed: int = 0
    index_nodes_visited: int = 0   # 0 for heap_scan
    error_msg: str = ""


@dataclass
class RecordResult:
    """
    FileIndexManager.search_record() / range_search() return value.
    Each record in 'records' is an ordered list matching the type's field order.
    """
    records: List[List[Any]]       # [[v1, v2, ...], [v1, v2, ...], ...]
    pages_accessed: int
    index_nodes_visited: int       # 0 for heap_scan
    status: str                    # "success" | "failure"
    error_msg: str = ""


@dataclass
class QueryPlanResult:
    """
    Returned by FileIndexManager.estimate_command().
    Used by QueryProcessor to print explain output before execution.
    """
    strategy: str                  # "heap_scan" | "hash_index" | "bplus_tree"
    estimated_io: int
    estimated_pages_scanned: int = 0
    estimated_index_nodes: int = 0
    status: str = "success"        # "success" | "failure"
    error_msg: str = ""
