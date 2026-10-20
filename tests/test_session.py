import os
import threading

import paramiko
import pytest

from lxscp.core import RemoteSession, TransferCancelled, UnknownHostKey, trust_host_key
from sftp_server import PASSWORD, USERNAME


def test_unknown_host_key_is_reported_then_trusted(sftp_server, tmp_path):
    known_hosts = str(tmp_path / "kh")
    s = RemoteSession(known_hosts_path=known_hosts)
    with pytest.raises(UnknownHostKey) as info:
        s.connect("127.0.0.1", sftp_server.port, USERNAME, PASSWORD)
    err = info.value
    assert err.hostname == f"[127.0.0.1]:{sftp_server.port}"
    assert err.fingerprint.startswith("SHA256:")
    assert not s.is_connected()

    trust_host_key(err.hostname, err.key, known_hosts)
    s.connect("127.0.0.1", sftp_server.port, USERNAME, PASSWORD)
    assert s.is_connected()
    assert s.home == "/"
    s.disconnect()
    assert not s.is_connected()


def test_changed_host_key_is_rejected(sftp_server, tmp_path):
    known_hosts = str(tmp_path / "kh")
    other_key = paramiko.RSAKey.generate(2048)
    trust_host_key(f"[127.0.0.1]:{sftp_server.port}", other_key, known_hosts)
    s = RemoteSession(known_hosts_path=known_hosts)
    with pytest.raises(paramiko.BadHostKeyException):
        s.connect("127.0.0.1", sftp_server.port, USERNAME, PASSWORD)
    assert not s.is_connected()


def test_wrong_password(sftp_server, session, tmp_path):
    s = RemoteSession(known_hosts_path=session.known_hosts_path)
    with pytest.raises(paramiko.AuthenticationException):
        s.connect("127.0.0.1", sftp_server.port, USERNAME, "wrong")
    assert not s.is_connected()


def test_connect_validation():
    s = RemoteSession()
    with pytest.raises(ValueError):
        s.connect("", 22, "user")
    with pytest.raises(ValueError):
        s.connect("host", 22, "")
    with pytest.raises(RuntimeError):
        s.list_dir("/")


def test_list_dir(session, remote_root):
    (remote_root / "sub").mkdir()
    (remote_root / "b.txt").write_bytes(b"hello")
    (remote_root / "a.txt").write_bytes(b"")
    os.symlink(remote_root / "sub", remote_root / "sublink")
    entries = session.list_dir("/")
    assert [e.name for e in entries] == ["sub", "sublink", "a.txt", "b.txt"]
    by_name = {e.name: e for e in entries}
    assert by_name["b.txt"].size == 5 and by_name["b.txt"].path == "/b.txt"
    assert by_name["sublink"].is_dir and by_name["sublink"].is_link
    assert by_name["sub"].mode != 0


def test_upload_and_download_tree(session, local_root, remote_root, tmp_path):
    src = local_root / "project"
    (src / "nested" / "deep").mkdir(parents=True)
    (src / "readme.md").write_text("hello world")
    (src / "nested" / "data.bin").write_bytes(os.urandom(300_000))
    (src / "nested" / "deep" / "empty").write_bytes(b"")
    single = local_root / "single.txt"
    single.write_text("one")
    os.utime(single, (1_600_000_000, 1_600_000_000))

    calls = []
    count = session.upload([str(src), str(single)], "/", progress=lambda d, t, n: calls.append((d, t)))
    assert count == 4
    assert (remote_root / "project" / "nested" / "data.bin").read_bytes() == (src / "nested" / "data.bin").read_bytes()
    assert (remote_root / "project" / "nested" / "deep" / "empty").exists()
    assert (remote_root / "single.txt").read_text() == "one"
    assert int(os.path.getmtime(remote_root / "single.txt")) == 1_600_000_000  # timestamps preserved
    total = 300_000 + len("hello world") + 3
    assert calls[-1] == (total, total)
    assert all(d <= t for d, t in calls)

    dest = tmp_path / "downloaded"
    dest.mkdir()
    assert session.download(["/project", "/single.txt"], str(dest)) == 4
    assert (dest / "project" / "nested" / "data.bin").read_bytes() == (src / "nested" / "data.bin").read_bytes()
    assert (dest / "project" / "readme.md").read_text() == "hello world"
    assert int(os.path.getmtime(dest / "single.txt")) == 1_600_000_000


def test_upload_overwrites_and_merges(session, local_root, remote_root):
    (remote_root / "dir").mkdir()
    (remote_root / "dir" / "keep.txt").write_text("keep")
    (remote_root / "dir" / "f.txt").write_text("old content that is longer")
    (local_root / "dir").mkdir()
    (local_root / "dir" / "f.txt").write_text("new")
    session.upload([str(local_root / "dir")], "/")
    assert (remote_root / "dir" / "f.txt").read_text() == "new"
    assert (remote_root / "dir" / "keep.txt").read_text() == "keep"


def test_upload_onto_file_with_folder_name_fails(session, local_root, remote_root):
    (remote_root / "clash").write_text("i am a file")
    (local_root / "clash").mkdir()
    (local_root / "clash" / "x").write_text("x")
    with pytest.raises(NotADirectoryError):
        session.upload([str(local_root / "clash")], "/")


def test_mkdir_rename_chmod_delete(session, remote_root):
    session.mkdir("/new")
    assert (remote_root / "new").is_dir()
    session.rename("/new", "/renamed")
    assert (remote_root / "renamed").is_dir() and not (remote_root / "new").exists()
    (remote_root / "renamed" / "a").mkdir()
    (remote_root / "renamed" / "a" / "f.txt").write_text("x")
    (remote_root / "top.txt").write_text("t")
    session.chmod("/top.txt", 0o600)
    assert oct(os.stat(remote_root / "top.txt").st_mode & 0o777) == oct(0o600)
    os.symlink(remote_root / "renamed", remote_root / "link")
    session.delete(["/link"])  # deletes only the link, not its target
    assert (remote_root / "renamed" / "a" / "f.txt").exists()
    session.delete(["/renamed", "/top.txt"])
    assert os.listdir(remote_root) == []
    assert not session.exists("/renamed")
    with pytest.raises(ValueError):
        session.delete(["/"])


def test_cancel_upload_removes_partial_file(session, local_root, remote_root):
    big = local_root / "big.bin"
    big.write_bytes(os.urandom(2_000_000))
    cancel = threading.Event()

    def progress(done, total, name):
        if done > 100_000:
            cancel.set()

    with pytest.raises(TransferCancelled):
        session.upload([str(big)], "/", progress=progress, cancel_event=cancel)
    assert not (remote_root / "big.bin").exists()
    assert session.is_connected()
    session.upload([str(big)], "/")  # session still usable
    assert (remote_root / "big.bin").stat().st_size == 2_000_000


def test_cancel_download_removes_partial_file(session, remote_root, tmp_path):
    (remote_root / "big.bin").write_bytes(os.urandom(2_000_000))
    cancel = threading.Event()

    def progress(done, total, name):
        if done > 100_000:
            cancel.set()

    with pytest.raises(TransferCancelled):
        session.download(["/big.bin"], str(tmp_path), progress=progress, cancel_event=cancel)
    assert not (tmp_path / "big.bin").exists()
    assert session.list_dir("/")[0].name == "big.bin"
