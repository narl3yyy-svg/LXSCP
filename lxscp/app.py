"""LXSCP main window and application entry point."""

from __future__ import annotations

import argparse
import os
import posixpath
import shutil
import sys
import tempfile
from typing import Dict, List, Optional

import paramiko
from PyQt6.QtCore import QFileSystemWatcher, QSettings, Qt, QTimer, QUrl
from PyQt6.QtGui import QAction, QDesktopServices, QIcon, QKeySequence
from PyQt6.QtWidgets import (
    QApplication,
    QInputDialog,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QSplitter,
    QStyle,
    QToolBar,
)

from lxscp import __version__
from lxscp.core import (
    FileEntry,
    RemoteSession,
    TransferCancelled,
    UnknownHostKey,
    delete_local,
    parse_sftp_url,
    remote_parent,
    trust_host_key,
)
from lxscp.dialogs import LoginDialog, PropertiesDialog, run_worker_modal
from lxscp.panels import FilePanel, LocalPanel, RemotePanel
from lxscp.sites import SiteStore

APP_NAME = "LXSCP"
ICON_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icons")


def app_icon() -> QIcon:
    icon = QIcon()
    for size in (16, 32, 48, 64, 128, 256):
        path = os.path.join(ICON_DIR, f"lxscp-{size}.png")
        if os.path.exists(path):
            icon.addFile(path)
    return QIcon.fromTheme("lxscp", icon)


class MainWindow(QMainWindow):
    def __init__(self, session: Optional[RemoteSession] = None, site_store: Optional[SiteStore] = None):
        super().__init__()
        self.session = session or RemoteSession()
        self.site_store = site_store or SiteStore()
        self.settings = QSettings("lxscp", "lxscp")
        self.busy = False

        self.local = LocalPanel()
        self.remote = RemotePanel(self.session)
        self.active_panel: FilePanel = self.local
        for panel in (self.local, self.remote):
            panel.activated.connect(lambda p=panel: self._set_active(p))
            panel.file_opened.connect(lambda entry, p=panel: self._open_file(p, entry))
            panel.tree.customContextMenuRequested.connect(lambda pos, p=panel: self._context_menu(p, pos))
        self.remote.tree.files_dropped.connect(self._on_drop_remote)
        self.local.tree.files_dropped.connect(self._on_drop_local)

        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.addWidget(self.local)
        self.splitter.addWidget(self.remote)
        self.splitter.setChildrenCollapsible(False)
        self.setCentralWidget(self.splitter)

        self._create_actions()
        self._create_menus()
        self._create_toolbar()
        self.connection_label = QLabel("Not connected")
        self.statusBar().addPermanentWidget(self.connection_label)

        # Remote files opened for editing: local temp path -> remote path.
        self.edited: Dict[str, str] = {}
        self.edit_mtimes: Dict[str, float] = {}
        self.edit_dirs: List[str] = []
        self.watcher = QFileSystemWatcher(self)
        self.watcher.fileChanged.connect(self._on_edited_file_changed)
        self._pending_uploads: set = set()
        self._edit_timer = QTimer(self)
        self._edit_timer.setSingleShot(True)
        self._edit_timer.setInterval(700)
        self._edit_timer.timeout.connect(self._upload_edited_files)

        self.resize(1280, 760)
        geometry = self.settings.value("geometry")
        if geometry is not None:
            self.restoreGeometry(geometry)
        self.setWindowIcon(app_icon())

        start_dir = self.settings.value("local_dir", "", type=str)
        if not start_dir or not os.path.isdir(start_dir):
            start_dir = os.path.expanduser("~")
        self.local.navigate(start_dir)
        self._set_active(self.local)
        self._update_state()

    # ------------------------------------------------------------------ UI setup

    def _action(self, text, slot, shortcut=None, icon=None, fallback=None, tip=None) -> QAction:
        action = QAction(text, self)
        if icon or fallback:
            std = self.style().standardIcon(fallback) if fallback else QIcon()
            action.setIcon(QIcon.fromTheme(icon, std) if icon else std)
        if shortcut:
            if isinstance(shortcut, (list, tuple)):
                action.setShortcuts([QKeySequence(s) for s in shortcut])
            else:
                action.setShortcut(QKeySequence(shortcut))
        if tip:
            action.setToolTip(tip)
        action.triggered.connect(slot)
        self.addAction(action)
        return action

    def _create_actions(self):
        SP = QStyle.StandardPixmap
        self.act_login = self._action("&New Session...", self.show_login, "Ctrl+N", "network-server", SP.SP_ComputerIcon,
                                      "Open a new session (Ctrl+N)")
        self.act_disconnect = self._action("&Disconnect", self.disconnect_session, "Shift+Ctrl+D", "network-offline",
                                           SP.SP_DialogCloseButton, "Disconnect")
        self.act_quit = self._action("&Quit", self.close, "Ctrl+Q", "application-exit")
        self.act_copy = self._action("&Copy to Other Panel", self.transfer_active, "F5", "edit-copy", SP.SP_ArrowForward,
                                     "Upload/download the selected files to the other panel (F5)")
        self.act_upload = self._action("&Upload", self.upload_selected, None, "go-up", SP.SP_ArrowUp,
                                       "Upload selected local files")
        self.act_download = self._action("Do&wnload", self.download_selected, None, "go-down", SP.SP_ArrowDown,
                                         "Download selected remote files")
        self.act_edit = self._action("&Edit / Open", self.edit_focused, "F4", "document-edit", SP.SP_FileIcon,
                                     "Open the file; remote files are uploaded back when saved (F4)")
        self.act_rename = self._action("&Rename", self.rename_focused, "F2", "edit-rename", None, "Rename (F2)")
        self.act_delete = self._action("&Delete", self.delete_selected, ["F8", "Delete"], "edit-delete", SP.SP_TrashIcon,
                                       "Delete (F8)")
        self.act_mkdir = self._action("&New Folder...", self.create_folder, "F7", "folder-new", SP.SP_FileDialogNewFolder,
                                      "Create a folder (F7)")
        self.act_props = self._action("&Properties", self.show_properties, "F9", "document-properties",
                                      SP.SP_FileDialogInfoView, "Properties and permissions (F9)")
        self.act_refresh = self._action("&Refresh", self.refresh_active, "Ctrl+R", "view-refresh", SP.SP_BrowserReload,
                                        "Refresh (Ctrl+R)")
        self.act_about = self._action("&About LXSCP", self.show_about, None, "help-about")

    def _create_menus(self):
        bar = self.menuBar()
        m = bar.addMenu("&Session")
        m.addActions([self.act_login, self.act_disconnect])
        m.addSeparator()
        m.addAction(self.act_quit)
        m = bar.addMenu("&Files")
        m.addActions([self.act_edit, self.act_copy, self.act_upload, self.act_download])
        m.addSeparator()
        m.addActions([self.act_rename, self.act_delete, self.act_mkdir, self.act_props])
        m = bar.addMenu("&View")
        m.addAction(self.act_refresh)
        m = bar.addMenu("&Help")
        m.addAction(self.act_about)

    def _create_toolbar(self):
        tb = QToolBar("Main")
        tb.setObjectName("main_toolbar")
        tb.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        tb.addActions([self.act_login, self.act_disconnect])
        tb.addSeparator()
        tb.addActions([self.act_upload, self.act_download])
        tb.addSeparator()
        tb.addActions([self.act_edit, self.act_rename, self.act_delete, self.act_mkdir, self.act_props])
        tb.addSeparator()
        tb.addAction(self.act_refresh)
        self.addToolBar(tb)

    def _context_menu(self, panel: FilePanel, pos):
        self._set_active(panel)
        menu = QMenu(self)
        if panel is self.local:
            menu.addActions([self.act_edit, self.act_upload])
        else:
            menu.addActions([self.act_edit, self.act_download])
        menu.addSeparator()
        menu.addActions([self.act_rename, self.act_delete, self.act_mkdir, self.act_props])
        menu.addSeparator()
        menu.addAction(self.act_refresh)
        menu.exec(panel.tree.viewport().mapToGlobal(pos))

    # ------------------------------------------------------------------ state

    def _set_active(self, panel: FilePanel):
        self.active_panel = panel
        self.local.set_active(panel is self.local)
        self.remote.set_active(panel is self.remote)

    def _update_state(self):
        connected = self.session.is_connected()
        for action in (self.act_disconnect, self.act_upload, self.act_download, self.act_copy):
            action.setEnabled(connected)
        self.remote.setEnabled(connected)
        if connected:
            who = f"{self.session.username}@{self.session.host}"
            if self.session.port != 22:
                who += f":{self.session.port}"
            self.connection_label.setText(f"SFTP  {who}")
            self.setWindowTitle(f"{who} - {APP_NAME}")
        else:
            self.connection_label.setText("Not connected")
            self.setWindowTitle(APP_NAME)

    def _ensure_connected(self) -> bool:
        if self.session.is_connected():
            return True
        if self.session.sftp is not None:  # the connection dropped
            self.disconnect_session()
            QMessageBox.warning(self, APP_NAME, "The connection to the server was lost.")
        else:
            QMessageBox.information(self, APP_NAME, "Not connected. Use Session > New Session to connect.")
        return False

    # Small wrappers so dialogs can be replaced in tests.
    def confirm(self, title: str, text: str) -> bool:
        return QMessageBox.question(self, title, text) == QMessageBox.StandardButton.Yes

    def ask_text(self, title: str, label: str, text: str = "") -> Optional[str]:
        value, ok = QInputDialog.getText(self, title, label, text=text)
        return value if ok else None

    def show_error(self, title: str, text: str) -> None:
        QMessageBox.critical(self, title, text)

    # ------------------------------------------------------------------ session

    def show_login(self, initial: Optional[dict] = None):
        if self.busy:
            return
        dialog = LoginDialog(self.site_store, self, initial=initial)
        if dialog.exec():
            self.connect_to(dialog.params())

    def connect_to(self, params: dict) -> bool:
        if self.busy:
            return False
        self.disconnect_session()
        while True:
            self.busy = True

            def work(worker):
                self.session.connect(params["host"], params.get("port", 22), params.get("username", ""),
                                     params.get("password") or None, params.get("key_file") or None)
                if worker.cancel_event.is_set():
                    self.session.disconnect()
                    raise TransferCancelled()

            _result, error = run_worker_modal(self, f"Connecting to {params['host']}", work)
            self.busy = False
            if isinstance(error, UnknownHostKey):
                text = (
                    f"The server's host key was not found in the cache. You have no guarantee that the server "
                    f"is the computer you think it is.\n\n"
                    f"Server:  {error.hostname}\nKey type:  {error.key_type}\nFingerprint:  {error.fingerprint}\n\n"
                    f"If you trust this host, press Yes to add the key to the cache and carry on connecting."
                )
                if not self.confirm("Warning - Unknown host key", text):
                    return False
                trust_host_key(error.hostname, error.key, self.session.known_hosts_path)
                continue
            if isinstance(error, paramiko.BadHostKeyException):
                self.show_error(
                    "WARNING - POTENTIAL SECURITY BREACH",
                    f"The server's host key does not match the one cached for {error.hostname}.\n"
                    f"This could mean someone is intercepting the connection, or the server was reinstalled.\n\n"
                    f"Expected: {error.expected_key.get_name()}\nReceived: {error.key.get_name()}\n\n"
                    f"The connection was refused. If the change is legitimate, remove the old key from "
                    f"~/.ssh/known_hosts or {self.session.known_hosts_path}.",
                )
                return False
            if isinstance(error, TransferCancelled):
                self.statusBar().showMessage("Connection cancelled", 5000)
                return False
            if isinstance(error, paramiko.AuthenticationException):
                self.show_error("Authentication failed", f"Access denied for user '{params.get('username')}'.\n\n{error}")
                return False
            if error is not None:
                self.show_error("Connection failed", f"Could not connect to {params['host']}:\n{error}")
                return False
            break

        self._update_state()
        remote_dir = params.get("remote_dir") or self.session.home
        if not self.remote.navigate(remote_dir):
            self.remote.navigate(self.session.home)
        local_dir = os.path.expanduser(params.get("local_dir") or "")
        if local_dir and os.path.isdir(local_dir):
            self.local.navigate(local_dir)
        self.statusBar().showMessage(f"Connected to {self.session.host}", 5000)
        return True

    def disconnect_session(self):
        if self.busy:
            return
        self.session.disconnect()
        self.remote.clear()
        self._update_state()

    # ------------------------------------------------------------------ transfers

    def _run(self, title: str, fn):
        """Run fn(worker) with a progress dialog. Returns True on success."""
        self.busy = True
        try:
            _result, error = run_worker_modal(self, title, fn)
        finally:
            self.busy = False
        if isinstance(error, TransferCancelled):
            self.statusBar().showMessage(f"{title}: cancelled", 5000)
            return False
        if error is not None:
            self.show_error(f"{title} failed", str(error) or error.__class__.__name__)
            return False
        return True

    def transfer_active(self):
        if self.active_panel is self.local:
            self.upload_selected()
        else:
            self.download_selected()

    def upload_selected(self):
        paths = [e.path for e in self.local.target_entries()]
        if paths:
            self.upload(paths, self.remote.current_path)

    def download_selected(self):
        paths = [e.path for e in self.remote.target_entries()]
        if paths:
            self.download(paths, self.local.current_path)

    def _on_drop_remote(self, paths, source_side, target):
        if source_side == "local":
            self.upload(paths, target or self.remote.current_path, ask_target=False)

    def _on_drop_local(self, paths, source_side, target):
        if source_side == "remote":
            self.download(paths, target or self.local.current_path, ask_target=False)

    def _describe(self, paths) -> str:
        if len(paths) == 1:
            return f"'{os.path.basename(paths[0].rstrip('/'))}'"
        return f"{len(paths)} items"

    def upload(self, paths: List[str], remote_dir: str, ask_target: bool = True) -> bool:
        if self.busy or not paths or not self._ensure_connected():
            return False
        if ask_target:
            remote_dir = self.ask_text("Upload", f"Upload {self._describe(paths)} to remote directory:", remote_dir)
            if not remote_dir:
                return False
            remote_dir = self.remote.resolve(remote_dir.strip())
        try:
            existing = {e.name for e in self.session.list_dir(remote_dir)}
        except Exception as exc:  # noqa: BLE001
            self.show_error("Upload failed", f"Cannot open remote directory '{remote_dir}':\n{exc}")
            return False
        conflicts = sorted({os.path.basename(p.rstrip(os.sep)) for p in paths} & existing)
        if conflicts and not self._confirm_overwrite(conflicts):
            return False
        ok = self._run("Uploading", lambda w: self.session.upload(paths, remote_dir, w.report, w.cancel_event))
        self.remote.refresh()
        if ok:
            self.statusBar().showMessage(f"Uploaded {self._describe(paths)} to {remote_dir}", 5000)
        return ok

    def download(self, paths: List[str], local_dir: str, ask_target: bool = True) -> bool:
        if self.busy or not paths or not self._ensure_connected():
            return False
        if ask_target:
            local_dir = self.ask_text("Download", f"Download {self._describe(paths)} to local directory:", local_dir)
            if not local_dir:
                return False
            local_dir = self.local.resolve(local_dir.strip())
        if not os.path.isdir(local_dir):
            self.show_error("Download failed", f"Local directory '{local_dir}' does not exist.")
            return False
        conflicts = sorted(posixpath.basename(p) for p in paths if os.path.lexists(os.path.join(local_dir, posixpath.basename(p))))
        if conflicts and not self._confirm_overwrite(conflicts):
            return False
        ok = self._run("Downloading", lambda w: self.session.download(paths, local_dir, w.report, w.cancel_event))
        self.local.refresh()
        if ok:
            self.statusBar().showMessage(f"Downloaded {self._describe(paths)} to {local_dir}", 5000)
        return ok

    def _confirm_overwrite(self, names: List[str]) -> bool:
        shown = "\n".join(names[:10]) + ("\n..." if len(names) > 10 else "")
        return self.confirm("Confirm overwrite",
                            f"The following item(s) already exist in the target directory:\n\n{shown}\n\n"
                            f"Overwrite them? (Folders are merged.)")

    # ------------------------------------------------------------------ file commands

    def refresh_active(self):
        self.local.refresh()
        if self.session.is_connected():
            self.remote.refresh()

    def _remote_ok(self, panel: FilePanel) -> bool:
        return panel is self.local or (not self.busy and self._ensure_connected())

    def create_folder(self):
        panel = self.active_panel
        if not panel.current_path or not self._remote_ok(panel):
            return
        name = self.ask_text("Create folder", "New folder name:", "New folder")
        if not name or not name.strip():
            return
        name = name.strip()
        if "/" in name or name in (".", ".."):
            self.show_error("Create folder", "Invalid folder name.")
            return
        path = panel.join(panel.current_path, name)
        try:
            if panel is self.local:
                os.mkdir(path)
            else:
                self.session.mkdir(path)
        except Exception as exc:  # noqa: BLE001
            self.show_error("Create folder", f"Cannot create folder '{name}':\n{exc}")
            return
        panel.navigate(panel.current_path, [name])

    def rename_focused(self):
        panel = self.active_panel
        entry = panel.focused_entry()
        if entry is None or not self._remote_ok(panel):
            return
        new_name = self.ask_text("Rename", f"Rename '{entry.name}' to:", entry.name)
        if not new_name or new_name.strip() == entry.name:
            return
        new_name = new_name.strip()
        if "/" in new_name or new_name in (".", ".."):
            self.show_error("Rename", "Invalid name.")
            return
        if new_name in panel.entry_names():
            self.show_error("Rename", f"'{new_name}' already exists.")
            return
        new_path = panel.join(panel.current_path, new_name)
        try:
            if panel is self.local:
                os.rename(entry.path, new_path)
            else:
                self.session.rename(entry.path, new_path)
        except Exception as exc:  # noqa: BLE001
            self.show_error("Rename", f"Cannot rename '{entry.name}':\n{exc}")
            return
        panel.navigate(panel.current_path, [new_name])

    def delete_selected(self):
        panel = self.active_panel
        entries = panel.target_entries()
        if not entries or not self._remote_ok(panel):
            return
        what = f"'{entries[0].name}'" if len(entries) == 1 else f"{len(entries)} items"
        where = "local" if panel is self.local else "remote"
        if not self.confirm("Confirm delete", f"Permanently delete {what} from the {where} side?\n"
                                              f"Folders are deleted with all their contents."):
            return
        paths = [e.path for e in entries]
        if panel is self.local:
            try:
                delete_local(paths)
            except Exception as exc:  # noqa: BLE001
                self.show_error("Delete", str(exc))
        else:
            self._run("Deleting", lambda w: self.session.delete(paths, w.cancel_event))
        panel.refresh()

    def show_properties(self):
        panel = self.active_panel
        entry = panel.focused_entry()
        if entry is None or not self._remote_ok(panel):
            return
        dialog = PropertiesDialog(entry, panel.current_path, self)
        if not dialog.exec() or not dialog.changed():
            return
        try:
            if panel is self.local:
                os.chmod(entry.path, dialog.mode())
            else:
                self.session.chmod(entry.path, dialog.mode())
        except Exception as exc:  # noqa: BLE001
            self.show_error("Properties", f"Cannot change permissions:\n{exc}")
        panel.refresh()

    def show_about(self):
        QMessageBox.about(
            self, f"About {APP_NAME}",
            f"<b>{APP_NAME} {__version__}</b><br>A WinSCP-style SFTP client for Linux.<br><br>"
            f"<a href='https://github.com/narl3yyy-svg/LXSCP'>github.com/narl3yyy-svg/LXSCP</a><br>"
            f"&copy; narl3yyy-svg - MIT License",
        )

    # ------------------------------------------------------------------ open / edit

    def edit_focused(self):
        panel = self.active_panel
        entry = panel.focused_entry()
        if entry is None:
            return
        if entry.is_dir:
            panel.navigate(entry.path)
        else:
            self._open_file(panel, entry)

    def _open_file(self, panel: FilePanel, entry: FileEntry):
        if panel is self.local:
            QDesktopServices.openUrl(QUrl.fromLocalFile(entry.path))
            return
        if self.busy or not self._ensure_connected():
            return
        tmp_dir = tempfile.mkdtemp(prefix="lxscp-edit-")
        self.edit_dirs.append(tmp_dir)
        if not self._run("Downloading", lambda w: self.session.download([entry.path], tmp_dir, w.report, w.cancel_event)):
            return
        local_path = os.path.join(tmp_dir, entry.name)
        self.edited[local_path] = entry.path
        self.edit_mtimes[local_path] = os.path.getmtime(local_path)
        self.watcher.addPath(local_path)
        QDesktopServices.openUrl(QUrl.fromLocalFile(local_path))
        self.statusBar().showMessage(
            f"Editing {entry.name}: changes are uploaded to {entry.path} when you save", 8000)

    def _on_edited_file_changed(self, path: str):
        self._pending_uploads.add(path)
        self._edit_timer.start()

    def _upload_edited_files(self):
        if self.busy:  # try again once the current operation is finished
            self._edit_timer.start()
            return
        pending, self._pending_uploads = self._pending_uploads, set()
        for local_path in pending:
            remote_path = self.edited.get(local_path)
            if remote_path is None:
                continue
            # Many editors replace the file on save, which drops the watch.
            if os.path.exists(local_path) and local_path not in self.watcher.files():
                self.watcher.addPath(local_path)
            if not os.path.exists(local_path):
                continue
            mtime = os.path.getmtime(local_path)
            if mtime == self.edit_mtimes.get(local_path):
                continue
            if not self.session.is_connected():
                self.statusBar().showMessage(f"Not connected - cannot upload {os.path.basename(local_path)}", 8000)
                continue
            target_dir = remote_parent(remote_path)
            if self._run("Uploading", lambda w: self.session.upload([local_path], target_dir, w.report, w.cancel_event)):
                self.edit_mtimes[local_path] = mtime
                self.statusBar().showMessage(f"Uploaded {remote_path}", 5000)
                if self.remote.current_path == target_dir:
                    self.remote.refresh()

    # ------------------------------------------------------------------ shutdown

    def closeEvent(self, event):
        if self.busy:
            event.ignore()
            return
        self.settings.setValue("geometry", self.saveGeometry())
        if self.local.current_path:
            self.settings.setValue("local_dir", self.local.current_path)
        self.session.disconnect()
        for path in self.edit_dirs:
            shutil.rmtree(path, ignore_errors=True)
        super().closeEvent(event)


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    parser = argparse.ArgumentParser(prog="lxscp", description="WinSCP-style SFTP client for Linux")
    parser.add_argument("url", nargs="?", help="optional session URL, e.g. sftp://user@host:22/path")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args, qt_args = parser.parse_known_args(argv[1:])

    app = QApplication([argv[0]] + qt_args)
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_NAME)
    app.setApplicationVersion(__version__)
    app.setDesktopFileName("lxscp")
    app.setWindowIcon(app_icon())

    window = MainWindow()
    window.show()

    initial = None
    if args.url:
        try:
            initial = parse_sftp_url(args.url)
        except ValueError as exc:
            QMessageBox.warning(window, APP_NAME, str(exc))
    QTimer.singleShot(0, lambda: window.show_login(initial))
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
