"""
QueryProcessor – Layer 4

Parses every input line, delegates to FileIndexManager, collects statistics,
writes results to output.txt, and logs every operation to log.csv.

It receives references to all four layers so it can read their stats counters.
It does NOT bypass layers for data access – only reads counters from them.

Output files (all in the same directory as archive.py)
────────────────────────────────────────────────────────
  output.txt       – results of search/range_search/explain commands (append)
  stats_output.txt – overwritten on every `stats` command
  log.csv          – append-only operation log (persistent across restarts)

Command grammar (all tokens space-separated)
─────────────────────────────────────────────
  create type <name> <num_fields> <pk_order> <f1> <t1> <f2> <t2> ...
  create record <type> <v1> <v2> ...
  delete record <type> <pk>
  search record <type> <pk>
  range_search <type> <field> <low> <high>
  explain <any DML command above>
  stats
  stats reset

Parsing notes
─────────────
  • pk_order is 1-indexed.
  • Values for int fields are parsed with int(); str fields stay as str.
  • pk for search/delete is cast to the primary key field's type before
    being passed to FileIndexManager.
  • range_search low/high are always int (validated before calling FIM).

Error handling
──────────────
  All failure conditions are logged as "failure" in log.csv and do NOT
  crash the engine. No output is written to output.txt on failure, except
  for explain which always prints the plan section.
"""

import os
import time

from file_index_manager import FileIndexManager
from buffer_manager import BufferManager
from disk_space_manager import DiskSpaceManager


class QueryProcessor:

    def __init__(
        self,
        config: dict,
        file_idx: FileIndexManager,
        buffer: BufferManager,
        disk: DiskSpaceManager,
    ):
        self.config = config
        self.file_idx = file_idx
        self.buffer = buffer
        self.disk = disk

        # Resolve output paths relative to archive.py's directory.
        self._base_dir: str = os.path.dirname(
            os.path.abspath(__file__ + "/../../archive.py")
        )
        self._output_path: str = os.path.join(self._base_dir, "output.txt")
        self._stats_path: str = os.path.join(self._base_dir, "stats_output.txt")
        self._log_path: str = os.path.join(self._base_dir, "log.csv")

    # ─── Entry point ─────────────────────────────────────────────────────────

    def process(self, line: str) -> None:
        """
        Parse and execute one command line.

        Dispatch table:
          "create type …"    → _cmd_create_type
          "create record …"  → _cmd_create_record
          "delete record …"  → _cmd_delete_record
          "search record …"  → _cmd_search_record
          "range_search …"   → _cmd_range_search
          "explain …"        → _cmd_explain
          "stats reset"      → _cmd_stats_reset
          "stats"            → _cmd_stats
          unknown            → log as failure, do nothing
        """
        raise NotImplementedError

    # ─── Command handlers ─────────────────────────────────────────────────────

    def _cmd_create_type(self, tokens: list) -> None:
        """
        Tokens (after "create type" stripped):
          [name, num_fields, pk_order, f1, t1, f2, t2, ...]

        Calls file_idx.create_type(); logs success or failure.
        """
        raise NotImplementedError

    def _cmd_create_record(self, tokens: list) -> None:
        """
        Tokens (after "create record" stripped):
          [type_name, v1, v2, ...]

        Casts each value to the declared field type before calling
        file_idx.create_record(). Logs success or failure.
        """
        raise NotImplementedError

    def _cmd_delete_record(self, tokens: list) -> None:
        """
        Tokens: [type_name, pk_value]
        Casts pk_value to primary key type. Logs success or failure.
        """
        raise NotImplementedError

    def _cmd_search_record(self, tokens: list) -> None:
        """
        Tokens: [type_name, pk_value]

        On success: write one line "<v1> <v2> ..." to output.txt.
        On failure (not found / type missing): log failure, write nothing.
        """
        raise NotImplementedError

    def _cmd_range_search(self, tokens: list) -> None:
        """
        Tokens: [type_name, field_name, low, high]

        On success: write one line per matching record to output.txt.
        Failure conditions: type missing, field not int, field not found.
        """
        raise NotImplementedError

    def _cmd_explain(self, tokens: list) -> None:
        """
        tokens is the full remaining token list after "explain" is stripped.
        Re-assembles the inner command, executes it normally, but also writes:

          --- PLAN ---
          Query: <original command>
          Strategy: <heap_scan | hash_index | bplus_tree>
          Estimated I/O: <integer>
          --- RESULT ---
          <normal result lines>
          --- STATS ---
          Actual I/O: <reads> reads, <writes> writes
          Buffer Hits: <n>
          Buffer Misses: <n>
          Pages Scanned: <n>

        The plan section must be produced from
        file_idx.estimate_command(tokens), not hard-coded inside QueryProcessor.
        All written to output.txt.
        """
        raise NotImplementedError

    def _cmd_stats(self) -> None:
        """
        Overwrite stats_output.txt with the fixed format:

          === STATISTICS ===
          Disk I/O: <reads> reads, <writes> writes
          Buffer Pool: <requests> requests, <hits> hits, <misses> misses (<hit_rate>% hit rate)
          Evictions: <n> (<dirty> dirty writebacks)
          Index: <strategy>, <nodes> nodes visited
          Records: <scanned> scanned, <returned> returned
        """
        raise NotImplementedError

    def _cmd_stats_reset(self) -> None:
        """Reset all layer counters via their reset_stats() methods."""
        raise NotImplementedError

    # ─── Output / log helpers ─────────────────────────────────────────────────

    def _write_output(self, text: str) -> None:
        """Append text (with newline) to output.txt."""
        with open(self._output_path, "a", encoding="ascii") as f:
            f.write(text + "\n")

    def _log(self, command: str, status: str) -> None:
        """
        Append one CSV row to log.csv:
          <unix_timestamp>,<command_string>,<status>
        status is "success" or "failure".
        """
        ts = int(time.time())
        with open(self._log_path, "a", encoding="ascii") as f:
            f.write(f"{ts},{command},{status}\n")

    def _format_record(self, values: list) -> str:
        """Format a record's field values as a space-separated string."""
        return " ".join(str(v) for v in values)
