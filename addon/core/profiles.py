"""Load and match field-mapping profiles.

A profile is the *only* place a deck's real field names live. Matching a profile to a
notetype is a **suggestion**; the user can always override it, and a profile that does not
cleanly fit is refused rather than force-fitted.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

from .role_schema import RoleMapping, ValidationResult

__all__ = [
    "Profile",
    "MatchQuality",
    "ProfileMatch",
    "load_profiles",
    "load_profile_file",
    "match_profile",
    "BUILTIN_PROFILE_DIR",
]

BUILTIN_PROFILE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "profiles")


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


def load_profiles(directory: str = BUILTIN_PROFILE_DIR) -> List[Profile]:
    if not os.path.isdir(directory):
        return []
    out: List[Profile] = []
    for name in sorted(os.listdir(directory)):
        if name.endswith(".json"):
            out.append(load_profile_file(os.path.join(directory, name)))
    return out


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
    live_names = [name for _, name in sorted(live_fields)]

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
