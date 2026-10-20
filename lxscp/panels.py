"""The local and remote file panels of the commander-style main window."""

from __future__ import annotations

import json
import os
from typing import List, Optional

from PyQt6.QtCore import QItemSelectionModel, QMimeData, QMimeDatabase, Qt, QUrl, pyqtSignal
from PyQt6.QtGui import QIcon, QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QStyle,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from lxscp.core import (
    FileEntry,
    RemoteSession,
    format_permissions,
    format_size,
    format_timestamp,
    list_local,
    normalize_remote_path,
    remote_join,
    remote_parent,
)

MIME_TYPE = "application/x-lxscp-paths"
COLUMNS = ["Name", "Size", "Type", "Changed", "Rights", "Owner"]


class EntryItem(QTreeWidgetItem):
    def __init__(self, entry: FileEntry, is_parent: bool = False):
        self.entry = entry
        self.is_parent = is_parent
        if is_parent:
            super().__init__(["..", "", "Parent directory", "", "", ""])
        else:
            super().__init__([
                entry.name,
                "" if entry.is_dir else format_size(entry.size),
                entry.type_name,
                format_timestamp(entry.mtime),
                format_permissions(entry.mode),
                entry.owner,
            ])
            self.setTextAlignment(1, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

    def _key(self, column: int):
        e = self.entry
        if column == 1:
            return e.size
        if column == 2:
            return e.type_name.casefold()
        if column == 3:
            return e.mtime
        if column == 4:
            return e.mode & 0o7777
        if column == 5:
            return e.owner.casefold()
        return e.name.casefold()

    def __lt__(self, other: "EntryItem") -> bool:
        tree = self.treeWidget()
        column = tree.sortColumn() if tree else 0
        ascending = tree is None or tree.header().sortIndicatorOrder() == Qt.SortOrder.AscendingOrder
        # ".." always on top, then folders, then files - in either sort order.
        if self.is_parent != other.is_parent:
            return ascending if self.is_parent else not ascending
        if self.entry.is_dir != other.entry.is_dir:
            return ascending if self.entry.is_dir else not ascending
        a, b = self._key(column), other._key(column)
        if a != b:
            return a < b
        return self.entry.name < other.entry.name


class FileTree(QTreeWidget):
    """Tree widget with drag & drop between the two panels."""

    files_dropped = pyqtSignal(list, str, str)  # paths, source side, target folder
    focused = pyqtSignal()

    def __init__(self, side: str, parent=None):
        super().__init__(parent)
        self.side = side
        self.setHeaderLabels(COLUMNS)
        self.setRootIsDecorated(False)
        self.setUniformRowHeights(True)
        self.setAllColumnsShowFocus(True)
        self.setAlternatingRowColors(True)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setSortingEnabled(True)
        self.sortByColumn(0, Qt.SortOrder.AscendingOrder)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        self.setDefaultDropAction(Qt.DropAction.CopyAction)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.header().setStretchLastSection(False)
        self.setColumnWidth(0, 240)
        self.setColumnWidth(1, 80)
        self.setColumnWidth(2, 110)
        self.setColumnWidth(3, 140)
        self.setColumnWidth(4, 80)
        self.setColumnWidth(5, 80)

    def focusInEvent(self, event):
        super().focusInEvent(event)
        self.focused.emit()

    # -- drag ------------------------------------------------------------------

    def mimeTypes(self):
        return [MIME_TYPE, "text/uri-list"]

    def mimeData(self, items):
        paths = [i.entry.path for i in items if isinstance(i, EntryItem) and not i.is_parent]
        data = QMimeData()
        data.setData(MIME_TYPE, json.dumps({"side": self.side, "paths": paths}).encode())
        if self.side == "local":
            data.setUrls([QUrl.fromLocalFile(p) for p in paths])
        return data

    def supportedDropActions(self):
        return Qt.DropAction.CopyAction

    # -- drop ------------------------------------------------------------------

    @staticmethod
    def decode(mime: QMimeData):
        """Return (source side, paths) for supported drag data, else (None, [])."""
        if mime.hasFormat(MIME_TYPE):
            try:
                payload = json.loads(bytes(mime.data(MIME_TYPE)).decode())
                return payload["side"], list(payload["paths"])
            except (ValueError, KeyError, TypeError):
                return None, []
        if mime.hasUrls():
            urls = mime.urls()
            if urls and all(u.isLocalFile() for u in urls):
                return "external", [u.toLocalFile() for u in urls]
        return None, []

    def _accepts(self, mime: QMimeData) -> bool:
        side, paths = self.decode(mime)
        if not paths or side is None:
            return False
        if side == "external":
            return self.side == "remote"
        return side != self.side

    def dragEnterEvent(self, event):
        if self._accepts(event.mimeData()):
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        if self._accepts(event.mimeData()):
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
        else:
            event.ignore()

    def dropEvent(self, event):
        side, paths = self.decode(event.mimeData())
        if not self._accepts(event.mimeData()):
            event.ignore()
            return
        target = ""
        item = self.itemAt(event.position().toPoint())
        if isinstance(item, EntryItem) and item.entry.is_dir:
            target = item.entry.path
        event.setDropAction(Qt.DropAction.CopyAction)
        event.accept()
        self.files_dropped.emit(paths, "local" if side == "external" else side, target)


class FilePanel(QWidget):
    """Base panel: header with path box and buttons, a file list and a footer."""

    activated = pyqtSignal()  # the panel got focus
    file_opened = pyqtSignal(object)  # FileEntry of a double-clicked file

    def __init__(self, side: str, title: str, parent=None):
        super().__init__(parent)
        self.side = side
        self.current_path = ""
        self._icons = {}
        self._mime_db = QMimeDatabase()

        self.title_label = QLabel(title)
        self.path_edit = QLineEdit()
        self.path_edit.returnPressed.connect(self._on_path_entered)

        def tool(icon_name, fallback, tip, slot):
            button = QToolButton()
            button.setIcon(QIcon.fromTheme(icon_name, self.style().standardIcon(fallback)))
            button.setToolTip(tip)
            button.setAutoRaise(True)
            button.clicked.connect(slot)
            return button

        self.up_button = tool("go-up", QStyle.StandardPixmap.SP_FileDialogToParent, "Parent directory (Backspace)", self.go_up)
        self.home_button = tool("go-home", QStyle.StandardPixmap.SP_DirHomeIcon, "Home directory", self.go_home)
        self.refresh_button = tool("view-refresh", QStyle.StandardPixmap.SP_BrowserReload, "Refresh (Ctrl+R)", self.refresh)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.addWidget(self.title_label)
        header.addWidget(self.path_edit, 1)
        header.addWidget(self.up_button)
        header.addWidget(self.home_button)
        header.addWidget(self.refresh_button)

        self.tree = FileTree(side)
        self.tree.itemActivated.connect(self._on_item_activated)
        self.tree.itemSelectionChanged.connect(self._update_footer)
        self.tree.focused.connect(self.activated.emit)
        self.path_edit.installEventFilter(self)
        # Backspace like WinSCP; Alt+Up / Ctrl+Up (Cmd+Up on macOS) like file managers and Finder.
        for key in ("Backspace", "Alt+Up", "Ctrl+Up"):
            QShortcut(QKeySequence(key), self.tree,
                      context=Qt.ShortcutContext.WidgetShortcut).activated.connect(self.go_up)

        self.footer = QLabel("")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.addLayout(header)
        layout.addWidget(self.tree, 1)
        layout.addWidget(self.footer)
        self._entries: List[FileEntry] = []

    def eventFilter(self, obj, event):
        if obj is self.path_edit and event.type() == event.Type.FocusIn:
            self.activated.emit()
        return super().eventFilter(obj, event)

    def set_active(self, active: bool) -> None:
        font = self.title_label.font()
        font.setBold(active)
        self.title_label.setFont(font)

    # -- to be implemented by subclasses ------------------------------------

    def list_entries(self, path: str) -> List[FileEntry]:
        raise NotImplementedError

    def parent_of(self, path: str) -> str:
        raise NotImplementedError

    def home_path(self) -> str:
        raise NotImplementedError

    def resolve(self, text: str) -> str:
        raise NotImplementedError

    def join(self, base: str, name: str) -> str:
        raise NotImplementedError

    def is_root(self, path: str) -> bool:
        return self.parent_of(path) == path

    def is_available(self) -> bool:
        return True

    # -- navigation ------------------------------------------------------------

    def navigate(self, path: str, select: Optional[List[str]] = None, show_errors: bool = True) -> bool:
        if not self.is_available():
            return False
        try:
            entries = self.list_entries(path)
        except Exception as exc:  # noqa: BLE001 - shown to the user
            self.path_edit.setText(self.current_path)
            if show_errors:
                QMessageBox.warning(self, "Error", f"Cannot open '{path}':\n{exc}")
            return False
        self.current_path = path
        self.path_edit.setText(path)
        self._populate(entries, select or [])
        return True

    def refresh(self) -> None:
        if not self.current_path:
            return
        selected = [e.name for e in self.selected_entries()]
        current = self.tree.currentItem()
        if isinstance(current, EntryItem) and not current.is_parent and current.entry.name not in selected:
            selected.append(current.entry.name)
        if not self.navigate(self.current_path, selected, show_errors=False):
            # The folder disappeared; walk up until something works.
            path = self.current_path
            while not self.is_root(path):
                path = self.parent_of(path)
                if self.navigate(path, show_errors=False):
                    return

    def go_up(self) -> None:
        if self.current_path and not self.is_root(self.current_path):
            name = os.path.basename(self.current_path.rstrip("/"))
            self.navigate(self.parent_of(self.current_path), [name])

    def go_home(self) -> None:
        if self.is_available():
            self.navigate(self.home_path())

    def clear(self) -> None:
        self.tree.clear()
        self.current_path = ""
        self.path_edit.clear()
        self._entries = []
        self.footer.clear()

    def _on_path_entered(self) -> None:
        text = self.path_edit.text().strip()
        if text and self.is_available():
            self.navigate(self.resolve(text))

    def _on_item_activated(self, item: QTreeWidgetItem, _column: int) -> None:
        if not isinstance(item, EntryItem):
            return
        if item.is_parent:
            self.go_up()
        elif item.entry.is_dir:
            self.navigate(item.entry.path)
        else:
            self.file_opened.emit(item.entry)

    def _icon_for(self, entry: FileEntry) -> QIcon:
        if entry.is_dir:
            key = "folder-link" if entry.is_link else "folder"
            fallback = QStyle.StandardPixmap.SP_DirLinkIcon if entry.is_link else QStyle.StandardPixmap.SP_DirIcon
        else:
            key = self._mime_db.mimeTypeForFile(entry.name, QMimeDatabase.MatchMode.MatchExtension).iconName()
            fallback = QStyle.StandardPixmap.SP_FileLinkIcon if entry.is_link else QStyle.StandardPixmap.SP_FileIcon
        if key not in self._icons:
            self._icons[key] = QIcon.fromTheme(key, self.style().standardIcon(fallback))
        return self._icons[key]

    def _populate(self, entries: List[FileEntry], select: List[str]) -> None:
        self._entries = entries
        self.tree.setSortingEnabled(False)
        self.tree.clear()
        if not self.is_root(self.current_path):
            parent_entry = FileEntry("..", self.parent_of(self.current_path), True)
            item = EntryItem(parent_entry, is_parent=True)
            item.setIcon(0, self.style().standardIcon(QStyle.StandardPixmap.SP_FileDialogToParent))
            self.tree.addTopLevelItem(item)
        items = []
        for entry in entries:
            item = EntryItem(entry)
            item.setIcon(0, self._icon_for(entry))
            items.append(item)
        self.tree.addTopLevelItems(items)
        self.tree.setSortingEnabled(True)

        wanted = set(select)
        first = None
        for item in items:
            if item.entry.name in wanted:
                item.setSelected(True)
                if first is None:
                    first = item
        current = first or self.tree.topLevelItem(0)
        if current is not None:
            self.tree.setCurrentItem(current, 0, QItemSelectionModel.SelectionFlag.NoUpdate)
            self.tree.scrollToItem(current)
        self._update_footer()

    def _update_footer(self) -> None:
        selected = self.selected_entries()
        total_size = sum(e.size for e in self._entries if not e.is_dir)
        sel_size = sum(e.size for e in selected if not e.is_dir)
        self.footer.setText(
            f"{format_size(sel_size)} of {format_size(total_size)} in {len(selected)} of {len(self._entries)}"
        )

    # -- selection -------------------------------------------------------------

    def selected_entries(self) -> List[FileEntry]:
        return [i.entry for i in self.tree.selectedItems() if isinstance(i, EntryItem) and not i.is_parent]

    def focused_entry(self) -> Optional[FileEntry]:
        item = self.tree.currentItem()
        if isinstance(item, EntryItem) and not item.is_parent:
            return item.entry
        return None

    def target_entries(self) -> List[FileEntry]:
        """Selected entries, or the focused one if nothing is selected (like WinSCP)."""
        selected = self.selected_entries()
        if selected:
            return selected
        entry = self.focused_entry()
        return [entry] if entry else []

    def entry_names(self) -> set:
        return {e.name for e in self._entries}


class LocalPanel(FilePanel):
    def __init__(self, parent=None):
        super().__init__("local", "Local:", parent)

    def list_entries(self, path):
        return list_local(path)

    def parent_of(self, path):
        return os.path.dirname(path.rstrip(os.sep)) or os.sep

    def home_path(self):
        return os.path.expanduser("~")

    def resolve(self, text):
        text = os.path.expanduser(text)
        if not os.path.isabs(text):
            text = os.path.join(self.current_path or self.home_path(), text)
        return os.path.abspath(text)

    def join(self, base, name):
        return os.path.join(base, name)


class RemotePanel(FilePanel):
    def __init__(self, session: RemoteSession, parent=None):
        super().__init__("remote", "Remote:", parent)
        self.session = session

    def is_available(self):
        return self.session.is_connected()

    def list_entries(self, path):
        return self.session.list_dir(path)

    def parent_of(self, path):
        return remote_parent(path)

    def home_path(self):
        return self.session.home

    def resolve(self, text):
        if not text.startswith("/"):
            text = remote_join(self.current_path or self.session.home, text)
        return normalize_remote_path(text)

    def join(self, base, name):
        return remote_join(base, name)
