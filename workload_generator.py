import argparse


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", required=True)
    parser.add_argument("--records", type=int, required=True)
    parser.add_argument("--queries", type=int, required=True)
    parser.parse_args()


if __name__ == "__main__":
    main()
