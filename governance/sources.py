"""Manually reviewed operational state, not a claim about threat actor activity."""
from dataclasses import dataclass

STATUSES = frozenset({"ACTIVE", "PAUSED", "UNAVAILABLE", "RETIRED"})


@dataclass(frozen=True)
class Source:
    name: str
    module: str
    status: str
    reason: str
    review_note: str


SOURCES = (
    Source("bitlock", "crawling.bitlock_crawler", "ACTIVE", "Current scheduled source",
           "Review availability and collection scope manually before enabling."),
    Source("gunra", "crawling.gunra_crawler", "UNAVAILABLE", "Excluded from scheduled collection",
           "Keep offline parser fixtures; manual review required before reactivation."),
    Source("black_shrantac", "crawling.Black_Shrantac_crawler", "UNAVAILABLE", "Excluded from scheduled collection",
           "Keep offline parser fixtures; manual review required before reactivation."),
    Source("dragonforce", "crawling.dragonforce_crawler", "UNAVAILABLE", "Excluded from scheduled collection",
           "Keep offline parser fixtures; manual review required before reactivation."),
)


def validate_registry(registry=None):
    rows = SOURCES if registry is None else registry
    names, modules = set(), set()
    for row in rows:
        if (not isinstance(row, Source) or row.status not in STATUSES or not row.reason or not row.review_note
                or row.name in names or row.module in modules or not row.module.startswith("crawling.")
                or not all(part.isidentifier() for part in row.module.split("."))):
            raise ValueError("invalid_source_registry")
        names.add(row.name)
        modules.add(row.module)
    return tuple(rows)


def active_modules():
    return tuple(row.module for row in validate_registry() if row.status == "ACTIVE")
