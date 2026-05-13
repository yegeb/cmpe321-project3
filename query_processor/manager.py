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

        # State used across process() / handlers
        self._current_line: str = ""
        self._explain_mode: bool = False
        self._explain_buffer: list = []
        self._suppress_log: bool = False
        self._last_command_status: str = "success"

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
        self._current_line = line
        self._last_command_status = "success"
        tokens = line.split()
        if not tokens:
            return

        cmd = tokens[0].lower()

        if cmd == "explain":
            self._cmd_explain(tokens[1:])
            return

        if cmd == "stats":
            if len(tokens) == 2 and tokens[1].lower() == "reset":
                self._cmd_stats_reset()
                self._log(line, self._last_command_status)
            elif len(tokens) == 1:
                self._cmd_stats()
                self._log(line, self._last_command_status)
            else:
                self._last_command_status = "failure"
                self._log(line, "failure")
            return

        if cmd == "create" and len(tokens) >= 2:
            sub = tokens[1].lower()
            if sub == "type":
                self._cmd_create_type(tokens[2:])
            elif sub == "record":
                self._cmd_create_record(tokens[2:])
            else:
                self._last_command_status = "failure"
                self._log(line, "failure")
            return

        if cmd == "delete" and len(tokens) >= 2 and tokens[1].lower() == "record":
            self._cmd_delete_record(tokens[2:])
            return

        if cmd == "search" and len(tokens) >= 2 and tokens[1].lower() == "record":
            self._cmd_search_record(tokens[2:])
            return

        if cmd == "range_search":
            self._cmd_range_search(tokens[1:])
            return

        self._last_command_status = "failure"
        self._log(line, "failure")

    # ─── Internal dispatch (reused by _cmd_explain) ───────────────────────────

    def _dispatch(self, tokens: list) -> None:
        """Route inner command tokens. Used by _cmd_explain to re-run the inner command."""
        if not tokens:
            self._last_command_status = "failure"
            return
        cmd = tokens[0].lower()
        if cmd == "create" and len(tokens) >= 2:
            sub = tokens[1].lower()
            if sub == "type":
                self._cmd_create_type(tokens[2:])
            elif sub == "record":
                self._cmd_create_record(tokens[2:])
            else:
                self._last_command_status = "failure"
        elif cmd == "delete" and len(tokens) >= 2 and tokens[1].lower() == "record":
            self._cmd_delete_record(tokens[2:])
        elif cmd == "search" and len(tokens) >= 2 and tokens[1].lower() == "record":
            self._cmd_search_record(tokens[2:])
        elif cmd == "range_search":
            self._cmd_range_search(tokens[1:])
        else:
            self._last_command_status = "failure"

    # ─── Command handlers ─────────────────────────────────────────────────────

    def _cmd_create_type(self, tokens: list) -> None:
        """
        Tokens (after "create type" stripped):
          [name, num_fields, pk_order, f1, t1, f2, t2, ...]

        Calls file_idx.create_type(); logs success or failure.
        """
        if len(tokens) < 3:
            self._last_command_status = "failure"
            self._log(self._current_line, "failure")
            return

        type_name = tokens[0]
        try:
            num_fields = int(tokens[1])
            pk_order = int(tokens[2])
        except ValueError:
            self._last_command_status = "failure"
            self._log(self._current_line, "failure")
            return

        if len(tokens) != 3 + 2 * num_fields:
            self._last_command_status = "failure"
            self._log(self._current_line, "failure")
            return

        fields = []
        for i in range(num_fields):
            fname = tokens[3 + 2 * i]
            ftype = tokens[4 + 2 * i]
            if ftype not in ("int", "str"):
                self._last_command_status = "failure"
                self._log(self._current_line, "failure")
                return
            fields.append((fname, ftype))

        result = self.file_idx.create_type(type_name, fields, pk_order)
        self._last_command_status = "success" if result.success else "failure"
        self._log(self._current_line, "success" if result.success else "failure")

    def _cmd_create_record(self, tokens: list) -> None:
        """
        Tokens (after "create record" stripped):
          [type_name, v1, v2, ...]

        Casts each value to the declared field type before calling
        file_idx.create_record(). Logs success or failure.
        """
        if not tokens:
            self._last_command_status = "failure"
            self._log(self._current_line, "failure")
            return

        type_name = tokens[0]
        ti = self.file_idx.get_type_info(type_name)
        if ti is None:
            self._last_command_status = "failure"
            self._log(self._current_line, "failure")
            return

        raw_values = tokens[1:]
        if len(raw_values) != len(ti.fields):
            self._last_command_status = "failure"
            self._log(self._current_line, "failure")
            return

        values = []
        for field, raw in zip(ti.fields, raw_values):
            try:
                if field.type == "int":
                    values.append(int(raw))
                else:
                    if not raw.isalnum():
                        raise ValueError
                    values.append(raw)
            except ValueError:
                self._last_command_status = "failure"
                self._log(self._current_line, "failure")
                return

        result = self.file_idx.create_record(type_name, values)
        self._last_command_status = "success" if result.success else "failure"
        self._log(self._current_line, "success" if result.success else "failure")

    def _cmd_delete_record(self, tokens: list) -> None:
        """
        Tokens: [type_name, pk_value]
        Casts pk_value to primary key type. Logs success or failure.
        """
        if len(tokens) != 2:
            self._last_command_status = "failure"
            self._log(self._current_line, "failure")
            return

        type_name = tokens[0]
        ti = self.file_idx.get_type_info(type_name)
        if ti is None:
            self._last_command_status = "failure"
            self._log(self._current_line, "failure")
            return

        pk_field = ti.primary_key_field
        try:
            pk_value = int(tokens[1]) if pk_field.type == "int" else tokens[1]
        except ValueError:
            self._last_command_status = "failure"
            self._log(self._current_line, "failure")
            return

        result = self.file_idx.delete_record(type_name, pk_value)
        self._last_command_status = "success" if result.success else "failure"
        self._log(self._current_line, "success" if result.success else "failure")

    def _cmd_search_record(self, tokens: list) -> None:
        """
        Tokens: [type_name, pk_value]

        On success: write one line "<v1> <v2> ..." to output.txt.
        On failure (not found / type missing): log failure, write nothing.
        """
        if len(tokens) != 2:
            self._last_command_status = "failure"
            self._log(self._current_line, "failure")
            return

        type_name = tokens[0]
        ti = self.file_idx.get_type_info(type_name)
        if ti is None:
            self._last_command_status = "failure"
            self._log(self._current_line, "failure")
            return

        pk_field = ti.primary_key_field
        try:
            pk_value = int(tokens[1]) if pk_field.type == "int" else tokens[1]
        except ValueError:
            self._last_command_status = "failure"
            self._log(self._current_line, "failure")
            return

        result = self.file_idx.search_record(type_name, pk_value)
        if result.records:
            for record in result.records:
                self._write_output(self._format_record(record))
            self._last_command_status = "success"
            self._log(self._current_line, "success")
        else:
            self._last_command_status = "failure"
            self._log(self._current_line, "failure")

    def _cmd_range_search(self, tokens: list) -> None:
        """
        Tokens: [type_name, field_name, low, high]

        On success: write one line per matching record to output.txt.
        Failure conditions: type missing, field not int, field not found.
        """
        if len(tokens) != 4:
            self._last_command_status = "failure"
            self._log(self._current_line, "failure")
            return

        type_name = tokens[0]
        field_name = tokens[1]

        try:
            low = int(tokens[2])
            high = int(tokens[3])
        except ValueError:
            self._last_command_status = "failure"
            self._log(self._current_line, "failure")
            return

        ti = self.file_idx.get_type_info(type_name)
        if ti is None:
            self._last_command_status = "failure"
            self._log(self._current_line, "failure")
            return

        field = ti.field_by_name(field_name)
        if field is None or field.type != "int":
            self._last_command_status = "failure"
            self._log(self._current_line, "failure")
            return

        result = self.file_idx.range_search(type_name, field_name, low, high)
        for record in result.records:
            self._write_output(self._format_record(record))
        # Type exists and field is valid int → always success regardless of record count
        self._last_command_status = "success"
        self._log(self._current_line, "success")

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
        inner_line = " ".join(tokens)

        # Get execution plan estimate before running the command
        plan = self.file_idx.estimate_command(tokens)

        # Snapshot stats before execution
        disk_before = self.disk.get_stats()
        buf_before = self.buffer.get_stats()
        fim_before = self.file_idx.get_stats()

        # Execute inner command: capture its output and suppress its log entry
        saved_line = self._current_line
        self._current_line = inner_line
        self._explain_mode = True
        self._explain_buffer = []
        self._suppress_log = True

        self._dispatch(tokens)

        self._suppress_log = False
        self._explain_mode = False
        captured = list(self._explain_buffer)
        self._explain_buffer = []
        self._current_line = saved_line

        # Compute delta stats
        disk_after = self.disk.get_stats()
        buf_after = self.buffer.get_stats()
        fim_after = self.file_idx.get_stats()

        reads_delta = disk_after["reads"] - disk_before["reads"]
        writes_delta = disk_after["writes"] - disk_before["writes"]
        hits_delta = buf_after["hits"] - buf_before["hits"]
        misses_delta = buf_after["misses"] - buf_before["misses"]
        pages_delta = fim_after["pages_accessed"] - fim_before["pages_accessed"]

        # Write formatted explain block to output.txt
        self._write_output("--- PLAN ---")
        self._write_output(f"Query: {inner_line}")
        self._write_output(f"Strategy: {plan.strategy}")
        self._write_output(f"Estimated I/O: {plan.estimated_io}")
        self._write_output("--- RESULT ---")
        for result_line in captured:
            self._write_output(result_line)
        self._write_output("--- STATS ---")
        self._write_output(f"Actual I/O: {reads_delta} reads, {writes_delta} writes")
        self._write_output(f"Buffer Hits: {hits_delta}")
        self._write_output(f"Buffer Misses: {misses_delta}")
        self._write_output(f"Pages Scanned: {pages_delta}")

        self._log(self._current_line, self._last_command_status)

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
        try:
            disk_stats = self.disk.get_stats()
            buf_stats = self.buffer.get_stats()
            fim_stats = self.file_idx.get_stats()

            hit_rate = buf_stats["hit_rate"] * 100

            lines = [
                "=== STATISTICS ===",
                f"Disk I/O: {disk_stats['reads']} reads, {disk_stats['writes']} writes",
                (
                    f"Buffer Pool: {buf_stats['requests']} requests, "
                    f"{buf_stats['hits']} hits, {buf_stats['misses']} misses "
                    f"({hit_rate:.1f}% hit rate)"
                ),
                f"Evictions: {buf_stats['evictions']} ({buf_stats['dirty_writebacks']} dirty writebacks)",
                f"Index: {fim_stats['index_strategy']}, {fim_stats['index_nodes_visited']} nodes visited",
                f"Records: {fim_stats['records_scanned']} scanned, {fim_stats['records_returned']} returned",
            ]

            with open(self._stats_path, "w", encoding="ascii") as f:
                f.write("\n".join(lines) + "\n")
            self._last_command_status = "success"
        except OSError:
            self._last_command_status = "failure"

    def _cmd_stats_reset(self) -> None:
        """Reset all layer counters via their reset_stats() methods."""
        try:
            self.disk.reset_stats()
            self.buffer.reset_stats()
            self.file_idx.reset_stats()
            self._last_command_status = "success"
        except Exception:
            self._last_command_status = "failure"

    # ─── Output / log helpers ─────────────────────────────────────────────────

    def _write_output(self, text: str) -> None:
        """
        Append text (with newline) to output.txt.
        In explain mode, captures into _explain_buffer instead.
        """
        if self._explain_mode:
            self._explain_buffer.append(text)
            return
        with open(self._output_path, "a", encoding="ascii") as f:
            f.write(text + "\n")

    def _log(self, command: str, status: str) -> None:
        """
        Append one CSV row to log.csv:
          <unix_timestamp>,<command_string>,<status>
        status is "success" or "failure".
        Suppressed when called from within _cmd_explain (inner command execution).
        """
        if self._suppress_log:
            return
        ts = int(time.time())
        with open(self._log_path, "a", encoding="ascii") as f:
            f.write(f"{ts},{command},{status}\n")

    def _format_record(self, values: list) -> str:
        """Format a record's field values as a space-separated string."""
        return " ".join(str(v) for v in values)
