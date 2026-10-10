"""Build the public criterion registry from reviewed, processed source matrices."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
MATRICES = (
    ("property", ROOT / "docs/criteria/property.json"),
    ("social", ROOT / "docs/criteria/social.json"),
    ("environment", ROOT / "docs/criteria/environment.json"),
)
OUTPUT = ROOT / "contracts/criteria_registry.json"
NUMERIC_OPERATORS = {"min", "max", "equals", "in"}
TEXT_OPERATORS = {"equals", "in", "contains"}
ARRAY_OPERATORS = {"contains", "in"}
BOOLEAN_OPERATORS = {"equals", "in"}


def value_type(raw: str, key: str) -> tuple[str, list[str]]:
    if key == "offer.price_per_sqm_rub":
        return "ratio", []
    kind = raw.removesuffix("|null")
    if kind == "integer":
        return "integer", []
    if kind in {"decimal", "number"}:
        return "decimal", []
    if kind == "boolean":
        return "boolean", []
    if kind.startswith("array<string>") or kind.startswith("array<cash|"):
        return "string_array", []
    if kind.startswith("array<object>"):
        return "object_array", []
    if kind == "object":
        return "object", []
    if kind == "string":
        return "string", []
    if kind == "enum":
        return "enum", []
    if "|" in kind and not kind.startswith("{"):
        return "enum", [v for v in kind.split("|") if v not in {"string", "null"}]
    return "string", []


def allowed_operators(kind: str, listed: list[str]) -> list[str]:
    supported = {
        "integer": NUMERIC_OPERATORS,
        "decimal": NUMERIC_OPERATORS,
        "ratio": NUMERIC_OPERATORS,
        "boolean": BOOLEAN_OPERATORS,
        "string": TEXT_OPERATORS,
        "enum": TEXT_OPERATORS,
        "string_array": ARRAY_OPERATORS,
        "object_array": set(),
        "object": set(),
    }[kind]
    return [op for op in listed if op in supported]


def availability(value: Any) -> str:
    text = str(value).casefold()
    if any(word in text for word in ("not_documented", "blocked", "unverified")):
        return "blocked"
    if any(word in text for word in ("conditional", "entitled", "authorized", "requires")):
        return "conditional"
    return "source_dependent"


def build_registry() -> dict[str, Any]:
    criteria: list[dict[str, Any]] = []
    seen: set[str] = set()
    for group, path in MATRICES:
        matrix = json.loads(path.read_text(encoding="utf-8"))
        for source in matrix["criteria"]:
            key = source["key"]
            if key in seen:
                raise ValueError(f"Duplicate criterion key: {key}")
            seen.add(key)
            kind, enum_values = value_type(source["type"], key)
            coverage_raw = source.get("coverage_requirement")
            coverage = coverage_raw if coverage_raw in {"none", "source_scope", "complete"} else (
                "complete" if coverage_raw else "none"
            )
            operators = allowed_operators(kind, source.get("filter_operators", []))
            descriptor = {
                "key": key,
                "label_ru": source.get("label_ru") or key,
                "group": group,
                "type": source["type"],
                "value_type": kind,
                "unit": source.get("unit"),
                "scope": source["scope"],
                "operators": operators,
                "enum_values": enum_values,
                "availability": availability(source.get("automatic_feasibility", "")),
                "filterable": bool(operators),
                "coverage_requirement": coverage,
                "coverage_note": coverage_raw if coverage_raw not in {None, "none", "source_scope", "complete"} else None,
                "source_ids": source.get("sources", source.get("source", [])),
                "evidence": source.get("evidence"),
                "unknown_policy": source.get("unknown_policy", source.get("unknown")),
                "refresh": source.get("refresh"),
            }
            criteria.append(descriptor)
    return {"version": "1.1", "criteria": sorted(criteria, key=lambda row: row["key"])}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Fail if the committed registry differs from source matrices")
    args = parser.parse_args()
    registry = build_registry()
    content = json.dumps(registry, ensure_ascii=False, indent=2) + "\n"
    if args.check:
        if not OUTPUT.is_file() or OUTPUT.read_text(encoding="utf-8") != content:
            raise SystemExit("Criterion registry drift: run contracts/build_registry.py")
        print(f"PASS: {len(registry['criteria'])} registry criteria match processed matrices")
    else:
        OUTPUT.write_text(content, encoding="utf-8")
        print(f"Wrote {len(registry['criteria'])} criteria to {OUTPUT}")


if __name__ == "__main__":
    main()
