"""
BufferManager – Layer 2

In-memory page cache between FileIndexManager and DiskSpaceManager.
FileIndexManager must NEVER call DiskSpaceManager directly.

Buffer pool
───────────
A fixed number of frames (config["buffer_pool_size"]) each holding one page.
Each frame tracks: (file_id, page_id, data, dirty, last_access_counter).

Replacement policies (config["replacement_policy"])
────────────────────────────────────────────────────
  "LRU" – evict the least recently used frame.
  "MRU" – evict the most recently used frame.

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

from dataclasses import dataclass

from shared.results import BufferResult
from disk_space_manager import DiskSpaceManager


@dataclass
class _Frame:
    file_id: str
    page_id: int
    data: bytes
    dirty: bool
    last_access_counter: int


class BufferManager:

    def __init__(self, config: dict, disk: DiskSpaceManager):
        self.config = config
        self.disk = disk
        self.pool_size: int = config["buffer_pool_size"]
        self.policy: str = config["replacement_policy"]   # "LRU" or "MRU"

        if self.pool_size <= 0:
            raise ValueError("buffer_pool_size must be positive")
        if self.policy not in {"LRU", "MRU"}:
            raise ValueError("replacement_policy must be 'LRU' or 'MRU'")

        # Stats counters (cumulative, reset by reset_stats())
        self.requests: int = 0
        self.hits: int = 0
        self.misses: int = 0
        self.evictions: int = 0
        self.dirty_writebacks: int = 0

        self._frames: dict[tuple[str, int], _Frame] = {}
        self._access_counter: int = 0

    # ─── Internal helpers ─────────────────────────────────────────────────────

    def _next_access_counter(self) -> int:
        self._access_counter += 1
        return self._access_counter

    def _touch(self, frame: _Frame) -> None:
        frame.last_access_counter = self._next_access_counter()

    def _make_result(
        self,
        frame: _Frame | None,
        cache_hit: bool,
        evicted_page_id: int | None,
        evicted_file_id: str | None,
        dirty_writeback: bool,
        status: str,
        error_msg: str = "",
    ) -> BufferResult:
        if frame is None:
            return BufferResult(
                data=b"",
                page_id=-1,
                file_id="",
                cache_hit=cache_hit,
                evicted_page_id=evicted_page_id,
                evicted_file_id=evicted_file_id,
                dirty_writeback=dirty_writeback,
                status=status,
                error_msg=error_msg,
            )

        return BufferResult(
            data=frame.data,
            page_id=frame.page_id,
            file_id=frame.file_id,
            cache_hit=cache_hit,
            evicted_page_id=evicted_page_id,
            evicted_file_id=evicted_file_id,
            dirty_writeback=dirty_writeback,
            status=status,
            error_msg=error_msg,
        )

    def _select_victim_key(self) -> tuple[str, int] | None:
        if not self._frames:
            return None

        if self.policy == "MRU":
            return max(
                self._frames,
                key=lambda key: self._frames[key].last_access_counter,
            )

        return min(
            self._frames,
            key=lambda key: self._frames[key].last_access_counter,
        )

    def _evict_if_needed(self) -> tuple[int | None, str | None, bool, str | None]:
        if len(self._frames) < self.pool_size:
            return None, None, False, None

        victim_key = self._select_victim_key()
        if victim_key is None:
            return None, None, False, "no frame available for eviction"

        victim = self._frames[victim_key]
        dirty_writeback = False

        if victim.dirty:
            write_result = self.disk.write_page(victim.file_id, victim.page_id, victim.data)
            if not write_result.success:
                return None, None, False, write_result.error_msg or "dirty writeback failed"
            self.dirty_writebacks += 1
            dirty_writeback = True

        del self._frames[victim_key]
        self.evictions += 1
        return victim.page_id, victim.file_id, dirty_writeback, None

    def _load_frame_from_disk(
        self,
        file_id: str,
        page_id: int,
    ) -> tuple[_Frame | None, int | None, str | None, bool, str | None]:
        evicted_page_id, evicted_file_id, dirty_writeback, eviction_error = self._evict_if_needed()
        if eviction_error is not None:
            return None, None, None, False, eviction_error

        page_result = self.disk.read_page(file_id, page_id)
        if page_result.status != "success":
            return None, evicted_page_id, evicted_file_id, dirty_writeback, page_result.error_msg

        frame = _Frame(
            file_id=file_id,
            page_id=page_id,
            data=page_result.data,
            dirty=False,
            last_access_counter=self._next_access_counter(),
        )
        self._frames[(file_id, page_id)] = frame
        return frame, evicted_page_id, evicted_file_id, dirty_writeback, None

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
        self.requests += 1
        key = (file_id, page_id)

        if key in self._frames:
            frame = self._frames[key]
            self.hits += 1
            self._touch(frame)
            return self._make_result(
                frame=frame,
                cache_hit=True,
                evicted_page_id=None,
                evicted_file_id=None,
                dirty_writeback=False,
                status="success",
            )

        self.misses += 1
        frame, evicted_page_id, evicted_file_id, dirty_writeback, error = self._load_frame_from_disk(
            file_id,
            page_id,
        )
        if frame is None:
            return BufferResult(
                data=b"",
                page_id=page_id,
                file_id=file_id,
                cache_hit=False,
                evicted_page_id=evicted_page_id,
                evicted_file_id=evicted_file_id,
                dirty_writeback=dirty_writeback,
                status="error",
                error_msg=error or "failed to fetch page",
            )

        return self._make_result(
            frame=frame,
            cache_hit=False,
            evicted_page_id=evicted_page_id,
            evicted_file_id=evicted_file_id,
            dirty_writeback=dirty_writeback,
            status="success",
        )

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
        if len(data) != self.disk.page_size:
            return BufferResult(
                data=b"",
                page_id=page_id,
                file_id=file_id,
                cache_hit=False,
                evicted_page_id=None,
                evicted_file_id=None,
                dirty_writeback=False,
                status="error",
                error_msg="data length must equal page_size",
            )

        key = (file_id, page_id)
        cache_hit = key in self._frames
        evicted_page_id = None
        evicted_file_id = None
        dirty_writeback = False

        if key not in self._frames:
            fetch_result = self.get_page(file_id, page_id)
            if fetch_result.status != "success":
                return fetch_result
            cache_hit = fetch_result.cache_hit
            evicted_page_id = fetch_result.evicted_page_id
            evicted_file_id = fetch_result.evicted_file_id
            dirty_writeback = fetch_result.dirty_writeback

        frame = self._frames[key]
        frame.data = data
        frame.dirty = True
        self._touch(frame)
        return self._make_result(
            frame=frame,
            cache_hit=cache_hit,
            evicted_page_id=evicted_page_id,
            evicted_file_id=evicted_file_id,
            dirty_writeback=dirty_writeback,
            status="success",
        )

    def new_page(self, file_id: str) -> BufferResult:
        """
        Allocate a new page in file_id via disk.allocate_page(),
        load the zero-filled page into the buffer pool, mark it dirty,
        and return a BufferResult.

        The caller (FileIndexManager) is responsible for writing meaningful
        content via write_page() immediately after.
        """
        alloc_result = self.disk.allocate_page(file_id)
        if not alloc_result.success:
            return BufferResult(
                data=b"",
                page_id=alloc_result.page_id,
                file_id=file_id,
                cache_hit=False,
                evicted_page_id=None,
                evicted_file_id=None,
                dirty_writeback=False,
                status="error",
                error_msg=alloc_result.error_msg,
            )

        key = (file_id, alloc_result.page_id)
        if key in self._frames:
            frame = self._frames[key]
            frame.data = b"\x00" * self.disk.page_size
            frame.dirty = True
            self._touch(frame)
            return self._make_result(
                frame=frame,
                cache_hit=True,
                evicted_page_id=None,
                evicted_file_id=None,
                dirty_writeback=False,
                status="success",
            )

        evicted_page_id, evicted_file_id, dirty_writeback, eviction_error = self._evict_if_needed()
        if eviction_error is not None:
            return BufferResult(
                data=b"",
                page_id=alloc_result.page_id,
                file_id=file_id,
                cache_hit=False,
                evicted_page_id=None,
                evicted_file_id=None,
                dirty_writeback=False,
                status="error",
                error_msg=eviction_error,
            )

        frame = _Frame(
            file_id=file_id,
            page_id=alloc_result.page_id,
            data=b"\x00" * self.disk.page_size,
            dirty=True,
            last_access_counter=self._next_access_counter(),
        )
        self._frames[key] = frame
        return self._make_result(
            frame=frame,
            cache_hit=False,
            evicted_page_id=evicted_page_id,
            evicted_file_id=evicted_file_id,
            dirty_writeback=dirty_writeback,
            status="success",
        )

    # ─── Flush ────────────────────────────────────────────────────────────────

    def flush(self) -> None:
        """
        Write all dirty frames to disk. Called by archive.py at the end of
        every run. After flush all frames are clean (dirty=False).
        """
        for frame in list(self._frames.values()):
            if not frame.dirty:
                continue
            write_result = self.disk.write_page(frame.file_id, frame.page_id, frame.data)
            if write_result.success:
                frame.dirty = False

    def flush_file(self, file_id: str) -> None:
        """Write all dirty frames belonging to file_id to disk."""
        for frame in list(self._frames.values()):
            if frame.file_id != file_id or not frame.dirty:
                continue
            write_result = self.disk.write_page(frame.file_id, frame.page_id, frame.data)
            if write_result.success:
                frame.dirty = False

    def estimate_data_page_reads(self, file_id: str, page_count: int) -> int:
        """
        Return a simple estimate for how many data-page reads a future operation
        may perform for this file under the current buffer state.

        This is intentionally approximate and is only used by QueryProcessor's
        explain output. The default contract is:
          - pages already resident in the buffer may contribute 0 estimated I/O
          - pages not resident may contribute 1 estimated read each
        """
        resident_pages = sum(1 for frame_key in self._frames if frame_key[0] == file_id)
        return max(0, page_count - resident_pages)

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
