"""Embeddable live card preview -- the single-screen redesign's left pane.

Ported from the real, verified ``aqt.clayout.CardLayout`` pattern
(``CardLayout.setup_preview``/``_renderPreview``, checked against the actual aqt 26.8.1
source while planning this) rather than launching Anki's own separate ``CardLayout``
window: the preview there is just a plain ``AnkiWebView`` fed by ``note.ephemeral_card()``
-- nothing about it requires CardLayout's dialog chrome, so it can be embedded as a widget
here instead.

Same safety property as the standalone preview it replaces (``addon/ui/preview.py``, M3):
``ephemeral_card()`` never writes to the collection. That matters specifically because the
M3 "invisible modal" hang was a collection *write* racing a newly-opened *modal dialog* --
this widget is not a dialog and never writes anything, so it can't reopen that bug class.

Also verified while building this: ``AnkiWebView.stdHtml()``'s ``context`` argument is only
special-cased for a handful of known Anki classes (``Editor`, ``Reviewer``, ``Previewer``,
``CardLayout``, ``DeckOptionsDialog``) in ``aqt/mediasrv.py``'s ``PageContext``; passing this
widget itself falls through to ``PageContext.UNKNOWN`` harmlessly -- that enum is compared
against ``EDITOR`` in exactly one place in the whole media server, so UNKNOWN behaves
identically to CARD_LAYOUT for every media (image/audio) request a preview needs to serve.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from aqt import mw
from aqt.qt import QButtonGroup, QHBoxLayout, QPushButton, QVBoxLayout, QWidget
from aqt.sound import av_player, play_clicked_audio
from aqt.theme import theme_manager
from aqt.webview import AnkiWebView, AnkiWebViewKind

__all__ = ["PreviewPanel"]


class PreviewPanel(QWidget):
    """A live, read-only card preview widget. Never writes to the collection."""

    def __init__(self, parent: Any = None):
        super().__init__(parent)
        self._note: Any = None
        self._model: Optional[dict] = None
        self._ord = 0
        self._front_html = ""
        self._back_html = ""
        self._css = ""
        self._show_front = True
        self._have_autoplayed = False
        self._rendered_card: Any = None
        self._render_timer: Any = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        toggle_row = QHBoxLayout()
        self.front_button = QPushButton("Front")
        self.front_button.setCheckable(True)
        self.front_button.setChecked(True)
        self.back_button = QPushButton("Back")
        self.back_button.setCheckable(True)
        self._toggle_group = QButtonGroup(self)
        self._toggle_group.setExclusive(True)
        self._toggle_group.addButton(self.front_button)
        self._toggle_group.addButton(self.back_button)
        self.front_button.clicked.connect(lambda: self.show_side(front=True))
        self.back_button.clicked.connect(lambda: self.show_side(front=False))
        toggle_row.addWidget(self.front_button)
        toggle_row.addWidget(self.back_button)
        toggle_row.addStretch(1)
        layout.addLayout(toggle_row)

        self.webview = AnkiWebView(kind=AnkiWebViewKind.CARD_LAYOUT)
        layout.addWidget(self.webview, stretch=1)
        self.webview.stdHtml(
            mw.reviewer.revHtml(),
            css=["css/reviewer.css"],
            js=["js/mathjax.js", "js/vendor/mathjax/tex-chtml-full.js", "js/reviewer.js"],
            context=self,
        )
        self.webview.set_bridge_command(self._on_bridge_cmd, self)

    # -- public API -----------------------------------------------------------

    def set_content(
        self, note: Any, model: dict, ord: int, *, front_html: str, back_html: str, css: str
    ) -> None:
        """Record what should be rendered. Does not render by itself -- call
        :meth:`refresh` (debounced) or :meth:`render` (immediate) afterward, so a burst of
        field/order edits collapses into a single render instead of one per keystroke."""
        self._note = note
        self._model = model
        self._ord = ord
        self._front_html = front_html
        self._back_html = back_html
        self._css = css

    def refresh(self) -> None:
        """Debounced render -- the same 200ms coalescing ``CardLayout`` itself uses."""
        if self._render_timer:
            self._render_timer.stop()
        self._render_timer = mw.progress.timer(200, self.render, False, parent=self)

    def render(self) -> None:
        if self._render_timer:
            self._render_timer.stop()
            self._render_timer = None
        if self._note is None or self._model is None:
            return

        template = dict(self._model["tmpls"][self._ord])
        template["qfmt"] = self._front_html
        template["afmt"] = self._back_html
        model = dict(self._model)
        model["css"] = self._css

        c = self._rendered_card = self._note.ephemeral_card(
            self._ord, custom_note_type=model, custom_template=template, fill_empty=False
        )

        body_class = theme_manager.body_classes_for_card_ord(c.ord, theme_manager.night_mode)
        text = mw.prepare_card_text_for_display(c.question() if self._show_front else c.answer())
        self.webview.eval("_showAnswer(%s, %s);" % (json.dumps(text), json.dumps(body_class)))

        if not self._have_autoplayed:
            self._have_autoplayed = True
            if c.autoplay():
                self.webview.setPlaybackRequiresGesture(False)
                audio = c.question_av_tags() if self._show_front else c.answer_av_tags()
            else:
                audio = []
                self.webview.setPlaybackRequiresGesture(True)
            av_player.play_tags(audio)

    def clear(self, message: str = "") -> None:
        """Blank the preview immediately -- for whenever there's nothing valid to render
        (an incomplete mapping, no notes selected, switching to a pair not ready yet).

        Without this, a caller that just stops calling :meth:`render`/:meth:`refresh`
        leaves whatever card was on screen from the *previous* selection, which reads as
        the preview being stuck rather than as there being nothing to show yet -- this is
        the fix for exactly that: switching decks and still seeing the old deck's card.
        """
        if self._render_timer:
            self._render_timer.stop()
            self._render_timer = None
        self._note = None
        self._model = None
        self._rendered_card = None
        self._have_autoplayed = False
        text = ('<div style="padding: 24px; opacity: 0.6;">%s</div>' % message) if message else ""
        self.webview.eval("_showAnswer(%s, %s);" % (json.dumps(text), json.dumps([])))

    def show_side(self, *, front: bool) -> None:
        self._show_front = front
        self._have_autoplayed = False
        if front:
            self.front_button.setChecked(True)
        else:
            self.back_button.setChecked(True)
        self.render()

    # -- internals --------------------------------------------------------------

    def _on_bridge_cmd(self, cmd: str) -> Any:
        if cmd.startswith("play:") and self._rendered_card is not None:
            play_clicked_audio(cmd, self._rendered_card)
