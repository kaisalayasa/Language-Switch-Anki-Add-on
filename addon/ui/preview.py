"""Open a real, rendered card preview using Anki's own Card Types editor.

``claude.md`` says to reuse ``aqt.clayout.CardLayout`` for preview rather than building a
raw renderer -- and that it's real M3 scope, pulled forward here at the user's explicit
request after M1's plain-text template dump turned out not to answer "what will this
actually look like".

``CardLayout``'s exact constructor is **not** independently verified the way the rest of
this addon's Anki calls are (see ``docs/api-notes.md``): its compiled module targets
Python 3.13 and cannot be introspected from outside a running Anki process. So this module
does what ``claude.md`` asks for exactly this situation -- it says so explicitly and
degrades to a clear, actionable message rather than guessing silently. See
:func:`open_card_layout`.
"""

from __future__ import annotations

import inspect
from typing import Any, List, Optional

__all__ = ["open_card_layout", "CardLayoutUnavailable"]


class CardLayoutUnavailable(RuntimeError):
    """``aqt.clayout.CardLayout`` could not be constructed with any attempted call shape."""


# Progressively simpler attempts, most-featured first. Every Anki version examined for
# this addon (see docs/api-notes.md) has carried this class under this name; what varies
# release to release is which of these keyword arguments it accepts.
_ATTEMPTS = [
    {"ord": 0, "fill_empty": False},
    {"ord": 0},
    {},
]


def open_card_layout(mw: Any, note: Any, *, parent: Any = None) -> Any:
    """Open Anki's Card Types editor on ``note``, rendered with its current templates.

    Raises :class:`CardLayoutUnavailable` with a message pointing at the probe to run,
    rather than crashing on a version mismatch.
    """
    try:
        from aqt.clayout import CardLayout
    except ImportError as exc:
        raise CardLayoutUnavailable(
            "aqt.clayout.CardLayout could not be imported: %r. This Anki build may not "
            "ship the Card Types editor under the expected module path." % (exc,)
        ) from exc

    errors: List[str] = []
    for kwargs in _ATTEMPTS:
        call_kwargs = dict(kwargs)
        if parent is not None:
            call_kwargs["parent"] = parent
        try:
            return CardLayout(mw, note, **call_kwargs)
        except TypeError as exc:
            errors.append("CardLayout(mw, note, **%r) -> %s" % (call_kwargs, exc))
            continue

    try:
        signature = str(inspect.signature(CardLayout.__init__))
    except (TypeError, ValueError):
        signature = "<not introspectable>"

    raise CardLayoutUnavailable(
        "Could not construct aqt.clayout.CardLayout with any of the attempted call "
        "shapes.\n\nInstalled signature: CardLayout.__init__%s\n\nAttempts tried:\n  %s\n\n"
        "Please paste this message, plus the signature above, so docs/api-notes.md can be "
        "updated with the exact call shape for your Anki build."
        % (signature, "\n  ".join(errors))
    )
