"""Defaults to DRY RUN. Deletion requires --apply and two exact database names."""
import argparse
from pathlib import Path
import sys

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from governance import audit
from governance.policy import PolicyError
from governance.retention import confirm_apply, execute_retention
from governance.runtime import load_config, mongo_database, mongo_uri


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--database")
    parser.add_argument("--confirm-database")
    args = parser.parse_args(argv)
    print("APPLY" if args.apply else "DRY RUN - no documents will be deleted")
    try:
        config = load_config()
        if args.apply:
            confirm_apply(config.database, args.database, args.confirm_database)
        elif args.database is not None and args.database != config.database:
            raise PolicyError("database_mismatch")
        with mongo_database(config, mongo_uri()) as database:
            report = execute_retention(database, config.rules, apply=args.apply, configured_name=config.database,
                                       database_name=args.database, confirmation=args.confirm_database)
        for collection, counts in report.items():
            print(f"[INFO] {collection} expired={counts['expired']} missing={counts['missing']} "
                  f"invalid={counts['invalid']} future={counts['future']} deleted={counts.get('deleted', 0)}")
        print("DAY13_RETENTION: PASS")
        return 0
    except Exception:
        audit.emit("retention_execution", category="retention", result="error")
        print("[FAIL] retention_configuration_or_database_error")
        if args.apply:
            print("[INFO] If deletion had started, some collections may already have been processed.")
        print("DAY13_RETENTION: FAIL")
        return 1


if __name__ == "__main__":
    sys.exit(main())
