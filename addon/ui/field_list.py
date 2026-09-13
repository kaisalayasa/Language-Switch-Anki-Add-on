"""Field/role list -- the single-screen redesign's default right-side... now left-side
view (the "Fields" mode, as opposed to the raw-HTML mode).

Ported from ``role_mapper.py``'s per-field row (name, sample, M4 "Detected" chip, Role
combo, Hide checkbox) onto a ``QListWidget``.

Drag-to-reorder was tried and then explicitly discarded (user feedback: it didn't earn its
complexity). Field order is therefore fixed per notetype: a saved profile's
``RoleMapping.render_order`` when one was loaded, otherwise plain notetype field order --
``current_render_order()`` only ever reports a non-empty order in the former case, so a
mapping with no saved order keeps generating templates with ``template_generator``'s own
default role ordering rather than an incidental one derived from field position.

A profile like Core 2000's marks several fields ``hidden`` -- bookkeeping fields such as
``Core-Index`` or ``Optimized-Voc-Index`` that hold no real content. Those are real clutter
in a field list this long, so they're left out by default; the "Show hidden fields" checkbox
above the list reveals them for the rare case someone needs to touch one.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

from aqt.qt import (
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
    pyqtSignal,
)

from ..core.language_detect import LanguageGuess, detect_field_language
from ..core.role_schema import (
    FieldAssignment,
    Role,
    RoleMapping,
    assignments_from_mapping,
)

__all__ = ["FieldListWidget"]

_UNMAPPED = "(unmapped)"


class _FieldRow(QWidget):
    """One row: name, sample, detected-language chip, Role combo, Hide check."""

    def __init__(
        self,
        *,
        name: str,
        ord: int,
        sample: str,
        guess: LanguageGuess,
        role: Optional[Role],
        hidden: bool,
        filter: Optional[str],
        css_class: Optional[str],
    ):
        super().__init__()
        self.name = name
        self.ord = ord
        self.filter = filter
        self.css_class = css_class

        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 4, 6, 4)

        name_label = QLabel(name)
        name_label.setMinimumWidth(130)
        name_label.setStyleSheet("font-weight: 600;")
        layout.addWidget(name_label)

        sample_label = QLabel(sample)
        sample_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        layout.addWidget(sample_label, stretch=1)

        detected_label = QLabel(guess.code or "")
        detected_label.setMinimumWidth(28)
        if guess.is_confident:
            detected_label.setToolTip(
                "Guessed %r via %s -- informational only, not applied anywhere."
                % (guess.code, guess.confidence)
            )
        layout.addWidget(detected_label)

        self.combo = QComboBox()
        self.combo.addItem(_UNMAPPED, None)
        for r in Role:
            self.combo.addItem(r.key, r)
        if role is not None:
            index = self.combo.findData(role)
            if index != -1:
                self.combo.setCurrentIndex(index)
        layout.addWidget(self.combo)

        self.check = QCheckBox("Hide")
        self.check.setChecked(hidden)
        layout.addWidget(self.check)

    def to_assignment(self) -> FieldAssignment:
        return FieldAssignment(
            name=self.name,
            ord=self.ord,
            role=self.combo.currentData(),
            hidden=self.check.isChecked(),
            filter=self.filter,
            css_class=self.css_class,
        )


class FieldListWidget(QWidget):
    """A "Show hidden fields" toggle over a plain (non-reorderable) list of :class:`_FieldRow`.

    Emits ``changed`` on any edit -- a role change or a hide toggle -- so the caller can
    trigger a single debounced preview refresh. Toggling "Show hidden fields" itself does
    not emit ``changed``: it only changes which rows are visible, not the mapping.
    """

    changed = pyqtSignal()

    def __init__(self, parent: Any = None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        top_row = QHBoxLayout()
        self.show_hidden_check = QCheckBox("Show hidden fields")
        self.show_hidden_check.setChecked(False)
        self.show_hidden_check.stateChanged.connect(lambda *_args: self._rebuild())
        top_row.addWidget(self.show_hidden_check)
        top_row.addStretch(1)
        layout.addLayout(top_row)

        self.list = QListWidget()
        layout.addWidget(self.list)

        # (ord, field name, current assignment) for every field, in fixed display order --
        # the source of truth even for rows currently filtered out of view.
        self._row_data: List[Tuple[int, str, FieldAssignment]] = []
        self._samples: Dict[str, List[str]] = {}
        self._has_explicit_order = False

    # -- populate ---------------------------------------------------------------

    def set_content(
        self,
        live_fields: Sequence[Tuple[int, str]],
        samples: Dict[str, List[str]],
        mapping: RoleMapping,
        *,
        order: Optional[Sequence[Role]] = None,
    ) -> None:
        """(Re)populate every row from ``mapping``'s current assignments.

        Row order is ``order`` (e.g. a previously-saved ``RoleMapping.render_order``) when
        given, else the notetype's own field order. An unassigned row, or one whose role
        isn't mentioned in ``order``, sorts after every row that is -- nothing is ever
        dropped by a partial or stale order.
        """
        self._samples = dict(samples)
        self._has_explicit_order = bool(order)
        assignments = assignments_from_mapping(mapping)
        by_ord = {a.ord: a for a in assignments}
        rows = [
            (ord_, name, by_ord.get(ord_) or FieldAssignment(name=name, ord=ord_))
            for ord_, name in live_fields
        ]
        if order:
            role_rank = {role: i for i, role in enumerate(order)}
            rows.sort(key=lambda row: role_rank.get(row[2].role, len(order)))
        self._row_data = rows
        self._rebuild()

    # -- internals ----------------------------------------------------------------

    def _sync_visible_rows(self) -> None:
        """Pull live widget state back into ``_row_data`` before a rebuild throws those
        widgets away -- a role/hide edit must never be lost just because a filter or a
        later ``set_content`` call rebuilds the list."""
        widgets = {row.ord: row for row in self._widget_rows()}
        self._row_data = [
            (ord_, name, widgets[ord_].to_assignment() if ord_ in widgets else a)
            for ord_, name, a in self._row_data
        ]

    def _rebuild(self) -> None:
        self._sync_visible_rows()
        self.list.clear()
        show_hidden = self.show_hidden_check.isChecked()
        for ord_, name, a in self._row_data:
            if a.hidden and not show_hidden:
                continue
            guess = detect_field_language(self._samples.get(name, []))
            row = _FieldRow(
                name=name,
                ord=ord_,
                sample="  |  ".join(self._samples.get(name, [])),
                guess=guess,
                role=a.role,
                hidden=a.hidden,
                filter=a.filter,
                css_class=a.css_class,
            )
            row.combo.currentIndexChanged.connect(self._on_row_edited)
            row.check.stateChanged.connect(self._on_row_edited)
            item = QListWidgetItem(self.list)
            item.setSizeHint(row.sizeHint())
            self.list.addItem(item)
            self.list.setItemWidget(item, row)

    def _on_row_edited(self, *_args: Any) -> None:
        self._sync_visible_rows()
        if not self.show_hidden_check.isChecked():
            # a row just got checked "Hide" -- drop it from view immediately.
            self._rebuild()
        self.changed.emit()

    def _widget_rows(self) -> List[_FieldRow]:
        return [self.list.itemWidget(self.list.item(i)) for i in range(self.list.count())]

    # -- read back ----------------------------------------------------------------

    def current_assignments(self) -> List[FieldAssignment]:
        self._sync_visible_rows()
        return [a for _ord, _name, a in self._row_data]

    def current_render_order(self) -> List[Role]:
        """Every field's role in display order, feeding
        ``core.template_generator.split_render_order`` and ``RoleMapping.render_order`` --
        empty unless ``set_content`` was given an explicit ``order`` (from a saved profile),
        since without one this list's order is just field position, not a deliberate choice
        worth freezing into the generated template or a saved profile."""
        if not self._has_explicit_order:
            return []
        self._sync_visible_rows()
        return [a.role for _ord, _name, a in self._row_data if a.role is not None]
