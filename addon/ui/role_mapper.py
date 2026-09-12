"""M3's real role-mapping UI: one dropdown per notetype field, replacing M1's hardcoded
profile-only path.

One role per field, modeled directly on the prior art ``claude.md`` names -- Anki's own
CSV-import field-mapping dialog (``aqt.import_export``), which is dropdown-per-column, not
drag-and-drop. The underlying data model (``RoleMapping``) still supports many-to-many
bindings; this dialog is a deliberately simpler v1 view onto it via
``core.role_schema.FieldAssignment``/``mapping_from_assignments``/``assignments_from_mapping``.

This module touches ``aqt`` freely (it's a dialog, not pure logic) but contains no field-name
matching of its own -- every field name it shows came from the live notetype or a profile the
user already chose to load, never something this module infers.

No preview button here -- live card preview is its own Tools-menu action
(``addon/ui/card_preview.py``) that works from a shipped profile directly. Previewing an
in-progress, not-yet-saved mapping built here isn't available yet; this dialog's job is
building the mapping, not rendering it.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

from aqt.qt import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    Qt,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from ..core.role_schema import (
    FieldAssignment,
    Role,
    RoleMapping,
    assignments_from_mapping,
    mapping_from_assignments,
)

__all__ = ["RoleMapperDialog"]

_UNMAPPED = "(unmapped)"
_COLUMNS = ["Ord", "Field", "Sample", "Role", "Hide"]


class RoleMapperDialog(QDialog):
    def __init__(
        self,
        parent: Any,
        *,
        notetype_name: str,
        live_fields: Sequence[Tuple[int, str]],
        samples: Dict[str, List[str]],
        initial_mapping: RoleMapping,
        profile_mapping: Optional[RoleMapping],
    ):
        super().__init__(parent)
        self.setWindowTitle("Map fields — %s" % notetype_name)
        self.resize(760, 520)

        self._notetype_name = notetype_name
        self._live_fields = list(live_fields)
        self._samples = samples
        self._profile_mapping = profile_mapping
        self._result_mapping: Optional[RoleMapping] = None
        self._role_combos: Dict[int, QComboBox] = {}
        self._hide_checks: Dict[int, QCheckBox] = {}
        self._passthrough: Dict[str, FieldAssignment] = {}

        layout = QVBoxLayout(self)

        lang_row = QHBoxLayout()
        lang_row.addWidget(QLabel("Target language (front):"))
        self.target_language = QLineEdit(initial_mapping.target_language or "")
        lang_row.addWidget(self.target_language)
        lang_row.addWidget(QLabel("Native language (back):"))
        self.native_language = QLineEdit(initial_mapping.native_language or "")
        lang_row.addWidget(self.native_language)
        layout.addLayout(lang_row)

        self.table = QTableWidget(len(self._live_fields), len(_COLUMNS))
        self.table.setHorizontalHeaderLabels(_COLUMNS)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        layout.addWidget(self.table, stretch=1)

        self._populate(assignments_from_mapping(initial_mapping))

        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        button_row = QHBoxLayout()
        self.reset_button = QPushButton("Reset to shipped profile")
        self.reset_button.setEnabled(profile_mapping is not None)
        self.reset_button.setToolTip(
            "Discards your edits and re-seeds this table from the shipped profile for this "
            "notetype." if profile_mapping is not None else
            "No shipped profile matches this notetype, so there is nothing to reset to."
        )
        self.reset_button.clicked.connect(self._on_reset)
        button_row.addWidget(self.reset_button)
        button_row.addStretch(1)
        layout.addLayout(button_row)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.accepted.connect(self._on_accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

        self.target_language.textChanged.connect(self._revalidate)
        self.native_language.textChanged.connect(self._revalidate)
        self._revalidate()

    # -- table <-> data -------------------------------------------------

    def _populate(self, assignments: Sequence[FieldAssignment]) -> None:
        self.table.setRowCount(len(self._live_fields))
        self._role_combos.clear()
        self._hide_checks.clear()
        by_ord = {a.ord: a for a in assignments}
        self._passthrough = {a.name: a for a in assignments}

        for row, (ord_, name) in enumerate(self._live_fields):
            a = by_ord.get(ord_) or FieldAssignment(name=name, ord=ord_)

            ord_item = QTableWidgetItem(str(ord_))
            ord_item.setFlags(ord_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.table.setItem(row, 0, ord_item)

            name_item = QTableWidgetItem(name)
            name_item.setFlags(name_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.table.setItem(row, 1, name_item)

            sample_item = QTableWidgetItem("  |  ".join(self._samples.get(name, [])))
            sample_item.setFlags(sample_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.table.setItem(row, 2, sample_item)

            combo = QComboBox()
            combo.addItem(_UNMAPPED, None)
            for role in Role:
                combo.addItem(role.key, role)
            if a.role is not None:
                index = combo.findData(a.role)
                if index != -1:
                    combo.setCurrentIndex(index)
            combo.currentIndexChanged.connect(self._revalidate)
            self.table.setCellWidget(row, 3, combo)
            self._role_combos[row] = combo

            check = QCheckBox()
            check.setChecked(a.hidden)
            check.stateChanged.connect(self._revalidate)
            self.table.setCellWidget(row, 4, check)
            self._hide_checks[row] = check

    def _current_assignments(self) -> List[FieldAssignment]:
        out: List[FieldAssignment] = []
        for row, (ord_, name) in enumerate(self._live_fields):
            role = self._role_combos[row].currentData()
            hidden = self._hide_checks[row].isChecked()
            passthrough = self._passthrough.get(name)
            out.append(
                FieldAssignment(
                    name=name,
                    ord=ord_,
                    role=role,
                    hidden=hidden,
                    filter=passthrough.filter if passthrough else None,
                    css_class=passthrough.css_class if passthrough else None,
                )
            )
        return out

    def _current_mapping(self) -> RoleMapping:
        return mapping_from_assignments(
            self._notetype_name,
            self._current_assignments(),
            target_language=self.target_language.text().strip() or None,
            native_language=self.native_language.text().strip() or None,
        )

    # -- actions ----------------------------------------------------------

    def _revalidate(self, *_args: Any) -> None:
        result = self._current_mapping().validate()
        if result.ok:
            text = "Ready." if not result.warnings else "Ready, with warnings:\n" + "\n".join(
                "  - %s" % w for w in result.warnings
            )
        else:
            text = "Cannot proceed:\n" + "\n".join("  - %s" % e for e in result.errors)
        self.status_label.setText(text)
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(result.ok)

    def _on_reset(self) -> None:
        if self._profile_mapping is None:
            return
        self.target_language.setText(self._profile_mapping.target_language or "")
        self.native_language.setText(self._profile_mapping.native_language or "")
        self._populate(assignments_from_mapping(self._profile_mapping))
        self._revalidate()

    def _on_accept(self) -> None:
        self._result_mapping = self._current_mapping()
        self.accept()

    def result_mapping(self) -> RoleMapping:
        return self._result_mapping
