"""Read-only Mongo retention/field checks, with optional read-only ES exposure check."""
import argparse
from pathlib import Path
import sys

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from elk.config import ElkError
from governance.policy import PolicyError
from governance.retention import inspect_retention
from governance.runtime import load_config, mongo_database, mongo_uri
from governance.sources import validate_registry


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-elk", action="store_true")
    args = parser.parse_args(argv)
    try:
        config = load_config()
        validate_registry()
        with mongo_database(config, mongo_uri()) as database:
            report = inspect_retention(database, config.rules, check_fields=True)
        passed = True
        for collection, counts in report.items():
            print(f"[PASS] {collection} retention policy readable")
            print(f"[INFO] {collection} expired={counts['expired']} missing={counts['missing']} "
                  f"invalid={counts['invalid']} future={counts['future']}")
            if counts["prohibited"]:
                passed = False
                print(f"[FAIL] {collection} prohibited_top_level_documents={counts['prohibited']}")
        if passed:
            print("[PASS] no prohibited top-level fields detected")
        print("[PASS] source registry valid")
        if args.check_elk:
            from elk.config import load_config as elk_config
            from elk.http import Http
            from governance.elk_check import check_exposure
            settings = elk_config()
            check_exposure(settings, Http(settings.elasticsearch, "Elasticsearch"))
            print("[PASS] ELK mapping and private source field checks")
        print("DAY13_GOVERNANCE_CHECK: " + ("PASS" if passed else "FAIL"))
        return 0 if passed else 1
    except (ElkError, PolicyError, ValueError):
        print("[FAIL] governance_configuration_or_service_error")
        print("DAY13_GOVERNANCE_CHECK: FAIL")
        return 1
    except Exception:
        print("[FAIL] governance_check_error")
        print("DAY13_GOVERNANCE_CHECK: FAIL")
        return 1


if __name__ == "__main__":
    sys.exit(main())
