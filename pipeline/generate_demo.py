"""Deterministic, explicitly illustrative catalogue for local development."""

from __future__ import annotations

import argparse
import json
import random
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "data" / "demo" / "catalog.json"
SNAPSHOT_AT = "2026-10-09T00:00:00Z"
BOUNDARY_VERSION = "demo-grid-v1"

DISTRICT_SEEDS = [
    ("tverskoy", "Тверской", 37.617, 55.770),
    ("presnensky", "Пресненский", 37.566, 55.758),
    ("basmanny", "Басманный", 37.673, 55.769),
    ("khamovniki", "Хамовники", 37.583, 55.732),
    ("tagansky", "Таганский", 37.663, 55.738),
    ("donskoy", "Донской", 37.603, 55.707),
    ("sokol", "Сокол", 37.515, 55.803),
    ("sokolniki", "Сокольники", 37.680, 55.792),
    ("marino", "Марьино", 37.747, 55.651),
    ("cheryomushki", "Черёмушки", 37.571, 55.664),
]


def generate() -> dict:
    rng = random.Random(50909)
    districts = [
        {
            "district_id": f"{BOUNDARY_VERSION}:{slug}",
            "name": name,
            "boundary_version": BOUNDARY_VERSION,
            "geometry": None,
            "location": [lon, lat],
            "source": "synthetic district label and point; no boundary polygon",
        }
        for slug, name, lon, lat in DISTRICT_SEEDS
    ]
    source = {
        "name": "Novostroy synthetic catalogue",
        "url": None,
        "quality": "illustrative",
        "record_mode": "demo",
        "observed_at": SNAPSHOT_AT,
        "source_updated_at": None,
        "rights_status": "demo",
        "attribution": None,
    }
    complexes = []
    offers = []
    for index in range(100):
        district = districts[index % len(districts)]
        base_lon, base_lat = district["location"]
        location = [round(base_lon + rng.uniform(-0.012, 0.012), 6), round(base_lat + rng.uniform(-0.008, 0.008), 6)]
        complex_id = f"demo-complex-{index + 1:03d}"
        facade = index < 3
        complexes.append({
            "complex_id": complex_id,
            "name": f"Демо ЖК {index + 1:03d}",
            "district_id": district["district_id"],
            "location": location,
            "developer": "Синтетический застройщик",
            "source": source,
            "model_quality": "synthetic_facade" if facade else "none",
            "facade_tileset_url": f"/assets/facades/{complex_id}/tileset.json" if facade else None,
            "description": "Вымышленный ЖК для проверки интерфейса; не является объявлением о продаже.",
        })
        for sub in range(10):
            offer_index = index * 10 + sub + 1
            rooms = sub % 5
            area = round(25 + rooms * 14 + rng.uniform(0, 15), 1)
            basis = "conditional_total" if sub == 9 else "asking_total"
            price = int((area * (220000 + index * 2100 + rng.randrange(0, 65000))) // 10000 * 10000)
            offers.append({
                "offer_id": f"demo-offer-{offer_index:04d}",
                "unit_id": None,
                "building_id": None,
                "section_id": None,
                "floor_id": None,
                "complex_id": complex_id,
                "district_id": district["district_id"],
                "rooms": rooms,
                "area_sqm": area,
                "floor": 2 + sub + index % 8,
                "price": {
                    "amount_rub": price,
                    "basis": basis,
                    "observed_at": SNAPSHOT_AT,
                    "conditions": "Условная цена для демо" if basis == "conditional_total" else None,
                },
                "source": source,
                "availability": None,
                "social": {"coverage": "unknown", "index": None},
            })
    return {
        "version": "1",
        "snapshot_id": "demo-2026-10-09-v1",
        "snapshot_at": SNAPSHOT_AT,
        "data_mode": "demo",
        "disclaimer": "Все ЖК, предложения и цены синтетические. Контуры зданий Overture имеют отдельное происхождение.",
        "districts": districts,
        "complexes": complexes,
        "offers": offers,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args()
    payload = generate()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if not args.output.exists() or args.output.read_text(encoding="utf-8") != encoded:
        args.output.write_text(encoded, encoding="utf-8")
    print(f"{args.output}: {len(payload['complexes'])} complexes, {len(payload['offers'])} offers")


if __name__ == "__main__":
    main()
