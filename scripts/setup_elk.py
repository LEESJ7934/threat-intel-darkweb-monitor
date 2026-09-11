"""Install Day 12 templates and data views; explicitly run on the user's PC."""
import argparse
from pathlib import Path
import sys

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from elk.config import ElkError, load_config
from elk.http import Http
from elk.setup import setup


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wait", type=int, default=60, help="Readiness wait seconds (0-300)")
    args = parser.parse_args(argv)
    try:
        if not 0 <= args.wait <= 300:
            raise ElkError("invalid_wait")
        config = load_config()
        setup(config, Http(config.elasticsearch, "Elasticsearch"),
              Http(config.kibana, "Kibana"), wait=args.wait)
        return 0
    except ElkError as error:
        print(f"[FAIL] {error.service}: {error.category}")
        print("DAY12_ELK_SETUP: FAIL")
        return 1


if __name__ == "__main__":
    sys.exit(main())
