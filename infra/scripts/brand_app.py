#!/usr/bin/env python3
"""Turn one tenant's manifest into the parts of a Flutter app that must be baked into the binary.

Everything that can be fetched at runtime is fetched at runtime; this script only writes what the
stores tie to a binary: the application id, the display name, the launcher icon and the splash.
"""

import argparse
import json
import pathlib
import re
import sys
import urllib.request


def _hex_to_rgb(value: str | None, fallback: tuple[int, int, int]) -> tuple[int, int, int]:
    if not value:
        return fallback
    raw = value.lstrip("#")
    if len(raw) != 6:
        return fallback
    return tuple(int(raw[i : i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def write_icon(app_dir: pathlib.Path, manifest: dict) -> None:
    """A tenant's logo becomes the launcher icon; without one we fall back to a brand-coloured tile."""
    from PIL import Image, ImageDraw

    branding = manifest.get("branding") or {}
    background = _hex_to_rgb(branding.get("primary_color"), (15, 118, 110))
    icon = Image.new("RGBA", (1024, 1024), (*background, 255))
    logo_url = branding.get("icon_url") or manifest.get("logo_url")
    if logo_url:
        try:
            with urllib.request.urlopen(logo_url, timeout=20) as response:  # noqa: S310
                logo = Image.open(response).convert("RGBA")
            logo.thumbnail((720, 720))
            icon.alpha_composite(
                logo, ((1024 - logo.width) // 2, (1024 - logo.height) // 2)
            )
        except Exception as exc:  # pragma: no cover - a missing logo must not fail a release
            print(f"logo unavailable ({exc}); using a plain brand tile", file=sys.stderr)
    else:
        draw = ImageDraw.Draw(icon)
        initial = (manifest.get("app_name") or "S")[0].upper()
        draw.text((512, 470), initial, anchor="mm", fill=(255, 255, 255, 255))
    target = app_dir / "assets" / "branding"
    target.mkdir(parents=True, exist_ok=True)
    icon.save(target / "icon.png")
    icon.resize((512, 512)).save(target / "splash.png")


def set_android_identity(app_dir: pathlib.Path, manifest: dict) -> None:
    gradle = app_dir / "android" / "app" / "build.gradle"
    if not gradle.exists() or not manifest.get("android_package"):
        return
    text = gradle.read_text()
    text = re.sub(
        r'applicationId\s+"[^"]+"', f'applicationId "{manifest["android_package"]}"', text
    )
    gradle.write_text(text)
    strings = app_dir / "android" / "app" / "src" / "main" / "res" / "values" / "strings.xml"
    if strings.exists():
        strings.write_text(
            '<?xml version="1.0" encoding="utf-8"?>\n<resources>\n'
            f'    <string name="app_name">{manifest.get("app_name", "")}</string>\n</resources>\n'
        )


def set_ios_identity(app_dir: pathlib.Path, manifest: dict) -> None:
    plist = app_dir / "ios" / "Runner" / "Info.plist"
    if not plist.exists():
        return
    text = plist.read_text()
    if manifest.get("app_name"):
        text = re.sub(
            r"(<key>CFBundleDisplayName</key>\s*<string>)[^<]*(</string>)",
            rf"\1{manifest['app_name']}\2",
            text,
        )
    plist.write_text(text)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--app", required=True)
    args = parser.parse_args()
    manifest = json.loads(pathlib.Path(args.manifest).read_text())
    app_dir = pathlib.Path(args.app)
    write_icon(app_dir, manifest)
    set_android_identity(app_dir, manifest)
    set_ios_identity(app_dir, manifest)
    print(f"branded {manifest.get('app_name')} ({manifest.get('slug')})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
