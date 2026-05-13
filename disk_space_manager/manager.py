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
Free slots within pages are tracked by slot bitmaps maintained by the
FileIndexManager (one bit per slot in the page header).  At the page
level, allocate_page() simply appends a new zeroed page to the file;
no page is ever freed back to the disk layer.

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
        self._base_dir: str = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

        self.read_count: int = 0
        self.write_count: int = 0

        # log_write hook – replaced by a real implementation if needed.
        # Must be called on every write (currently a no-op).
        self._log_write_hook = None

    # ─── Internal helpers ─────────────────────────────────────────────────────

    def _zero_page(self) -> bytes:
        return b"\x00" * self.page_size

    def _ensure_parent_dir(self) -> None:
        os.makedirs(self._base_dir, exist_ok=True)

    def _page_offset(self, page_id: int) -> int:
        return page_id * self.page_size

    def _read_page_bytes_no_count(self, file_id: str, page_id: int) -> bytes:
        file_path = self._file_path(file_id)
        with open(file_path, "rb") as data_file:
            data_file.seek(self._page_offset(page_id))
            return data_file.read(self.page_size)

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

    def delete_file(self, file_id: str) -> bool:
        """
        Best-effort deletion helper used for cleanup of failed type creation.
        Returns True if the file is absent after the call.
        """
        file_path = self._file_path(file_id)
        try:
            if os.path.exists(file_path):
                os.remove(file_path)
            return True
        except OSError:
            return False

    def create_file(self, file_id: str) -> bool:
        """
        Create the binary file if it doesn't exist.
        Returns True if a new file was created, False if it already existed.
        """
        self._ensure_parent_dir()
        file_path = self._file_path(file_id)
        if not os.path.exists(file_path):
            with open(file_path, "wb"):
                pass
            return True
        return False

    def file_exists(self, file_id: str) -> bool:
        """Return True if the file for file_id exists on disk."""
        return os.path.exists(self._file_path(file_id))

    def get_page_count(self, file_id: str) -> int:
        """Return the total number of pages currently in the file."""
        file_path = self._file_path(file_id)
        if not os.path.exists(file_path):
            return 0
        size = os.path.getsize(file_path)
        return size // self.page_size

    # ─── Core I/O ─────────────────────────────────────────────────────────────

    def read_page(self, file_id: str, page_id: int) -> PageResult:
        """
        Read page_id from file_id.
        Returns PageResult with exactly page_size bytes in .data.
        Increments read_count on actual disk access.

        Returns PageResult(status="error") if file does not exist or
        page_id is out of range.
        """
        if page_id < 0:
            return PageResult(
                data=b"",
                page_id=page_id,
                file_id=file_id,
                io_performed=False,
                status="error",
                error_msg="page_id must be non-negative",
            )

        if not self.file_exists(file_id):
            return PageResult(
                data=b"",
                page_id=page_id,
                file_id=file_id,
                io_performed=False,
                status="error",
                error_msg="file does not exist",
            )

        page_count = self.get_page_count(file_id)
        if page_id >= page_count:
            return PageResult(
                data=b"",
                page_id=page_id,
                file_id=file_id,
                io_performed=False,
                status="error",
                error_msg="page_id out of range",
            )

        file_path = self._file_path(file_id)
        with open(file_path, "rb") as data_file:
            data_file.seek(self._page_offset(page_id))
            data = data_file.read(self.page_size)

        self.read_count += 1

        if len(data) != self.page_size:
            return PageResult(
                data=data,
                page_id=page_id,
                file_id=file_id,
                io_performed=True,
                status="error",
                error_msg="short read",
            )

        return PageResult(
            data=data,
            page_id=page_id,
            file_id=file_id,
            io_performed=True,
            status="success",
            error_msg="",
        )

    def write_page(self, file_id: str, page_id: int, data: bytes) -> WriteResult:
        """
        Overwrite page_id in file_id with data (must be exactly page_size bytes).
        Increments write_count.
        Calls self.log_write() before returning.

        Returns WriteResult(success=False) if file does not exist or
        page_id is out of range.
        """
        if page_id < 0:
            return WriteResult(
                success=False,
                status="error",
                page_id=page_id,
                file_id=file_id,
                old_data=b"",
                new_data=data,
                error_msg="page_id must be non-negative",
            )

        if len(data) != self.page_size:
            return WriteResult(
                success=False,
                status="error",
                page_id=page_id,
                file_id=file_id,
                old_data=b"",
                new_data=data,
                error_msg="data length must equal page_size",
            )

        if not self.file_exists(file_id):
            return WriteResult(
                success=False,
                status="error",
                page_id=page_id,
                file_id=file_id,
                old_data=b"",
                new_data=data,
                error_msg="file does not exist",
            )

        page_count = self.get_page_count(file_id)
        if page_id >= page_count:
            return WriteResult(
                success=False,
                status="error",
                page_id=page_id,
                file_id=file_id,
                old_data=b"",
                new_data=data,
                error_msg="page_id out of range",
            )

        old_data = self._read_page_bytes_no_count(file_id, page_id)
        if len(old_data) != self.page_size:
            return WriteResult(
                success=False,
                status="error",
                page_id=page_id,
                file_id=file_id,
                old_data=old_data,
                new_data=data,
                error_msg="short read before write",
            )

        file_path = self._file_path(file_id)
        with open(file_path, "r+b") as data_file:
            data_file.seek(self._page_offset(page_id))
            data_file.write(data)
            data_file.flush()

        self.write_count += 1
        self.log_write(file_id, page_id, old_data, data)

        return WriteResult(
            success=True,
            status="success",
            page_id=page_id,
            file_id=file_id,
            old_data=old_data,
            new_data=data,
            error_msg="",
        )

    def allocate_page(self, file_id: str) -> AllocResult:
        """
        Append a new zeroed page to file_id and return its page_id.
        Creates the file if it does not yet exist.

        Returns AllocResult(success=False) on I/O error.
        """
        try:
            self.create_file(file_id)
            file_path = self._file_path(file_id)
            page_id = self.get_page_count(file_id)

            with open(file_path, "ab") as data_file:
                data_file.write(self._zero_page())
                data_file.flush()

            self.write_count += 1
            self.log_write(file_id, page_id, b"", self._zero_page())

            return AllocResult(
                success=True,
                status="success",
                page_id=page_id,
                file_id=file_id,
                error_msg="",
            )
        except OSError as exc:
            return AllocResult(
                success=False,
                status="error",
                page_id=-1,
                file_id=file_id,
                error_msg=str(exc),
            )

    # ─── Stats ────────────────────────────────────────────────────────────────

    def get_stats(self) -> dict:
        """Return {"reads": int, "writes": int}."""
        return {"reads": self.read_count, "writes": self.write_count}

    def reset_stats(self) -> None:
        self.read_count = 0
        self.write_count = 0
