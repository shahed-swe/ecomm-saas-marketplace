"""Emit the theme document JSON Schema + presets for TS/Dart codegen (ADR 0012)."""

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.modules.theme.presets import PRESETS, preset_document  # noqa: E402
from app.modules.theme.schema import ThemeDocument  # noqa: E402

out = pathlib.Path(__file__).resolve().parents[3] / "packages" / "theme-schema"
(out / "presets").mkdir(parents=True, exist_ok=True)
schema = ThemeDocument.model_json_schema(by_alias=True)
(out / "theme-document.schema.json").write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n")
for key in PRESETS:
    doc = preset_document(key).model_dump(mode="json", by_alias=True)
    (out / "presets" / f"{key}.json").write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n")
print("theme schema exported")
