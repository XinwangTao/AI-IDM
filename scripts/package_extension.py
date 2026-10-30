#!/usr/bin/env python3
"""Package the browser extension and refresh latest.json.

Why this exists: the repo shipped v1.12.5 while the packed extension inside it was still
1.2.0 -- extension 1.3 .. 1.11 were never released. This script makes the version bump a
hard gate instead of something to remember.

Usage:
    python scripts/package_extension.py                 # verify + build zip + update latest.json
    python scripts/package_extension.py --check         # verify only (manifest <-> CHANGELOG)
    python scripts/package_extension.py --store-url URL --zip-url URL --notes "..."

Output: dist/ai-idm-extension-<version>.zip  (dist/ is gitignored)
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXT = ROOT / "browser-extension"
LATEST = ROOT / "latest.json"
CHANGELOG = ROOT / "CHANGELOG.md"

# Only these go into the store package; a stray .pem / latest.json / test dir must never ship.
INCLUDE_SUFFIXES = {".json", ".js", ".html", ".css", ".png"}
EXCLUDE_NAMES = {"latest.json"}
EXCLUDE_SUFFIXES = {".pem", ".map", ".py"}


def fail(msg: str) -> "NoReturn":  # type: ignore[name-defined]
    print(f"[FAIL] {msg}")
    sys.exit(1)


def read_manifest() -> dict:
    path = EXT / "manifest.json"
    if not path.is_file():
        fail(f"missing {path}")
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        fail(f"manifest.json is not valid JSON: {exc}")
    if "version" not in manifest:
        fail("manifest.json has no version field")
    return manifest


def check_changelog(version: str) -> None:
    if not CHANGELOG.is_file():
        fail("CHANGELOG.md not found")
    text = CHANGELOG.read_text(encoding="utf-8")
    # Accept both "## Extension 1.12.0 - date" and "## [1.12.0] - date"
    pattern = re.compile(
        rf"^##\s+(?:Extension\s+)?\[?{re.escape(version)}\]?\b", re.MULTILINE)
    if not pattern.search(text):
        fail(
            f"CHANGELOG.md has no entry for {version}. "
            "Add '## Extension {version} - <date>' before packaging."
        )


def collect_files() -> list[Path]:
    files = []
    for path in sorted(EXT.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(EXT)
        if any(part in {"node_modules", "__pycache__", ".git"} for part in rel.parts):
            continue
        if path.name in EXCLUDE_NAMES or path.suffix.lower() in EXCLUDE_SUFFIXES:
            continue
        if path.suffix.lower() not in INCLUDE_SUFFIXES:
            continue
        files.append(path)
    return files


def check_referenced(manifest: dict, files: list[Path]) -> None:
    """Every file the manifest points at must be inside the package."""
    have = {p.relative_to(EXT).as_posix() for p in files}
    wanted = set()
    for key in ("background", "action", "options_ui"):
        block = manifest.get(key) or {}
        for field in ("service_worker", "default_popup", "page"):
            if isinstance(block.get(field), str):
                wanted.add(block[field])
    for script in manifest.get("content_scripts", []):
        wanted.update(script.get("js", []))
    for size, icon in (manifest.get("icons") or {}).items():
        wanted.add(icon)
    for size, icon in ((manifest.get("action") or {}).get("default_icon") or {}).items():
        wanted.add(icon)
    missing = sorted(w for w in wanted if w not in have)
    if missing:
        fail(f"manifest references files that are not in the package: {missing}")


def build_zip(version: str, files: list[Path]) -> Path:
    dist = ROOT / "dist"
    dist.mkdir(exist_ok=True)
    target = dist / f"ai-idm-extension-{version}.zip"
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in files:
            zf.write(path, path.relative_to(EXT).as_posix())
    return target


def update_latest(version: str, store_url: str, zip_url: str, notes: str) -> Path:
    data = {}
    if LATEST.is_file():
        try:
            data = json.loads(LATEST.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = {}
    data["schema"] = 1
    data["version"] = version
    data["published"] = _dt.date.today().isoformat()
    if notes:
        data["notes"] = notes
    data.setdefault("notes", "")
    # Preserve previously published URLs unless explicitly overridden.
    if store_url:
        data["store_url"] = store_url
    data.setdefault("store_url", "")
    if zip_url:
        data["zip_url"] = zip_url
    data.setdefault("zip_url", "")
    LATEST.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return LATEST


def main() -> None:
    ap = argparse.ArgumentParser(description="Verify and package the AI-IDM browser extension.")
    ap.add_argument("--check", action="store_true", help="verify versions only, do not build")
    ap.add_argument("--store-url", default="", help="store listing URL to publish in latest.json")
    ap.add_argument("--zip-url", default="", help="download URL of this zip to publish")
    ap.add_argument("--notes", default="", help="one-line release note for the popup banner")
    args = ap.parse_args()

    manifest = read_manifest()
    version = str(manifest["version"])
    check_changelog(version)
    print(f"[ok] manifest {version} matches CHANGELOG")

    files = collect_files()
    check_referenced(manifest, files)
    if not files:
        fail("no files collected for packaging")

    if args.check:
        print(f"[ok] {len(files)} files would be packaged")
        return

    target = build_zip(version, files)
    print(f"[ok] packed {len(files)} files -> {target.relative_to(ROOT).as_posix()}")
    for path in files:
        print(f"     {path.relative_to(EXT).as_posix()}")

    produced = update_latest(version, args.store_url, args.zip_url, args.notes)
    print(f"[ok] refreshed {produced.name}: version={version}")


if __name__ == "__main__":
    main()
