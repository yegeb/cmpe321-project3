# CMPE321 Project 3

This project implements a modular DBMS engine with four layers:

- `disk_space_manager/`
- `buffer_manager/`
- `file_index_manager/`
- `query_processor/`

The system is executed only through `archive.py`, which reads a configuration
file, builds the layer stack, and processes an input workload.

## Run

```bash
python3 archive.py config.json input.txt
```

All output files are written to the same directory as `archive.py`:

- `output.txt`
- `stats_output.txt`
- `log.csv`

## Configuration

Example `config.json`:

```json
{
  "page_size": 4096,
  "max_records_per_page": 10,
  "buffer_pool_size": 16,
  "replacement_policy": "LRU",
  "index_strategy": "bplus_tree"
}
```

Supported configuration fields:

- `page_size`: bytes per page
- `max_records_per_page`: up to 10
- `buffer_pool_size`: buffer frame count
- `replacement_policy`: `LRU` or `MRU`
- `index_strategy`: `heap_scan`, `hash_index`, or `bplus_tree`

## Supported Commands

- `create type <type-name> <num-fields> <primary-key-order> <field1-name> <field1-type> ...`
- `create record <type> <v1> <v2> ...`
- `delete record <type> <pk-value>`
- `search record <type> <pk-value>`
- `range_search <type> <field> <low> <high>`
- `explain <any DML command>`
- `stats`
- `stats reset`

## Workload Generator

The repository includes `workload_generator.py` for experiment input generation.

Usage:

```bash
python3 workload_generator.py --mode MODE --records N --queries Q > workload.txt
```

Modes:

- `sequential`: insert `N` records, then `Q` full-table range scans
- `random`: insert `N` records, then `Q` random equality searches
- `range`: insert `N` records, then `Q` random range queries on an integer field
- `mixed`: insert `N` records, then `Q` mixed `search` / `insert` / `delete` operations

Optional:

- `--seed`: random seed for reproducible workload generation

Example:

```bash
python3 workload_generator.py --mode random --records 1000 --queries 200 --seed 321 > workload.txt
```

## Notes

- String values produced by the workload generator are alphanumeric only.
- The generator always creates a type with 6 fields, including 3 integer fields.
- `record.txt` contains reproducibility command templates for the required experiments.
