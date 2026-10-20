import os
import sys

import pytest

# Run Qt without a display and keep config files out of the real home directory.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sftp_server import PASSWORD, USERNAME, SFTPTestServer  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    # Never touch the developer's real desktop keyring from tests.
    monkeypatch.setattr("lxscp.passwords._keyring", lambda: None)
    return tmp_path / "config"


@pytest.fixture
def remote_root(tmp_path):
    root = tmp_path / "remote"
    root.mkdir()
    return root


@pytest.fixture
def local_root(tmp_path):
    root = tmp_path / "local"
    root.mkdir()
    return root


@pytest.fixture
def sftp_server(remote_root):
    server = SFTPTestServer(str(remote_root))
    yield server
    server.stop()


@pytest.fixture
def session(sftp_server, tmp_path):
    from lxscp.core import RemoteSession, UnknownHostKey, trust_host_key

    known_hosts = str(tmp_path / "known_hosts")
    s = RemoteSession(known_hosts_path=known_hosts)
    try:
        s.connect("127.0.0.1", sftp_server.port, USERNAME, PASSWORD)
    except UnknownHostKey as exc:
        trust_host_key(exc.hostname, exc.key, known_hosts)
        s.connect("127.0.0.1", sftp_server.port, USERNAME, PASSWORD)
    yield s
    s.disconnect()


@pytest.fixture(scope="session")
def qapp():
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication(["lxscp-tests"])
    yield app
