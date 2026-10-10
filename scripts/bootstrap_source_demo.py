"""Build a small, explicitly synthetic city for a fresh source-only clone.

Never run this in the checkout holding the real local asset manifest.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CITY = ROOT / "data/demo/city.synthetic.geojson"


def main() -> None:
    if (ROOT / "assets/manifest.json").exists() or CITY.exists():
        raise SystemExit("Existing demo/asset data found; source-only bootstrap is for a fresh clone")
    city = {
        "type": "FeatureCollection",
        "metadata": {"data_mode": "demo", "attribution": "Synthetic local demo"},
        "features": [
            {
                "type": "Feature", "id": f"synthetic-building-{index}",
                "properties": {"source_id": f"synthetic-building-{index}",
                               "source_release": "synthetic-demo-v1", "height_m": height},
                "geometry": {"type": "Polygon", "coordinates": [[
                    [lon, lat], [lon + 0.00015, lat],
                    [lon + 0.00015, lat + 0.00012], [lon, lat + 0.00012],
                    [lon, lat],
                ]]},
            }
            for index, (lon, lat, height) in enumerate(
                ((37.6160, 55.7500, 12), (37.6164, 55.7502, 18)), start=1
            )
        ],
    }
    CITY.parent.mkdir(parents=True, exist_ok=True)
    CITY.write_text(json.dumps(city, ensure_ascii=False) + "\n", encoding="utf-8")
    subprocess.run([sys.executable, "-m", "pipeline.generate_demo"], cwd=ROOT, check=True)
    subprocess.run([sys.executable, "-m", "pipeline.build_tiles", "--city-input", str(CITY)],
                   cwd=ROOT, check=True)
    manifest_path = ROOT / "assets/manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["city_source_mode"] = "synthetic_demo"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                             encoding="utf-8")
    print("Synthetic source-only demo is ready; it contains no real registry or citywide data")


if __name__ == "__main__":
    main()
