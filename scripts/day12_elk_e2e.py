"""PC-only synthetic fixture writer/checker; requires an isolated test DB/prefix."""
import argparse
from pathlib import Path
import sys

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from elk.config import ElkError, load_config, mongo_uri
from elk.e2e import guard, seed, update, verify_fixture
from elk.http import Http
from elk.mongo import mongo_database


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("seed", "update", "verify"))
    parser.add_argument("--database", required=True)
    parser.add_argument("--revision", type=int, choices=(1, 2, 3), default=1)
    parser.add_argument("--wait", type=int, default=60)
    args = parser.parse_args(argv)
    try:
        if not 0 <= args.wait <= 300:
            raise ElkError("invalid_wait")
        config = load_config()
        # Validate both guards before constructing any HTTP or Mongo client.
        guard(config, args.database)
        if args.action == "update" and args.revision not in {2, 3}:
            raise ElkError("invalid_revision", "E2E")
        with mongo_database(config, mongo_uri()) as database:
            if args.action == "seed":
                seed(config, args.database, database)
                print("[PASS] synthetic initial records in three collections prepared (no Telegram)")
            elif args.action == "update":
                update(config, args.database, database, args.revision)
                print(f"[PASS] synthetic revision={args.revision} prepared (no Telegram)")
            else:
                verify_fixture(config, args.database, database,
                               Http(config.elasticsearch, "Elasticsearch"), args.revision, wait=args.wait)
        return 0
    except ElkError as error:
        print(f"[FAIL] {error.service}: {error.category}")
        print("DAY12_ELK_E2E: FAIL")
        return 1


if __name__ == "__main__":
    sys.exit(main())
