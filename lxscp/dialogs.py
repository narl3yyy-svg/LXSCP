"""Dialogs and the background worker used by the main window."""

from __future__ import annotations

import os
import stat
import threading
import time
from typing import Callable, Optional

from PyQt6.QtCore import Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from lxscp.core import FileEntry, format_permissions, format_size, format_timestamp
from lxscp.sites import Site, SiteStore


class Worker(QThread):
    """Runs ``fn(worker)`` in a background thread."""

    progress = pyqtSignal(object, object, str)  # done, total, current item
    succeeded = pyqtSignal(object)
    failed = pyqtSignal(object)

    def __init__(self, fn: Callable[["Worker"], object], parent=None):
        super().__init__(parent)
        self._fn = fn
        self.cancel_event = threading.Event()
        self._last_report = 0.0

    def run(self):
        try:
            result = self._fn(self)
        except BaseException as exc:  # noqa: BLE001 - reported to the GUI
            self.failed.emit(exc)
        else:
            self.succeeded.emit(result)

    def report(self, done: int, total: int, name: str) -> None:
        now = time.monotonic()
        if done >= total or now - self._last_report >= 0.1:
            self._last_report = now
            self.progress.emit(done, total, name)


class ProgressDialog(QDialog):
    """Modal progress window for transfers and other long operations."""

    def __init__(self, title: str, worker: Worker, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.setMinimumWidth(480)
        self._worker = worker
        self._started = time.monotonic()

        layout = QVBoxLayout(self)
        self.operation_label = QLabel(title + "...")
        self.file_label = QLabel("")
        self.file_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.file_label.setWordWrap(True)
        self.bar = QProgressBar()
        self.bar.setRange(0, 0)
        self.stats_label = QLabel("")
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.clicked.connect(self.reject)

        layout.addWidget(self.operation_label)
        layout.addWidget(self.file_label)
        layout.addWidget(self.bar)
        layout.addWidget(self.stats_label)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(self.cancel_button)
        layout.addLayout(row)

        worker.progress.connect(self._on_progress)
        worker.finished.connect(self.accept)

    def _on_progress(self, done, total, name):
        self.file_label.setText(name)
        if total:
            self.bar.setRange(0, 1000)
            self.bar.setValue(int(done * 1000 / total))
            elapsed = max(time.monotonic() - self._started, 0.001)
            speed = done / elapsed
            self.stats_label.setText(f"{format_size(done)} of {format_size(total)}  -  {format_size(int(speed))}/s")

    def reject(self):
        # Closing/Cancel asks the worker to stop; the dialog closes once it has.
        if self._worker.isFinished():
            super().reject()
            return
        self._worker.cancel_event.set()
        self.cancel_button.setEnabled(False)
        self.operation_label.setText("Cancelling...")

    def accept(self):
        super().accept()


class LoginDialog(QDialog):
    """WinSCP-like login dialog with a list of saved sites."""

    NEW_SITE = "New Site"

    def __init__(self, store: SiteStore, parent=None, initial: Optional[dict] = None):
        super().__init__(parent)
        self.setWindowTitle("Login - LXSCP")
        self.resize(720, 400)
        self.store = store

        self.site_list = QListWidget()
        self.site_list.setMinimumWidth(200)
        self.site_list.currentItemChanged.connect(self._on_site_selected)
        self.site_list.itemDoubleClicked.connect(lambda _item: self._login())

        session_box = QGroupBox("Session")
        form = QFormLayout(session_box)
        self.protocol = QComboBox()
        self.protocol.addItem("SFTP")
        self.protocol.setEnabled(False)
        self.host = QLineEdit()
        self.host.setPlaceholderText("server.example.com")
        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        self.port.setValue(22)
        host_row = QHBoxLayout()
        host_row.addWidget(self.host, 1)
        host_row.addWidget(QLabel("Port number:"))
        host_row.addWidget(self.port)
        self.username = QLineEdit()
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password.setPlaceholderText("SSH password")
        self.save_password = QCheckBox("Save password")
        self.save_password.setToolTip(f"Store the password in the {store.passwords.backend_name}")
        self.show_password = QCheckBox("Show")
        self.show_password.setToolTip("Show the password and key passphrase")
        self.show_password.toggled.connect(self._toggle_show)
        password_row = QHBoxLayout()
        password_row.addWidget(self.password, 1)
        password_row.addWidget(self.show_password)
        password_row.addWidget(self.save_password)
        self.passphrase = QLineEdit()
        self.passphrase.setEchoMode(QLineEdit.EchoMode.Password)
        self.passphrase.setPlaceholderText("only if the private key is encrypted")
        self.save_passphrase = QCheckBox("Save passphrase")
        self.save_passphrase.setToolTip(f"Store the key passphrase in the {store.passwords.backend_name}")
        passphrase_row = QHBoxLayout()
        passphrase_row.addWidget(self.passphrase, 1)
        passphrase_row.addWidget(self.save_passphrase)
        self.key_file = QLineEdit()
        self.key_file.setPlaceholderText("optional, e.g. ~/.ssh/id_ed25519")
        browse = QPushButton("...")
        browse.setFixedWidth(32)
        browse.clicked.connect(self._browse_key)
        key_row = QHBoxLayout()
        key_row.addWidget(self.key_file, 1)
        key_row.addWidget(browse)
        self.remote_dir = QLineEdit()
        self.remote_dir.setPlaceholderText("home directory")
        self.local_dir = QLineEdit()
        self.local_dir.setPlaceholderText("current directory")

        form.addRow("File protocol:", self.protocol)
        form.addRow("Host name:", host_row)
        form.addRow("User name:", self.username)
        form.addRow("Password:", password_row)
        form.addRow("Private key file:", key_row)
        form.addRow("Key passphrase:", passphrase_row)
        form.addRow("Remote directory:", self.remote_dir)
        form.addRow("Local directory:", self.local_dir)

        self.save_button = QPushButton("Save")
        self.save_button.clicked.connect(self._save)
        self.delete_button = QPushButton("Delete")
        self.delete_button.clicked.connect(self._delete)
        self.login_button = QPushButton("Login")
        self.login_button.setDefault(True)
        self.login_button.clicked.connect(self._login)
        close_button = QPushButton("Close")
        close_button.clicked.connect(self.reject)

        right = QVBoxLayout()
        right.addWidget(session_box)
        right.addStretch(1)
        buttons = QHBoxLayout()
        buttons.addWidget(self.save_button)
        buttons.addWidget(self.delete_button)
        buttons.addStretch(1)
        buttons.addWidget(self.login_button)
        buttons.addWidget(close_button)
        right.addLayout(buttons)

        layout = QHBoxLayout(self)
        layout.addWidget(self.site_list)
        layout.addLayout(right, 1)

        if initial:
            self._reload_sites(select=initial.get("site", ""))
            self._fill(initial)
            self.password.setText(initial.get("password", ""))
            self.passphrase.setText(initial.get("passphrase", ""))
            site = self.current_site()
            self.save_password.setChecked(bool(site and site.save_password))
            self.save_passphrase.setChecked(bool(site and site.save_passphrase))
            self.host.setFocus()
        else:
            # Reopen on the site used last time, with its saved password/passphrase.
            self._reload_sites(select=store.last_used())
            self._on_site_selected(self.site_list.currentItem())

    def _toggle_show(self, on: bool) -> None:
        mode = QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password
        self.password.setEchoMode(mode)
        self.passphrase.setEchoMode(mode)

    def current_site(self) -> Optional[Site]:
        item = self.site_list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    # -- sites ---------------------------------------------------------------

    def _reload_sites(self, select: str = "") -> None:
        self.site_list.blockSignals(True)
        self.site_list.clear()
        new_item = QListWidgetItem(self.NEW_SITE)
        new_item.setData(Qt.ItemDataRole.UserRole, None)
        self.site_list.addItem(new_item)
        current = new_item
        for site in self.store.load():
            item = QListWidgetItem(site.name)
            item.setData(Qt.ItemDataRole.UserRole, site)
            self.site_list.addItem(item)
            if site.name == select:
                current = item
        self.site_list.setCurrentItem(current)
        self.site_list.blockSignals(False)
        self.delete_button.setEnabled(current is not new_item)

    def _on_site_selected(self, item, _previous=None):
        site = item.data(Qt.ItemDataRole.UserRole) if item else None
        self.delete_button.setEnabled(site is not None)
        self._fill(vars(site) if site else {})
        self.password.setText(self.store.password(site) if site else "")
        self.save_password.setChecked(bool(site and site.save_password))
        self.passphrase.setText(self.store.passphrase(site) if site else "")
        self.save_passphrase.setChecked(bool(site and site.save_passphrase))
        if site is None:
            self.host.setFocus()
        elif self.password.text():
            self.login_button.setFocus()
        else:
            self.password.setFocus()

    def _fill(self, values: dict) -> None:
        self.host.setText(values.get("host", ""))
        self.port.setValue(int(values.get("port", 22) or 22))
        self.username.setText(values.get("username", ""))
        self.key_file.setText(values.get("key_file", ""))
        self.remote_dir.setText(values.get("remote_dir", ""))
        self.local_dir.setText(values.get("local_dir", ""))

    def _browse_key(self):
        start = os.path.expanduser("~/.ssh")
        path, _ = QFileDialog.getOpenFileName(self, "Select private key", start)
        if path:
            self.key_file.setText(path)

    def _save(self):
        if not self.host.text().strip():
            QMessageBox.warning(self, "Save session", "Enter a host name first.")
            return
        site = self.current_site()
        default = site.name if site else (
            f"{self.username.text().strip()}@{self.host.text().strip()}".lstrip("@")
        )
        name, ok = QInputDialog.getText(self, "Save session as site", "Site name:", text=default)
        name = name.strip()
        if not ok or not name:
            return
        p = self.params()
        if self.save_password.isChecked() and not p["password"]:
            QMessageBox.information(self, "Save session", "Enter the password to save it with the site.")
            self.password.setFocus()
            return
        if self.save_passphrase.isChecked() and not p["passphrase"]:
            QMessageBox.information(self, "Save session", "Enter the key passphrase to save it with the site.")
            self.passphrase.setFocus()
            return
        self.store.upsert(Site(name=name, host=p["host"], port=p["port"], username=p["username"],
                               key_file=p["key_file"], remote_dir=p["remote_dir"], local_dir=p["local_dir"],
                               save_password=self.save_password.isChecked(),
                               save_passphrase=self.save_passphrase.isChecked()),
                          password=p["password"], passphrase=p["passphrase"])
        self._reload_sites(select=name)

    def _delete(self):
        item = self.site_list.currentItem()
        site = item.data(Qt.ItemDataRole.UserRole) if item else None
        if site is None:
            return
        if QMessageBox.question(self, "Delete site", f"Delete site '{site.name}'?") == QMessageBox.StandardButton.Yes:
            self.store.delete(site.name)
            self._reload_sites()

    # -- result --------------------------------------------------------------

    def _login(self):
        if not self.host.text().strip():
            QMessageBox.warning(self, "Login", "Enter a host name.")
            return
        if not self.username.text().strip():
            QMessageBox.warning(self, "Login", "Enter a user name.")
            return
        site = self.current_site()
        if site is not None:
            # Keep the saved secrets in step with the checkboxes, even without pressing Save.
            p = self.params()
            self.store.update_secrets(site.name, self.save_password.isChecked(), p["password"],
                                      self.save_passphrase.isChecked(), p["passphrase"])
        self.store.set_last_used(site.name if site else "")
        self.accept()

    def params(self) -> dict:
        return {
            "host": self.host.text().strip(),
            "port": self.port.value(),
            "username": self.username.text().strip(),
            "password": self.password.text(),
            "passphrase": self.passphrase.text(),
            "key_file": self.key_file.text().strip(),
            "remote_dir": self.remote_dir.text().strip(),
            "local_dir": self.local_dir.text().strip(),
            "site": self.current_site().name if self.current_site() else "",
        }


class PropertiesDialog(QDialog):
    """Shows file details and lets the user change permissions."""

    _BITS = [
        ("Owner", (stat.S_IRUSR, stat.S_IWUSR, stat.S_IXUSR)),
        ("Group", (stat.S_IRGRP, stat.S_IWGRP, stat.S_IXGRP)),
        ("Others", (stat.S_IROTH, stat.S_IWOTH, stat.S_IXOTH)),
    ]

    def __init__(self, entry: FileEntry, location: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"{entry.name} - Properties")
        self.entry = entry
        layout = QVBoxLayout(self)

        info = QFormLayout()
        info.addRow("Name:", QLabel(entry.name))
        info.addRow("Location:", QLabel(location))
        info.addRow("Type:", QLabel(entry.type_name))
        if not entry.is_dir:
            info.addRow("Size:", QLabel(f"{format_size(entry.size)} ({entry.size:,} bytes)"))
        info.addRow("Modified:", QLabel(format_timestamp(entry.mtime)))
        info.addRow("Owner:", QLabel(entry.owner))
        layout.addLayout(info)

        box = QGroupBox("Permissions")
        grid = QGridLayout(box)
        for col, title in enumerate(("Read", "Write", "Execute"), start=1):
            grid.addWidget(QLabel(title), 0, col)
        self.checks = []
        for row, (who, bits) in enumerate(self._BITS, start=1):
            grid.addWidget(QLabel(who), row, 0)
            for col, bit in enumerate(bits, start=1):
                cb = QCheckBox()
                cb.setChecked(bool(entry.mode & bit))
                cb.toggled.connect(self._sync_octal)
                grid.addWidget(cb, row, col)
                self.checks.append((cb, bit))
        self.octal = QLineEdit()
        self.octal.setMaxLength(4)
        self.octal.setFixedWidth(60)
        self.octal.textEdited.connect(self._sync_checks)
        grid.addWidget(QLabel("Octal:"), 4, 0)
        grid.addWidget(self.octal, 4, 1, 1, 2)
        self.rights = QLabel()
        grid.addWidget(self.rights, 4, 3)
        layout.addWidget(box)
        if entry.is_link:
            box.setEnabled(False)
            box.setTitle("Permissions (not editable for links)")

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._sync_octal()

    def mode(self) -> int:
        special = self.entry.mode & 0o7000
        perms = 0
        for cb, bit in self.checks:
            if cb.isChecked():
                perms |= bit
        return special | perms

    def changed(self) -> bool:
        return not self.entry.is_link and self.mode() != (self.entry.mode & 0o7777)

    def _sync_octal(self, *_):
        mode = self.mode()
        self.octal.setText(f"{mode:04o}")
        self.rights.setText(format_permissions(stat.S_IFREG | mode))

    def _sync_checks(self, text: str):
        try:
            value = int(text, 8)
        except ValueError:
            return
        for cb, bit in self.checks:
            cb.blockSignals(True)
            cb.setChecked(bool(value & bit))
            cb.blockSignals(False)
        self.rights.setText(format_permissions(stat.S_IFREG | self.mode()))


def run_worker_modal(parent: QWidget, title: str, fn: Callable[[Worker], object]):
    """Run ``fn`` in a Worker while showing a modal ProgressDialog.

    Returns ``(result, error)``; exactly one of them is meaningful.
    """
    worker = Worker(fn, parent)
    outcome = {"result": None, "error": None}
    worker.succeeded.connect(lambda r: outcome.__setitem__("result", r))
    worker.failed.connect(lambda e: outcome.__setitem__("error", e))
    dialog = ProgressDialog(title, worker, parent)
    # Start after the dialog's event loop is running so `finished` can't be missed.
    QTimer.singleShot(0, worker.start)
    dialog.exec()
    worker.wait()
    dialog.deleteLater()
    worker.deleteLater()
    return outcome["result"], outcome["error"]
