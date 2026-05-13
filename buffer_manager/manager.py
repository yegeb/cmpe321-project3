"""
BufferManager – Layer 2

In-memory page cache between FileIndexManager and DiskSpaceManager.
FileIndexManager must NEVER call DiskSpaceManager directly.

Buffer pool
───────────
A fixed number of frames (config["buffer_pool_size"]) each holding one page.
Each frame tracks: (file_id, page_id, data, dirty, pinned).

Replacement policies (config["replacement_policy"])
────────────────────────────────────────────────────
  "LRU" – evict the least recently used unpinned frame.
  "MRU" – evict the most recently used unpinned frame.

Write flow
──────────
  1. FileIndexManager calls get_page(file_id, page_id) → receives page bytes.
  2. FileIndexManager modifies the bytes locally.
  3. FileIndexManager calls write_page(file_id, page_id, new_data) → buffer
     updates the frame and marks it dirty. No disk I/O yet.
  4. On eviction (or flush()), dirty frames are written to disk via disk.write_page().

new_page flow
─────────────
  When FileIndexManager needs to grow a file it calls new_page(file_id).
  BufferManager calls disk.allocate_page(file_id) to get a fresh page_id,
  loads the zero-filled page into the pool, and returns a BufferResult.
"""

from shared.results import BufferResult
from disk_space_manager import DiskSpaceManager


class BufferManager:

    def __init__(self, config: dict, disk: DiskSpaceManager):
        self.config = config
        self.disk = disk
        self.pool_size: int = config["buffer_pool_size"]
        self.policy: str = config["replacement_policy"]   # "LRU" or "MRU"

        # Stats counters (cumulative, reset by reset_stats())
        self.requests: int = 0
        self.hits: int = 0
        self.misses: int = 0
        self.evictions: int = 0
        self.dirty_writebacks: int = 0

    # ─── Core operations (called by FileIndexManager) ─────────────────────────

    def get_page(self, file_id: str, page_id: int) -> BufferResult:
        """
        Fetch (file_id, page_id) from the pool or disk.

        Hit path  – frame already in pool: return its bytes, no disk I/O.
        Miss path – evict a frame if pool is full (dirty → write to disk),
                    then read the requested page from disk into the freed frame.

        Increments requests + (hits xor misses) accordingly.
        Returns BufferResult(status="error") if disk read fails.
        """
        raise NotImplementedError

    def write_page(self, file_id: str, page_id: int, data: bytes) -> BufferResult:
        """
        Update the in-pool frame for (file_id, page_id) with new data bytes
        and mark the frame dirty.  The page must already be in the pool
        (caller should have called get_page first).

        If the frame is not in the pool, load it first (same as get_page),
        then update it.

        Does NOT write to disk immediately.
        Returns BufferResult with the updated data.
        """
        raise NotImplementedError

    def new_page(self, file_id: str) -> BufferResult:
        """
        Allocate a new page in file_id via disk.allocate_page(),
        load the zero-filled page into the buffer pool, mark it dirty,
        and return a BufferResult.

        The caller (FileIndexManager) is responsible for writing meaningful
        content via write_page() immediately after.
        """
        raise NotImplementedError

    # ─── Flush ────────────────────────────────────────────────────────────────

    def flush(self) -> None:
        """
        Write all dirty frames to disk. Called by archive.py at the end of
        every run. After flush all frames are clean (dirty=False).
        """
        raise NotImplementedError

    def flush_file(self, file_id: str) -> None:
        """Write all dirty frames belonging to file_id to disk."""
        raise NotImplementedError

    # ─── Stats ────────────────────────────────────────────────────────────────

    def get_stats(self) -> dict:
        """
        Return a snapshot dict consumed by QueryProcessor for stats output.
        Keys: requests, hits, misses, evictions, dirty_writebacks, hit_rate.
        """
        hit_rate = (self.hits / self.requests) if self.requests else 0.0
        return {
            "requests": self.requests,
            "hits": self.hits,
            "misses": self.misses,
            "evictions": self.evictions,
            "dirty_writebacks": self.dirty_writebacks,
            "hit_rate": hit_rate,
        }

    def reset_stats(self) -> None:
        self.requests = 0
        self.hits = 0
        self.misses = 0
        self.evictions = 0
        self.dirty_writebacks = 0
