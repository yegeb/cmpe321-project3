import argparse
import random
import sys


TYPE_NAME = "benchtype"
FIELDS = [
    ("id", "int"),
    ("name", "str"),
    ("age", "int"),
    ("city", "str"),
]
PRIMARY_KEY_ORDER = 1
CREATE_TYPE_LINE = (
    f"create type {TYPE_NAME} 4 {PRIMARY_KEY_ORDER} "
    "id int name str age int city str"
)


def make_record_values(record_id: int) -> list[str]:
    return [
        str(record_id),
        f"Name{record_id}",
        str(20 + (record_id % 50)),
        f"City{record_id % 100}",
    ]


def emit(line: str) -> None:
    print(line)


def emit_create_record(record_id: int) -> None:
    values = make_record_values(record_id)
    emit(f"create record {TYPE_NAME} " + " ".join(values))


def emit_initial_dataset(record_count: int) -> None:
    emit(CREATE_TYPE_LINE)
    for record_id in range(1, record_count + 1):
        emit_create_record(record_id)


def sequential_mode(record_count: int, query_count: int) -> None:
    emit_initial_dataset(record_count)
    for _ in range(query_count):
        emit(f"range_search {TYPE_NAME} age 0 1000")


def random_mode(record_count: int, query_count: int, rng: random.Random) -> None:
    emit_initial_dataset(record_count)
    for _ in range(query_count):
        record_id = rng.randint(1, record_count)
        emit(f"search record {TYPE_NAME} {record_id}")


def range_mode(record_count: int, query_count: int, rng: random.Random) -> None:
    emit_initial_dataset(record_count)
    for _ in range(query_count):
        low = rng.randint(20, 60)
        high = low + rng.randint(0, 15)
        emit(f"range_search {TYPE_NAME} age {low} {high}")


def mixed_mode(record_count: int, query_count: int, rng: random.Random) -> None:
    emit_initial_dataset(record_count)

    existing_ids = set(range(1, record_count + 1))
    deleted_ids: list[int] = []
    next_record_id = record_count + 1

    for _ in range(query_count):
        operation = rng.choice(["search", "insert", "delete"])

        if operation == "search":
            if not existing_ids:
                emit_create_record(next_record_id)
                existing_ids.add(next_record_id)
                next_record_id += 1
                continue
            record_id = rng.choice(sorted(existing_ids))
            emit(f"search record {TYPE_NAME} {record_id}")
            continue

        if operation == "insert":
            emit_create_record(next_record_id)
            existing_ids.add(next_record_id)
            next_record_id += 1
            continue

        if not existing_ids:
            emit_create_record(next_record_id)
            existing_ids.add(next_record_id)
            next_record_id += 1
            continue

        record_id = rng.choice(sorted(existing_ids))
        emit(f"delete record {TYPE_NAME} {record_id}")
        existing_ids.remove(record_id)
        deleted_ids.append(record_id)


def main():
    parser = argparse.ArgumentParser(
        description="Generate CMPE321 Project 3 workloads."
    )
    parser.add_argument(
        "--mode",
        required=True,
        choices=["sequential", "random", "range", "mixed"],
    )
    parser.add_argument("--records", type=int, required=True)
    parser.add_argument("--queries", type=int, required=True)
    parser.add_argument("--seed", type=int, default=321)
    args = parser.parse_args()

    if args.records < 1:
        parser.error("--records must be at least 1")
    if args.queries < 0:
        parser.error("--queries must be non-negative")

    rng = random.Random(args.seed)

    if args.mode == "sequential":
        sequential_mode(args.records, args.queries)
    elif args.mode == "random":
        random_mode(args.records, args.queries, rng)
    elif args.mode == "range":
        range_mode(args.records, args.queries, rng)
    else:
        mixed_mode(args.records, args.queries, rng)


if __name__ == "__main__":
    main()
