"""
DiskSpaceManager – Layer 1

Sole component that performs real file I/O.
Reads and writes fixed-size pages to/from binary files using seek().
Has no knowledge of records, indexes, or queries – only raw pages.

File layout on disk
───────────────────
Each relation and each index is stored as a separate binary file named
'<file_id>.db' in the same directory as archive.py.

Free-space tracking
────────────────────
A companion metadata file '<file_id>.meta' stores a simple free-list as a
JSON array of free page_ids.  When allocate_page() is called, the list is
checked first; if empty, the file is extended by one page.

I/O counting
─────────────
read_count and write_count are incremented on every actual disk operation.
BufferManager reads these via get_stats().
"""

import os

from shared.results import PageResult, WriteResult, AllocResult


class DiskSpaceManager:

    def __init__(self, config: dict):
        self.config = config
        self.page_size: int = config["page_size"]

        # Directory where archive.py lives – all files written here.
        self._base_dir: str = os.path.dirname(os.path.abspath(__file__ + "/../../archive.py"))

        self.read_count: int = 0
        self.write_count: int = 0

        # log_write hook – replaced by a real implementation if needed.
        # Must be called on every write (currently a no-op).
        self._log_write_hook = None

    # ─── Public log_write stub ────────────────────────────────────────────────

    def log_write(
        self,
        file_id: str,
        page_id: int,
        old_data: bytes,
        new_data: bytes,
    ) -> None:
        """Called on every write. Currently a no-op stub."""
        if self._log_write_hook is not None:
            self._log_write_hook(file_id, page_id, old_data, new_data)

    # ─── File helpers ─────────────────────────────────────────────────────────

    def _file_path(self, file_id: str) -> str:
        return os.path.join(self._base_dir, f"{file_id}.db")

    def _meta_path(self, file_id: str) -> str:
        return os.path.join(self._base_dir, f"{file_id}.meta")

    def create_file(self, file_id: str) -> bool:
        """
        Create the binary file and its companion meta file if they don't exist.
        Returns True if created, False if already existed.
        """
        raise NotImplementedError

    def file_exists(self, file_id: str) -> bool:
        """Return True if the file for file_id exists on disk."""
        raise NotImplementedError

    def get_page_count(self, file_id: str) -> int:
        """Return the total number of pages currently in the file."""
        raise NotImplementedError

    # ─── Core I/O ─────────────────────────────────────────────────────────────

    def read_page(self, file_id: str, page_id: int) -> PageResult:
        """
        Read page_id from file_id.
        Returns PageResult with exactly page_size bytes in .data.
        Increments read_count on actual disk access.

        Returns PageResult(status="error") if file does not exist or
        page_id is out of range.
        """
        raise NotImplementedError

    def write_page(self, file_id: str, page_id: int, data: bytes) -> WriteResult:
        """
        Overwrite page_id in file_id with data (must be exactly page_size bytes).
        Increments write_count.
        Calls self.log_write() before returning.

        Returns WriteResult(success=False) if file does not exist or
        page_id is out of range.
        """
        raise NotImplementedError

    def allocate_page(self, file_id: str) -> AllocResult:
        """
        Extend file_id by one page and return the new page's id.
        The new page is zeroed (b'\\x00' * page_size).
        Creates the file if it does not yet exist.

        Returns AllocResult(success=False) on I/O error.
        """
        raise NotImplementedError

    # ─── Stats ────────────────────────────────────────────────────────────────

    def get_stats(self) -> dict:
        """Return {"reads": int, "writes": int}."""
        return {"reads": self.read_count, "writes": self.write_count}

    def reset_stats(self) -> None:
        self.read_count = 0
        self.write_count = 0
