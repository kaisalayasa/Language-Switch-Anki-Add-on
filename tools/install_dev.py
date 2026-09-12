"""Install this repo into Anki's addons folder for development.

    python tools/install_dev.py            # copy
    python tools/install_dev.py --link     # symlink/junction (edit in place)
    python tools/install_dev.py --remove

Anki must be closed. Addons live in %APPDATA%/Anki2/addons21 and are shared across
profiles -- but the addon only *acts* when you invoke it, so installing it is safe; which
collection it touches is decided by which profile you have open.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys

PACKAGE = "deck_direction_converter"
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCE = os.path.join(REPO, "addon")


def addons_dir() -> str:
    if sys.platform.startswith("win"):
        base = os.environ.get("APPDATA", os.path.expanduser("~"))
        return os.path.join(base, "Anki2", "addons21")
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Application Support/Anki2/addons21")
    return os.path.expanduser("~/.local/share/Anki2/addons21")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--link", action="store_true", help="symlink instead of copying")
    parser.add_argument("--remove", action="store_true", help="uninstall")
    args = parser.parse_args()

    target = os.path.join(addons_dir(), PACKAGE)
    if not os.path.isdir(addons_dir()):
        print("addons folder not found: %s" % addons_dir())
        return 1

    if os.path.islink(target):
        os.unlink(target)
    elif os.path.isdir(target):
        shutil.rmtree(target)

    if args.remove:
        print("removed %s" % target)
        return 0

    if args.link:
        try:
            os.symlink(SOURCE, target, target_is_directory=True)
        except OSError:
            # Windows without developer mode: fall back to a directory junction.
            subprocess.check_call(["cmd", "/c", "mklink", "/J", target, SOURCE])
        print("linked  %s -> %s" % (target, SOURCE))
    else:
        shutil.copytree(
            SOURCE, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "meta.json")
        )
        print("copied  %s -> %s" % (SOURCE, target))

    print("\nRestart Anki, switch to your test profile, then:")
    print("  Tools > Convert deck language direction…")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
