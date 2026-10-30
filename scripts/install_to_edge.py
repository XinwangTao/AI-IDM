#!/usr/bin/env python3
"""Sync browser-extension/ into the copy Edge actually loads.

Edge's "Load unpacked" ends up running a snapshot under
    %LOCALAPPDATA%\\Microsoft\\Edge\\User Data\\<Profile>\\UnpackedExtensions\\ai-idm-extension_*
so editing the repo and clicking Reload in edge://extensions does nothing. This copies the
current files over that snapshot (extension ID stays the same, so the token/port settings
survive) and tells you to reload.

Usage:
    python scripts/install_to_edge.py            # sync every matching unpacked copy
    python scripts/install_to_edge.py --dry-run  # show what would be copied
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXT = ROOT / "browser-extension"
MARKER = "AI-IDM"          # manifest.name of our extension

INCLUDE_SUFFIXES = {".json", ".js", ".html", ".css", ".png"}
EXCLUDE_NAMES = {"latest.json"}


def candidates() -> list[Path]:
    local = Path(os.environ.get("LOCALAPPDATA", ""))
    bases = [
        local / "Microsoft" / "Edge" / "User Data",
        local / "Google" / "Chrome" / "User Data",
    ]
    found = []
    for base in bases:
        if not base.is_dir():
            continue
        for path in base.glob("*/UnpackedExtensions/*"):
            manifest = path / "manifest.json"
            if not manifest.is_file():
                continue
            try:
                data = json.loads(manifest.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            if MARKER in str(data.get("name", "")):
                found.append(path)
    return found


def source_files() -> list[Path]:
    files = []
    for path in sorted(EXT.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(EXT)
        if any(part in {"node_modules", "__pycache__", ".git"} for part in rel.parts):
            continue
        if path.name in EXCLUDE_NAMES or path.suffix.lower() not in INCLUDE_SUFFIXES:
            continue
        files.append(path)
    return files


def version_of(folder: Path) -> str:
    try:
        return str(json.loads((folder / "manifest.json").read_text(encoding="utf-8"))["version"])
    except (OSError, json.JSONDecodeError, KeyError):
        return "?"


def sync(target: Path, files: list[Path], dry_run: bool) -> None:
    before = version_of(target)
    source_version = version_of(EXT)
    print(f"[target] {target}")
    print(f"         version {before} -> {source_version}")
    if dry_run:
        for path in files:
            print(f"         would copy {path.relative_to(EXT).as_posix()}")
        return

    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = Path(os.environ.get("TEMP", ".")) / f"edge-ai-idm-ext-backup-{stamp}"
    shutil.copytree(target, backup)
    print(f"[backup] {backup}")

    for path in files:
        rel = path.relative_to(EXT)
        dest = target / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, dest)
    print(f"[ok] copied {len(files)} files")


def main() -> None:
    ap = argparse.ArgumentParser(description="Sync the extension into Edge's unpacked copy.")
    ap.add_argument("--dry-run", action="store_true", help="print the plan, change nothing")
    ap.add_argument("--target", default="", help="sync to this folder instead of auto-detecting")
    args = ap.parse_args()

    if not (EXT / "manifest.json").is_file():
        print(f"[FAIL] {EXT / 'manifest.json'} not found")
        sys.exit(1)

    files = source_files()
    if not files:
        print("[FAIL] nothing to copy")
        sys.exit(1)

    targets = [Path(args.target)] if args.target else candidates()
    if not targets:
        print("[FAIL] no unpacked AI-IDM extension found under Edge/Chrome profiles.")
        print("       Install it once via edge://extensions -> Load unpacked, or pass --target.")
        sys.exit(1)

    for target in targets:
        if not target.is_dir():
            print(f"[FAIL] {target} is not a directory")
            sys.exit(1)
        sync(target, files, args.dry_run)

    if not args.dry_run:
        print("\nNext: open edge://extensions and click Reload on AI-IDM.")


if __name__ == "__main__":
    main()
