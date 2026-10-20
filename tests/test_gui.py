import os

import pytest
from PyQt6.QtCore import QItemSelectionModel, QMimeData, Qt, QUrl

from lxscp.app import MainWindow, main
from lxscp.core import RemoteSession
from lxscp.panels import EntryItem, FileTree
from lxscp.sites import SiteStore
from sftp_server import PASSWORD, USERNAME


@pytest.fixture
def window(qapp, tmp_path, local_root, monkeypatch):
    session = RemoteSession(known_hosts_path=str(tmp_path / "kh"))
    win = MainWindow(session=session, site_store=SiteStore(str(tmp_path / "sites.json")))
    win.answers = []  # answers for confirm(); default "yes"
    win.texts = []    # answers for ask_text(); default: accept the proposed text
    win.errors = []
    win.passwords = []  # answers for ask_password(); default: cancel
    win.password_prompts = []
    monkeypatch.setattr(win, "confirm", lambda title, text: win.answers.pop(0) if win.answers else True)
    monkeypatch.setattr(win, "ask_text", lambda title, label, text="": win.texts.pop(0) if win.texts else text)
    monkeypatch.setattr(win, "show_error", lambda title, text: win.errors.append((title, text)))

    def ask_password(title, label):
        win.password_prompts.append(label)
        return win.passwords.pop(0) if win.passwords else None

    monkeypatch.setattr(win, "ask_password", ask_password)
    win.local.navigate(str(local_root))
    win.show()
    yield win
    win.busy = False
    win.close()


def names(panel):
    out = []
    for i in range(panel.tree.topLevelItemCount()):
        out.append(panel.tree.topLevelItem(i).text(0))
    return out


def select(panel, *wanted):
    panel.tree.clearSelection()
    for i in range(panel.tree.topLevelItemCount()):
        item = panel.tree.topLevelItem(i)
        if item.text(0) in wanted:
            panel.tree.setCurrentItem(item, 0, QItemSelectionModel.SelectionFlag.NoUpdate)
            item.setSelected(True)


def connect(win, server):
    return win.connect_to({"host": "127.0.0.1", "port": server.port, "username": USERNAME, "password": PASSWORD})


def test_startup_not_connected(window):
    assert window.windowTitle() == "LXSCP"
    assert not window.act_upload.isEnabled()
    assert not window.remote.isEnabled()
    assert window.connection_label.text() == "Not connected"


def test_local_panel_listing_and_sorting(window, local_root):
    (local_root / "zeta").mkdir()
    (local_root / "Alpha.txt").write_text("a")
    (local_root / "beta.txt").write_text("bb")
    window.local.refresh()
    assert names(window.local) == ["..", "zeta", "Alpha.txt", "beta.txt"]
    window.local.tree.sortByColumn(0, Qt.SortOrder.DescendingOrder)
    assert names(window.local) == ["..", "zeta", "beta.txt", "Alpha.txt"]  # ".." and folders stay on top
    window.local.tree.sortByColumn(1, Qt.SortOrder.DescendingOrder)
    assert names(window.local)[2:] == ["beta.txt", "Alpha.txt"]
    window.local.navigate(str(local_root / "zeta"))
    window.local.go_up()
    assert window.local.current_path == str(local_root)
    assert window.local.focused_entry().name == "zeta"


def test_connect_accepts_host_key_once(window, sftp_server):
    window.answers = [False]
    assert not connect(window, sftp_server)  # user rejected the unknown key
    assert not window.session.is_connected()

    assert connect(window, sftp_server)  # accepted: key is cached
    assert window.session.is_connected()
    assert window.remote.current_path == "/"
    assert "tester@127.0.0.1" in window.windowTitle()
    window.disconnect_session()
    window.answers = [False]  # would refuse if asked again
    assert connect(window, sftp_server)
    assert window.answers == [False]  # no prompt this time


def test_wrong_password_prompts_again(window, sftp_server):
    window.answers = [True]
    window.passwords = [PASSWORD]
    assert window.connect_to({"host": "127.0.0.1", "port": sftp_server.port,
                              "username": USERNAME, "password": "nope"})
    assert len(window.password_prompts) == 1
    assert "Access denied" in window.password_prompts[0] and "tester@127.0.0.1" in window.password_prompts[0]


def test_missing_password_is_asked_for(window, sftp_server):
    window.answers = [True]
    window.passwords = [PASSWORD]
    assert window.connect_to({"host": "127.0.0.1", "port": sftp_server.port, "username": USERNAME})
    assert window.password_prompts == ["Password for tester@127.0.0.1:"]


def test_cancelled_password_prompt(window, sftp_server):
    window.answers = [True]
    assert not window.connect_to({"host": "127.0.0.1", "port": sftp_server.port, "username": USERNAME})
    assert len(window.password_prompts) == 1 and not window.errors
    assert not window.session.is_connected()


def test_gives_up_after_three_wrong_passwords(window, sftp_server):
    window.answers = [True]
    window.passwords = ["a", "b", "c"]
    assert not window.connect_to({"host": "127.0.0.1", "port": sftp_server.port,
                                  "username": USERNAME, "password": "nope"})
    assert len(window.password_prompts) == 3
    assert window.errors and window.errors[-1][0] == "Authentication failed"


def test_upload_download_rename_delete(window, sftp_server, local_root, remote_root):
    (local_root / "docs").mkdir()
    (local_root / "docs" / "a.txt").write_text("A")
    (local_root / "notes.txt").write_text("notes")
    window.local.refresh()
    assert connect(window, sftp_server)

    # Upload (F5 from the local panel)
    select(window.local, "docs", "notes.txt")
    window._set_active(window.local)
    window.transfer_active()
    assert (remote_root / "docs" / "a.txt").read_text() == "A"
    assert (remote_root / "notes.txt").read_text() == "notes"
    assert names(window.remote) == ["docs", "notes.txt"]  # remote root has no ".."

    # Upload again -> overwrite prompt; answer "no" -> nothing happens
    (local_root / "notes.txt").write_text("changed")
    select(window.local, "notes.txt")
    window.answers = [False]
    assert not window.upload_selected() and (remote_root / "notes.txt").read_text() == "notes"

    # Create folder, rename, download
    window._set_active(window.remote)
    window.texts = ["backup"]
    window.create_folder()
    assert (remote_root / "backup").is_dir()
    assert window.remote.focused_entry().name == "backup"
    select(window.remote, "notes.txt")
    window.texts = ["renamed.txt"]
    window.rename_focused()
    assert (remote_root / "renamed.txt").exists() and not (remote_root / "notes.txt").exists()

    (local_root / "notes.txt").unlink()
    window.local.refresh()
    select(window.remote, "renamed.txt", "docs")
    window.transfer_active()
    assert (local_root / "renamed.txt").read_text() == "notes"
    assert (local_root / "docs" / "a.txt").read_text() == "A"
    assert "renamed.txt" in names(window.local)

    # Delete on the remote side (confirm "yes")
    select(window.remote, "docs", "renamed.txt", "backup")
    window.delete_selected()
    assert os.listdir(remote_root) == []
    assert names(window.remote) == []

    # Delete on the local side, refused then accepted
    window._set_active(window.local)
    select(window.local, "renamed.txt")
    window.answers = [False]
    window.delete_selected()
    assert (local_root / "renamed.txt").exists()
    window.delete_selected()
    assert not (local_root / "renamed.txt").exists()
    assert not window.errors


def test_remote_navigation_by_path_and_double_click(window, sftp_server, remote_root):
    (remote_root / "a" / "b").mkdir(parents=True)
    assert connect(window, sftp_server)
    window.remote.path_edit.setText("a/b")
    window.remote._on_path_entered()
    assert window.remote.current_path == "/a/b"
    item = window.remote.tree.topLevelItem(0)
    assert item.text(0) == ".."
    window.remote._on_item_activated(item, 0)
    assert window.remote.current_path == "/a"
    select(window.remote, "b")
    window.remote._on_item_activated(window.remote.tree.currentItem(), 0)
    assert window.remote.current_path == "/a/b"


def test_drag_and_drop_mime(window, sftp_server, local_root, remote_root):
    (local_root / "drag.txt").write_text("dragged")
    window.local.refresh()
    assert connect(window, sftp_server)
    items = [i for i in (window.local.tree.topLevelItem(n) for n in range(window.local.tree.topLevelItemCount()))
             if i.text(0) == "drag.txt"]
    mime = window.local.tree.mimeData(items)
    assert window.remote.tree._accepts(mime)
    assert not window.local.tree._accepts(mime)  # can't drop onto the same side
    side, paths = FileTree.decode(mime)
    window.remote.tree.files_dropped.emit(paths, side, "")
    assert (remote_root / "drag.txt").read_text() == "dragged"

    external = QMimeData()
    external.setUrls([QUrl.fromLocalFile(str(local_root / "drag.txt"))])
    assert window.remote.tree._accepts(external)
    assert not window.local.tree._accepts(external)


def test_properties_chmod(window, sftp_server, remote_root, monkeypatch):
    from lxscp import app as app_module

    (remote_root / "script.sh").write_text("#!/bin/sh\n")
    os.chmod(remote_root / "script.sh", 0o644)
    assert connect(window, sftp_server)
    select(window.remote, "script.sh")
    window._set_active(window.remote)

    class FakeDialog:
        def __init__(self, entry, location, parent):
            assert entry.name == "script.sh" and location == "/"
        def exec(self):
            return True
        def changed(self):
            return True
        def mode(self):
            return 0o755

    monkeypatch.setattr(app_module, "PropertiesDialog", FakeDialog)
    window.show_properties()
    assert os.stat(remote_root / "script.sh").st_mode & 0o777 == 0o755
    assert window.remote.focused_entry().mode & 0o777 == 0o755


def test_properties_dialog_octal_sync(qapp):
    from lxscp.core import FileEntry
    from lxscp.dialogs import PropertiesDialog

    d = PropertiesDialog(FileEntry("f", "/f", False, mode=0o100644), "/")
    assert d.octal.text() == "0644" and not d.changed()
    d._sync_checks("0750")
    assert d.mode() == 0o750 and d.changed()


def test_edit_remote_file_uploads_on_save(window, sftp_server, remote_root, monkeypatch):
    from lxscp import app as app_module

    opened = []
    monkeypatch.setattr(app_module.QDesktopServices, "openUrl", lambda url: opened.append(url.toLocalFile()))
    (remote_root / "config.ini").write_text("a=1\n")
    assert connect(window, sftp_server)
    select(window.remote, "config.ini")
    window._set_active(window.remote)
    window.edit_focused()
    assert len(opened) == 1 and open(opened[0]).read() == "a=1\n"

    with open(opened[0], "w") as fh:
        fh.write("a=2\n")
    os.utime(opened[0], (2_000_000_000, 2_000_000_000))
    window._on_edited_file_changed(opened[0])
    window._upload_edited_files()
    assert (remote_root / "config.ini").read_text() == "a=2\n"
    tmp_dir = os.path.dirname(opened[0])
    window.close()
    assert not os.path.exists(tmp_dir)  # temp copies are cleaned up


def test_login_dialog_initial_values(qapp, tmp_path):
    from lxscp.dialogs import LoginDialog

    store = SiteStore(str(tmp_path / "s.json"))
    d = LoginDialog(store, initial={"host": "example.com", "port": 2222, "username": "bob", "password": "pw"})
    p = d.params()
    assert (p["host"], p["port"], p["username"], p["password"]) == ("example.com", 2222, "bob", "pw")
    assert d.site_list.count() == 1  # only "New Site"


@pytest.mark.parametrize("save_password", [True, False])
def test_login_dialog_save_password(qapp, tmp_path, monkeypatch, save_password):
    from lxscp import dialogs
    from lxscp.dialogs import LoginDialog

    store = SiteStore(str(tmp_path / "s.json"))
    d = LoginDialog(store)
    d.host.setText("example.com")
    d.username.setText("bob")
    d.password.setText("hunter2")
    d.save_password.setChecked(save_password)
    monkeypatch.setattr(dialogs.QInputDialog, "getText", staticmethod(lambda *a, **k: ("My server", True)))
    d._save()
    assert "hunter2" not in open(store.path).read()  # never in sites.json

    # Re-open the dialog and pick the saved site, as a user would.
    d2 = LoginDialog(store)
    d2.site_list.setCurrentRow(1)
    assert d2.host.text() == "example.com" and d2.username.text() == "bob"
    assert d2.save_password.isChecked() is save_password
    assert d2.password.text() == ("hunter2" if save_password else "")

    d2.password.setText("")
    d2.show_password.setChecked(True)
    assert d2.password.echoMode() == dialogs.QLineEdit.EchoMode.Normal


def test_connect_with_saved_site_password(window, sftp_server):
    from lxscp.dialogs import LoginDialog
    from lxscp.sites import Site

    window.site_store.upsert(Site("srv", "127.0.0.1", sftp_server.port, USERNAME, save_password=True),
                             password=PASSWORD)
    d = LoginDialog(window.site_store, window)
    d.site_list.setCurrentRow(1)
    window.answers = [True]
    assert window.connect_to(d.params())
    assert window.password_prompts == []


def test_main_version(capsys):
    with pytest.raises(SystemExit) as info:
        main(["lxscp", "--version"])
    assert info.value.code == 0
    assert "lxscp 0.1.0" in capsys.readouterr().out
