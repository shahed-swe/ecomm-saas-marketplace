"""Write the OpenAPI schema to packages/api-contract/openapi.json (source for TS + Dart clients)."""

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.core.config import Settings
from app.main import create_app

root = pathlib.Path(__file__).resolve().parents[3]
out = root / "packages" / "api-contract" / "openapi.json"
out.parent.mkdir(parents=True, exist_ok=True)
schema = create_app(Settings(env="test")).openapi()
out.write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n")
print(f"wrote {out.relative_to(root)}")
