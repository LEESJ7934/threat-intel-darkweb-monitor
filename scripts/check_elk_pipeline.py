"""Read-only Mongo/ES count, mapping, liveness and data-view checks."""
import argparse
from pathlib import Path
import sys

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from elk.config import ElkError, load_config, mongo_uri
from elk.http import Http
from elk.mongo import mongo_database
from elk.verify import verify


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wait", type=int, default=60, help="Eventual sync wait seconds (0-300)")
    args = parser.parse_args(argv)
    try:
        if not 0 <= args.wait <= 300:
            raise ElkError("invalid_wait")
        config = load_config()
        with mongo_database(config, mongo_uri()) as database:
            verify(config, Http(config.elasticsearch, "Elasticsearch"),
                   Http(config.kibana, "Kibana"), Http(config.monstache, "Monstache"),
                   database, wait=args.wait)
        return 0
    except ElkError as error:
        print(f"[FAIL] {error.service}: {error.category}")
        print("DAY12_ELK_PIPELINE: FAIL")
        return 1


if __name__ == "__main__":
    sys.exit(main())
