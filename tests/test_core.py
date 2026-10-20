import os
import stat

import pytest

from lxscp.core import (
    FileEntry,
    delete_local,
    format_permissions,
    format_size,
    format_timestamp,
    is_safe_name,
    list_local,
    normalize_remote_path,
    parse_sftp_url,
    remote_join,
    remote_parent,
    sort_entries,
    validate_host,
    validate_port,
)
from lxscp.passwords import PasswordStore
from lxscp.sites import Site, SiteStore


def test_validate_host():
    assert validate_host("  example.com ") == "example.com"
    assert validate_host("::1") == "::1"
    for bad in ("", "   ", "my host", "/etc"):
        with pytest.raises(ValueError):
            validate_host(bad)


def test_validate_port():
    assert validate_port("22") == 22
    for bad in ("abc", 0, 70000, None):
        with pytest.raises(ValueError):
            validate_port(bad)


@pytest.mark.parametrize("raw, expected", [
    ("", "/"), ("/", "/"), ("tmp", "/tmp"), ("/var/www/", "/var/www"),
    ("/a/../b", "/b"), ("//x//y", "/x/y"), ("/..", "/"),
])
def test_normalize_remote_path(raw, expected):
    assert normalize_remote_path(raw) == expected


def test_remote_join_and_parent():
    assert remote_join("/var/www", "f.txt") == "/var/www/f.txt"
    assert remote_join("/var/www/", "f.txt") == "/var/www/f.txt"
    assert remote_parent("/var/www") == "/var"
    assert remote_parent("/var") == "/"
    assert remote_parent("/") == "/"


def test_is_safe_name():
    assert is_safe_name("file.txt")
    assert is_safe_name(".bashrc")
    for bad in ("", ".", "..", "../etc", "a/b", "x\0y"):
        assert not is_safe_name(bad)


def test_format_helpers():
    assert format_size(0) == "0 B"
    assert format_size(1023) == "1023 B"
    assert format_size(2048) == "2.0 KB"
    assert format_size(5 * 1024 ** 3) == "5.0 GB"
    assert format_timestamp(0) == ""
    assert format_timestamp(1_700_000_000).startswith("2023-11-1")
    assert format_permissions(stat.S_IFDIR | 0o755) == "rwxr-xr-x"
    assert format_permissions(stat.S_IFREG | 0o640) == "rw-r-----"


def test_type_name():
    assert FileEntry("a.txt", "/a.txt", False).type_name == "TXT file"
    assert FileEntry("Makefile", "/Makefile", False).type_name == "File"
    assert FileEntry("d", "/d", True).type_name == "File folder"
    assert FileEntry("l", "/l", True, is_link=True).type_name == "Link to folder"


def test_sort_entries_dirs_first():
    entries = [FileEntry("b.txt", "", False), FileEntry("Zdir", "", True),
               FileEntry("a.txt", "", False), FileEntry("adir", "", True)]
    assert [e.name for e in sort_entries(entries)] == ["adir", "Zdir", "a.txt", "b.txt"]


def test_parse_sftp_url():
    assert parse_sftp_url("sftp://bob@example.com:2222/var/www") == {
        "host": "example.com", "port": 2222, "username": "bob", "password": "", "remote_dir": "/var/www"}
    assert parse_sftp_url("sftp://example.com")["port"] == 22
    with pytest.raises(ValueError):
        parse_sftp_url("http://example.com")
    with pytest.raises(ValueError):
        parse_sftp_url("sftp://")


def test_list_local_and_delete(tmp_path):
    (tmp_path / "dir").mkdir()
    (tmp_path / "dir" / "inner.txt").write_text("x")
    (tmp_path / "file.bin").write_bytes(b"12345")
    os.symlink(tmp_path / "dir", tmp_path / "link_to_dir")
    os.symlink(tmp_path / "missing", tmp_path / "broken")
    entries = {e.name: e for e in list_local(str(tmp_path))}
    assert entries["dir"].is_dir and not entries["dir"].is_link
    assert entries["file.bin"].size == 5 and not entries["file.bin"].is_dir
    assert entries["link_to_dir"].is_dir and entries["link_to_dir"].is_link
    assert entries["broken"].is_link and not entries["broken"].is_dir
    assert [e.name for e in list_local(str(tmp_path))][:2] == ["dir", "link_to_dir"]

    delete_local([str(tmp_path / "link_to_dir")])  # removes only the link
    assert (tmp_path / "dir" / "inner.txt").exists()
    delete_local([str(tmp_path / "dir"), str(tmp_path / "file.bin"), str(tmp_path / "broken")])
    assert os.listdir(tmp_path) == []


def test_site_store_roundtrip(tmp_path):
    store = SiteStore(str(tmp_path / "sites.json"))
    assert store.load() == []
    store.upsert(Site("web", "example.com", 2222, "bob", remote_dir="/var/www"))
    store.upsert(Site("alpha", "a.example.com"))
    store.upsert(Site("web", "example.org", 22, "alice"))
    sites = store.load()
    assert [s.name for s in sites] == ["alpha", "web"]
    assert sites[1].host == "example.org" and sites[1].username == "alice"
    assert stat.S_IMODE(os.stat(store.path).st_mode) == 0o600
    assert "\"password\"" not in open(store.path).read()
    store.delete("alpha")
    assert [s.name for s in store.load()] == ["web"]


def test_site_store_ignores_garbage(tmp_path):
    path = tmp_path / "sites.json"
    path.write_text("{not json")
    assert SiteStore(str(path)).load() == []
    path.write_text('[{"name": "x", "host": "h", "bogus": 1}, {"name": "no-host"}]')
    assert [s.name for s in SiteStore(str(path)).load()] == ["x"]


def test_password_store_file_fallback(tmp_path):
    store = PasswordStore(str(tmp_path / "pw.json"), use_keyring=False)
    assert store.get("x") == ""
    store.set("x", "secret")
    store.set("y", "other")
    assert store.get("x") == "secret" and store.get("y") == "other"
    assert stat.S_IMODE(os.stat(store.path).st_mode) == 0o600
    store.delete("x")
    assert store.get("x") == "" and store.get("y") == "other"


class FakeKeyring:
    def __init__(self):
        self.data = {}

    def get_password(self, service, name):
        return self.data.get((service, name))

    def set_password(self, service, name, value):
        self.data[(service, name)] = value

    def delete_password(self, service, name):
        self.data.pop((service, name), None)


def test_password_store_prefers_keyring(tmp_path):
    store = PasswordStore(str(tmp_path / "pw.json"), use_keyring=False)
    store._keyring = FakeKeyring()
    store.set("site", "pw")
    assert store._keyring.data == {("lxscp", "site"): "pw"}
    assert not os.path.exists(store.path)  # nothing written to the file
    assert store.get("site") == "pw"
    store.delete("site")
    assert store._keyring.data == {}


def test_site_store_passwords(tmp_path):
    store = SiteStore(str(tmp_path / "sites.json"), PasswordStore(str(tmp_path / "pw.json"), use_keyring=False))
    store.upsert(Site("a", "h", save_password=True), password="pw")
    site = store.load()[0]
    assert site.save_password and store.password(site) == "pw"
    assert "pw" not in open(store.path).read().replace('"save_password"', "")

    store.upsert(Site("a", "h", save_password=False), password="pw")  # unticked: forget it
    site = store.load()[0]
    assert not site.save_password and store.password(site) == "" and store.passwords.get("a") == ""

    store.upsert(Site("b", "h", save_password=True), password="pw2")
    store.delete("b")
    assert store.passwords.get("b") == ""


def test_site_store_passphrase_and_last_used(tmp_path):
    store = SiteStore(str(tmp_path / "sites.json"), PasswordStore(str(tmp_path / "pw.json"), use_keyring=False))
    store.upsert(Site("k", "h", key_file="~/.ssh/id", save_passphrase=True), passphrase="s3cr3t-kp")
    site = store.load()[0]
    assert site.save_passphrase and not site.save_password
    assert store.passphrase(site) == "s3cr3t-kp" and store.password(site) == ""
    assert "s3cr3t-kp" not in open(store.path).read()

    store.update_secrets("k", True, "pw", True, "phrase2")
    site = store.load()[0]
    assert (store.password(site), store.passphrase(site)) == ("pw", "phrase2")
    assert site.key_file == "~/.ssh/id"  # other settings untouched
    store.update_secrets("k", True, "pw", False, "phrase2")  # unticked: forget the passphrase
    site = store.load()[0]
    assert store.passphrase(site) == "" and store.passwords.get("k (key passphrase)") == ""

    assert store.last_used() == ""
    store.set_last_used("k")
    assert store.last_used() == "k"
    store.delete("k")
    assert store.last_used() == "" and store.passwords.get("k") == ""
