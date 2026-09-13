"""Package this repo's addon/ folder into a real, installable .ankiaddon file.

    python tools/build_ankiaddon.py

Produces dist/<package>.ankiaddon -- a zip with manifest.json, __init__.py, etc. at its
root (Anki's required layout: https://addon-docs.ankiweb.net package format), ready to
install via Anki -> Tools -> Add-ons -> Install from file...

This is a small local script rather than the community "anki-addon-builder" (aab) tool
claude.md names, because aab expects the addon package to live under src/<module_name>/
with an addon.json at the repo root, and is built around git-tag-based versioning for
publishing to AnkiWeb. Adopting it would mean restructuring this repo for a public-release
workflow this project isn't committed to yet (the license -- MIT vs GPL -- is still an open
question; see README.md). This script does the one thing needed in the meantime: turn
addon/ into a file Anki can actually install, for real testing outside the dev symlink.

Excludes addon/user_files/ (gitignored, per-machine: the developer's downloaded Piper
binary/voice model and any profiles they saved locally while testing -- a fresh install
should download/build all of that itself, not inherit the packager's machine state) and the
usual __pycache__/meta.json cruft, the same exclusions tools/install_dev.py's copy mode
already uses.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import zipfile
from typing import Tuple

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCE = os.path.join(REPO, "addon")
DIST = os.path.join(REPO, "dist")

_EXCLUDE_DIRS = {"__pycache__", "user_files"}
_EXCLUDE_FILES = {"meta.json"}
_EXCLUDE_SUFFIXES = (".pyc", ".pyo")


def _manifest() -> dict:
    with open(os.path.join(SOURCE, "manifest.json"), "r", encoding="utf-8") as fh:
        return json.load(fh)


def _sanity_check() -> None:
    """Refuse to package something that doesn't even byte-compile."""
    result = subprocess.run(
        [sys.executable, "-m", "compileall", "-q", SOURCE], cwd=REPO
    )
    if result.returncode != 0:
        raise SystemExit("addon/ failed to compile -- fix that before packaging.")


def _iter_files():
    for root, dirs, files in os.walk(SOURCE):
        dirs[:] = [d for d in dirs if d not in _EXCLUDE_DIRS]
        for name in files:
            if name in _EXCLUDE_FILES or name.endswith(_EXCLUDE_SUFFIXES):
                continue
            full = os.path.join(root, name)
            arcname = os.path.relpath(full, SOURCE)
            yield full, arcname


def build() -> Tuple[str, int]:
    _sanity_check()
    manifest = _manifest()
    package = manifest["package"]
    version = manifest.get("human_version", "0.0.0")

    os.makedirs(DIST, exist_ok=True)
    out_path = os.path.join(DIST, "%s-%s.ankiaddon" % (package, version))

    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        count = 0
        for full, arcname in _iter_files():
            zf.write(full, arcname)
            count += 1

    return out_path, count


def main() -> int:
    out_path, count = build()
    size_mb = os.path.getsize(out_path) / (1024 * 1024)
    print("built %s  (%d files, %.1f MB)" % (out_path, count, size_mb))
    print("\nInstall via Anki -> Tools -> Add-ons -> Install from file...")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
