"""Print the templates a profile generates, without Anki.

    python tools/preview_templates.py [profile-id] [--audio]

Handy for eyeballing layout changes and for pasting into a bug report. Runs on stock
Python because ``addon.core`` is Anki-free by design.
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from addon.core.profiles import load_profiles  # noqa: E402
from addon.core.template_generator import TemplateOptions, generate_templates  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("profile", nargs="?", default="core2000")
    parser.add_argument("--audio", action="store_true", help="reference audio fields (M5 behaviour)")
    parser.add_argument("--css", action="store_true", help="also print the generated CSS block")
    args = parser.parse_args()

    profiles = {p.id: p for p in load_profiles()}
    profile = profiles.get(args.profile)
    if profile is None:
        print("no such profile: %s (have: %s)" % (args.profile, ", ".join(sorted(profiles))))
        return 1

    mapping = profile.to_mapping()
    result = generate_templates(mapping, options=TemplateOptions(include_audio=args.audio))

    print("profile   : %s  (%s)" % (profile.id, profile.title))
    print("notetype  : %s" % mapping.notetype_name)
    print("languages : target=%s  native=%s" % (mapping.target_language, mapping.native_language))
    print("template  : %s" % result.template_name)
    print()
    print("=" * 70)
    print("FRONT")
    print("=" * 70)
    print(result.front_html)
    print()
    print("=" * 70)
    print("BACK")
    print("=" * 70)
    print(result.back_html)
    print()
    print("referenced fields (%d): %s" % (len(result.referenced_fields), ", ".join(result.referenced_fields)))
    unmapped = [b.name for b in mapping.unmapped()]
    hidden = [b.name for b in mapping.fields if b.hidden]
    print("unmapped & shown (%d): %s" % (len(unmapped), ", ".join(unmapped) or "-"))
    print("hidden by profile (%d): %s" % (len(hidden), ", ".join(hidden) or "-"))

    if args.css:
        print()
        print("=" * 70)
        print("GENERATED CSS")
        print("=" * 70)
        print(result.css)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
