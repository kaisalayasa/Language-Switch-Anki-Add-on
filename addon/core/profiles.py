"""Load and match field-mapping profiles.

A profile is the *only* place a deck's real field names live. Matching a profile to a
notetype is a **suggestion**; the user can always override it, and a profile that does not
cleanly fit is refused rather than force-fitted.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple, Union

from .audio_fields import is_generated_field
from .role_schema import RoleMapping, ValidationResult

__all__ = [
    "Profile",
    "MatchQuality",
    "ProfileMatch",
    "load_profiles",
    "load_profile_file",
    "match_profile",
    "save_profile",
    "slugify",
    "BUILTIN_PROFILE_DIR",
    "USER_PROFILE_DIR",
]

BUILTIN_PROFILE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "profiles")

#: Where a user's own saved mappings live -- inside ``user_files/``, the addon convention
#: (also used by the Piper binary/voice cache) for data that survives an addon update and
#: is never bundled/shared by default. Checked before ``BUILTIN_PROFILE_DIR`` in
#: ``load_profiles()`` so a user's saved/edited profile wins over a shipped one describing
#: the same notetype.
USER_PROFILE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "user_files", "profiles"
)


class MatchQuality:
    """How confident we are that a profile describes a given notetype."""

    EXACT = "exact"          # field list matches the fingerprint exactly
    NAME_ONLY = "name_only"  # notetype name matches but fields differ
    NONE = "none"


@dataclass
class Profile:
    id: str
    title: str
    data: dict

    @property
    def notetype_names(self) -> List[str]:
        binds = self.data.get("binds_to") or {}
        names = list(binds.get("notetype_names") or [])
        declared = self.data.get("notetype")
        if declared and declared not in names:
            names.append(declared)
        return names

    @property
    def fingerprint(self) -> List[str]:
        binds = self.data.get("binds_to") or {}
        fp = binds.get("field_fingerprint")
        if fp:
            return list(fp)
        return [f["name"] for f in sorted(self.data.get("fields", []), key=lambda f: f["ord"])]

    def to_mapping(self, live_fields: Optional[Sequence[Tuple[int, str]]] = None) -> RoleMapping:
        return RoleMapping.from_profile(self.data, live_fields=live_fields)


@dataclass
class ProfileMatch:
    profile: Optional[Profile]
    quality: str
    validation: Optional[ValidationResult] = None

    @property
    def usable(self) -> bool:
        """Only an exact, validating match may be applied without asking the user."""
        return (
            self.profile is not None
            and self.quality == MatchQuality.EXACT
            and self.validation is not None
            and self.validation.ok
        )


def load_profile_file(path: str) -> Profile:
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    return Profile(
        id=data.get("id") or os.path.splitext(os.path.basename(path))[0],
        title=data.get("title") or data.get("id") or os.path.basename(path),
        data=data,
    )


def load_profiles(
    directories: Optional[Union[str, Sequence[str]]] = None,
) -> List[Profile]:
    """Every profile found across ``directories``, in order.

    Defaults to ``(USER_PROFILE_DIR, BUILTIN_PROFILE_DIR)`` -- user profiles first, so that
    when a user has saved/edited a profile describing the same notetype as a shipped one,
    :func:`match_profile`'s first-exact-match search prefers theirs. A missing directory
    (e.g. no profiles saved yet) is skipped rather than treated as an error.
    """
    if directories is None:
        directories = (USER_PROFILE_DIR, BUILTIN_PROFILE_DIR)
    elif isinstance(directories, str):
        directories = (directories,)

    out: List[Profile] = []
    for directory in directories:
        if not os.path.isdir(directory):
            continue
        for name in sorted(os.listdir(directory)):
            if name.endswith(".json"):
                out.append(load_profile_file(os.path.join(directory, name)))
    return out


def slugify(text: str) -> str:
    """A filesystem- and id-safe slug: lowercase, ``[a-z0-9]`` runs joined by ``_``.

    Falls back to ``"profile"`` for input that has no alphanumeric content at all, so a
    caller always gets a usable filename stem.
    """
    slug = re.sub(r"[^a-z0-9]+", "_", text.strip().lower()).strip("_")
    return slug or "profile"


def save_profile(
    mapping: RoleMapping,
    *,
    id: str,
    title: str,
    description: str = "",
    directory: str = USER_PROFILE_DIR,
) -> str:
    """Write ``mapping`` out as a profile JSON file under ``directory``, returning its path.

    Always overwrites an existing file at the same path -- this function has no opinion on
    whether that's wanted; the caller (the UI) decides that policy and confirms with the
    user first if it matters.
    """
    data = mapping.to_profile()
    data["id"] = id
    data["title"] = title
    if description:
        data["description"] = description
    data["version"] = 1
    # Same reasoning as match_profile's: a profile saved off a converted deck must still
    # match the unconverted original it came from, so the addon's own generated fields stay
    # out of the fingerprint that identifies the deck.
    field_names = [f["name"] for f in data["fields"] if not is_generated_field(f["name"])]
    data["binds_to"] = {
        "notetype_names": [mapping.notetype_name] if mapping.notetype_name else [],
        "field_fingerprint": field_names,
    }

    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, slugify(id) + ".json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    return path


def match_profile(
    notetype_name: str,
    live_fields: Sequence[Tuple[int, str]],
    profiles: Optional[Sequence[Profile]] = None,
) -> ProfileMatch:
    """Find a profile describing this notetype.

    ``live_fields`` is ``[(ord, name), ...]`` read from the notetype as it exists now.

    A profile only counts as usable when the field list matches exactly *and* the resulting
    mapping validates against the live notetype. Anything less is handed back with a
    non-exact quality so the caller can route to the mapper UI instead of writing the wrong
    content into every note.
    """
    candidates = list(profiles) if profiles is not None else load_profiles()
    # Fields this addon generated are excluded from the fingerprint: they describe a
    # conversion's output, not the deck's own identity. Without this, converting a deck
    # would change its fingerprint (the clone carries an extra audio field) and its own
    # profile would stop matching the moment it was most needed -- on reopening the
    # converted deck to generate audio for it.
    live_names = [name for _, name in sorted(live_fields) if not is_generated_field(name)]

    by_name = [p for p in candidates if notetype_name in p.notetype_names]
    fallback: Optional[Profile] = by_name[0] if by_name else None

    for profile in candidates:
        if profile.fingerprint == live_names:
            mapping = profile.to_mapping(live_fields=live_fields)
            return ProfileMatch(
                profile=profile,
                quality=MatchQuality.EXACT,
                validation=mapping.validate_against(live_fields),
            )

    if fallback is not None:
        return ProfileMatch(profile=fallback, quality=MatchQuality.NAME_ONLY, validation=None)

    return ProfileMatch(profile=None, quality=MatchQuality.NONE, validation=None)
