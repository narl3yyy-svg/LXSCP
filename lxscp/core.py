"""Core (non-GUI) logic for LXSCP: path helpers, local listings and the SFTP session."""

from __future__ import annotations

import base64
import functools
import hashlib
import os
import posixpath
import pwd
import shutil
import stat
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, List, Optional, Sequence, Tuple
from urllib.parse import unquote, urlsplit

import paramiko

# bytes_done, bytes_total, current item
ProgressCallback = Callable[[int, int, str], None]


def config_dir() -> str:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "lxscp")


def known_hosts_file() -> str:
    return os.path.join(config_dir(), "known_hosts")


# ---------------------------------------------------------------------------
# Data model and formatting helpers
# ---------------------------------------------------------------------------


@dataclass
class FileEntry:
    name: str
    path: str
    is_dir: bool
    size: int = 0
    mtime: float = 0.0
    mode: int = 0
    is_link: bool = False
    owner: str = ""

    @property
    def type_name(self) -> str:
        if self.is_link and self.is_dir:
            return "Link to folder"
        if self.is_dir:
            return "File folder"
        if self.is_link:
            return "Link"
        ext = os.path.splitext(self.name)[1]
        return f"{ext[1:].upper()} file" if ext else "File"


def format_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    value = float(size)
    for unit in ("KB", "MB", "GB", "TB", "PB"):
        value /= 1024
        if value < 1024 or unit == "PB":
            return f"{value:.1f} {unit}"
    return f"{size} B"  # unreachable


def format_timestamp(timestamp: Optional[float]) -> str:
    if not timestamp:
        return ""
    try:
        return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")
    except (OverflowError, OSError, ValueError):
        return ""


def format_permissions(mode: int) -> str:
    """Return WinSCP-style rights, e.g. ``rwxr-xr-x``."""
    if not mode:
        return ""
    return stat.filemode(mode)[1:]


def validate_host(host: str) -> str:
    cleaned = (host or "").strip()
    if not cleaned:
        raise ValueError("Host name cannot be empty.")
    if any(ch.isspace() for ch in cleaned):
        raise ValueError("Host name cannot contain spaces.")
    if cleaned.startswith(("/", "\\")):
        raise ValueError("Host name must be a server name or IP address.")
    return cleaned


def validate_port(port) -> int:
    try:
        value = int(port)
    except (TypeError, ValueError):
        raise ValueError("Port must be a number.") from None
    if not 1 <= value <= 65535:
        raise ValueError("Port must be between 1 and 65535.")
    return value


def normalize_remote_path(path: str) -> str:
    path = (path or "").strip()
    if not path:
        return "/"
    if not path.startswith("/"):
        path = "/" + path
    norm = posixpath.normpath(path)
    if norm.startswith("//"):  # POSIX allows a leading '//', we don't
        norm = "/" + norm.lstrip("/")
    return norm


def remote_join(base: str, *names: str) -> str:
    return posixpath.join(base or "/", *names)


def remote_parent(path: str) -> str:
    return posixpath.dirname(normalize_remote_path(path)) or "/"


def is_safe_name(name: str) -> bool:
    """Reject names a (malicious) server could use to escape the target directory."""
    return bool(name) and name not in (".", "..") and "/" not in name and "\0" not in name


def sort_entries(entries: Sequence[FileEntry]) -> List[FileEntry]:
    return sorted(entries, key=lambda e: (not e.is_dir, e.name.casefold(), e.name))


def parse_sftp_url(url: str) -> dict:
    """Parse ``sftp://user@host:port/path`` into login parameters."""
    parts = urlsplit(url.strip())
    if parts.scheme.lower() not in ("sftp", "ssh", "scp"):
        raise ValueError(f"Unsupported URL scheme: {parts.scheme or '(none)'}")
    if not parts.hostname:
        raise ValueError("URL does not contain a host name.")
    return {
        "host": parts.hostname,
        "port": parts.port or 22,
        "username": unquote(parts.username or ""),
        "password": unquote(parts.password or ""),
        "remote_dir": unquote(parts.path) if parts.path not in ("", "/") else "",
    }


@functools.lru_cache(maxsize=256)
def _local_owner(uid: int) -> str:
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return str(uid)


# ---------------------------------------------------------------------------
# Local file system
# ---------------------------------------------------------------------------


def list_local(path: str) -> List[FileEntry]:
    entries = []
    with os.scandir(path) as it:
        for de in it:
            try:
                lst = de.stat(follow_symlinks=False)
            except OSError:
                continue
            is_link = stat.S_ISLNK(lst.st_mode)
            st = lst
            if is_link:
                try:
                    st = de.stat()
                except OSError:  # broken link
                    st = lst
            is_dir = stat.S_ISDIR(st.st_mode)
            entries.append(
                FileEntry(
                    name=de.name,
                    path=de.path,
                    is_dir=is_dir,
                    size=0 if is_dir else st.st_size,
                    mtime=st.st_mtime,
                    mode=st.st_mode,
                    is_link=is_link,
                    owner=_local_owner(lst.st_uid),
                )
            )
    return sort_entries(entries)


def delete_local(paths: Sequence[str]) -> None:
    for path in paths:
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path)
        else:
            os.remove(path)


# ---------------------------------------------------------------------------
# SFTP session
# ---------------------------------------------------------------------------


class TransferCancelled(Exception):
    """Raised when the user cancels a running operation."""


class UnknownHostKey(Exception):
    """The server presented a host key that is not in any known_hosts file."""

    def __init__(self, hostname: str, key: paramiko.PKey):
        self.hostname = hostname
        self.key = key
        super().__init__(f"Unknown host key for {hostname}: {self.fingerprint}")

    @property
    def key_type(self) -> str:
        return self.key.get_name()

    @property
    def fingerprint(self) -> str:
        digest = hashlib.sha256(self.key.asbytes()).digest()
        return "SHA256:" + base64.b64encode(digest).decode().rstrip("=")


class _ReportUnknownHostKey(paramiko.MissingHostKeyPolicy):
    def missing_host_key(self, client, hostname, key):
        raise UnknownHostKey(hostname, key)


def trust_host_key(hostname: str, key: paramiko.PKey, path: Optional[str] = None) -> None:
    """Store a host key the user accepted in LXSCP's own known_hosts file."""
    path = path or known_hosts_file()
    keys = paramiko.HostKeys()
    if os.path.exists(path):
        keys.load(path)
    keys.add(hostname, key.get_name(), key)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    keys.save(path)
    os.chmod(path, 0o600)


def _remote_owner(attr: paramiko.SFTPAttributes) -> str:
    longname = getattr(attr, "longname", None)
    if longname:
        parts = longname.split()
        if len(parts) >= 4 and parts[0][:1] in "-dlcbps":
            return parts[2]
    uid = getattr(attr, "st_uid", None)
    return "" if uid is None else str(uid)


# (source, destination, size, atime, mtime)
_Job = Tuple[str, str, int, Optional[float], Optional[float]]


class RemoteSession:
    """A single SSH/SFTP connection."""

    def __init__(self, known_hosts_path: Optional[str] = None):
        self.known_hosts_path = known_hosts_path or known_hosts_file()
        self.client: Optional[paramiko.SSHClient] = None
        self.sftp: Optional[paramiko.SFTPClient] = None
        self.host = ""
        self.port = 22
        self.username = ""
        self.home = "/"

    # -- connection -------------------------------------------------------

    def connect(
        self,
        host: str,
        port: int = 22,
        username: str = "",
        password: Optional[str] = None,
        key_filename: Optional[str] = None,
        passphrase: Optional[str] = None,
        timeout: float = 15.0,
    ) -> None:
        host = validate_host(host)
        port = validate_port(port)
        username = (username or "").strip()
        if not username:
            raise ValueError("User name is required.")

        self.disconnect()
        client = paramiko.SSHClient()
        client.load_system_host_keys()
        if os.path.exists(self.known_hosts_path):
            client.get_host_keys().load(self.known_hosts_path)
        client.set_missing_host_key_policy(_ReportUnknownHostKey())
        try:
            client.connect(
                hostname=host,
                port=port,
                username=username,
                password=password or None,
                key_filename=os.path.expanduser(key_filename) if key_filename else None,
                passphrase=passphrase or None,
                timeout=timeout,
                banner_timeout=timeout,
                auth_timeout=timeout,
                allow_agent=True,
                look_for_keys=not (password or key_filename),
            )
            transport = client.get_transport()
            if transport is not None:
                transport.set_keepalive(30)
            sftp = client.open_sftp()
            home = sftp.normalize(".")
        except BaseException:
            client.close()
            raise

        self.client, self.sftp = client, sftp
        self.host, self.port, self.username = host, port, username
        self.home = home or "/"

    def is_connected(self) -> bool:
        if self.sftp is None or self.client is None:
            return False
        transport = self.client.get_transport()
        return transport is not None and transport.is_active()

    def disconnect(self) -> None:
        for closer in (self.sftp, self.client):
            if closer is not None:
                try:
                    closer.close()
                except Exception:
                    pass
        self.sftp = None
        self.client = None

    def _require(self) -> paramiko.SFTPClient:
        if self.sftp is None:
            raise RuntimeError("Not connected to a server.")
        return self.sftp

    # -- browsing and simple commands -------------------------------------

    def list_dir(self, path: str) -> List[FileEntry]:
        sftp = self._require()
        path = normalize_remote_path(path)
        entries = []
        for attr in sftp.listdir_attr(path):
            if not is_safe_name(attr.filename):
                continue
            full = remote_join(path, attr.filename)
            mode = attr.st_mode or 0
            is_link = stat.S_ISLNK(mode)
            is_dir = stat.S_ISDIR(mode)
            size = attr.st_size or 0
            if is_link:
                try:
                    target = sftp.stat(full)
                    is_dir = stat.S_ISDIR(target.st_mode or 0)
                    size = target.st_size or 0
                except OSError:  # broken link
                    pass
            entries.append(
                FileEntry(
                    name=attr.filename,
                    path=full,
                    is_dir=is_dir,
                    size=0 if is_dir else size,
                    mtime=attr.st_mtime or 0,
                    mode=mode,
                    is_link=is_link,
                    owner=_remote_owner(attr),
                )
            )
        return sort_entries(entries)

    def exists(self, path: str) -> bool:
        try:
            self._require().stat(path)
            return True
        except FileNotFoundError:
            return False

    def mkdir(self, path: str) -> None:
        self._require().mkdir(path)

    def rename(self, old: str, new: str) -> None:
        self._require().rename(old, new)

    def chmod(self, path: str, mode: int) -> None:
        self._require().chmod(path, mode)

    def delete(self, paths: Sequence[str], cancel_event: Optional[threading.Event] = None) -> None:
        sftp = self._require()
        for path in paths:
            self._delete(sftp, normalize_remote_path(path), cancel_event)

    def _delete(self, sftp, path: str, cancel_event) -> None:
        _check_cancel(cancel_event)
        if path == "/":
            raise ValueError("Refusing to delete the root directory.")
        st = sftp.lstat(path)
        if stat.S_ISDIR(st.st_mode or 0):
            for attr in sftp.listdir_attr(path):
                if is_safe_name(attr.filename):
                    self._delete(sftp, remote_join(path, attr.filename), cancel_event)
            sftp.rmdir(path)
        else:
            sftp.remove(path)

    # -- transfers ----------------------------------------------------------

    def upload(
        self,
        local_paths: Sequence[str],
        remote_dir: str,
        progress: Optional[ProgressCallback] = None,
        cancel_event: Optional[threading.Event] = None,
        preserve_times: bool = True,
    ) -> int:
        """Upload files/folders into ``remote_dir``. Returns the number of files copied."""
        sftp = self._require()
        remote_dir = normalize_remote_path(remote_dir)
        dirs: List[str] = []
        jobs: List[_Job] = []
        for local in local_paths:
            local = os.path.abspath(local)
            target = remote_join(remote_dir, os.path.basename(local.rstrip(os.sep)))
            if os.path.isdir(local):
                dirs.append(target)
                for root, dnames, fnames in os.walk(local):
                    # Don't follow symlinked folders inside a tree (avoids loops).
                    dnames[:] = sorted(d for d in dnames if not os.path.islink(os.path.join(root, d)))
                    rel = os.path.relpath(root, local)
                    rbase = target if rel == "." else remote_join(target, *rel.split(os.sep))
                    if rel != ".":
                        dirs.append(rbase)
                    for fname in sorted(fnames):
                        fpath = os.path.join(root, fname)
                        try:
                            st = os.stat(fpath)
                        except OSError:  # broken symlink
                            continue
                        if stat.S_ISREG(st.st_mode):
                            jobs.append((fpath, remote_join(rbase, fname), st.st_size, st.st_atime, st.st_mtime))
            else:
                st = os.stat(local)
                jobs.append((local, target, st.st_size, st.st_atime, st.st_mtime))

        for d in dirs:
            _check_cancel(cancel_event)
            self._ensure_remote_dir(sftp, d)

        def put(src, dst, callback):
            sftp.put(src, dst, callback=callback)

        def cleanup(dst):
            try:
                sftp.remove(dst)
            except OSError:
                pass

        def set_times(dst, atime, mtime):
            sftp.utime(dst, (atime, mtime))

        return _run_jobs(jobs, put, cleanup, set_times if preserve_times else None, progress, cancel_event)

    def download(
        self,
        remote_paths: Sequence[str],
        local_dir: str,
        progress: Optional[ProgressCallback] = None,
        cancel_event: Optional[threading.Event] = None,
        preserve_times: bool = True,
    ) -> int:
        """Download files/folders into ``local_dir``. Returns the number of files copied."""
        sftp = self._require()
        dirs: List[str] = []
        jobs: List[_Job] = []
        for remote in remote_paths:
            remote = normalize_remote_path(remote)
            name = posixpath.basename(remote)
            if not is_safe_name(name):
                raise ValueError(f"Cannot download {remote!r}.")
            target = os.path.join(local_dir, name)
            st = sftp.stat(remote)
            if stat.S_ISDIR(st.st_mode or 0):
                self._plan_remote_dir(sftp, remote, target, dirs, jobs, cancel_event)
            else:
                jobs.append((remote, target, st.st_size or 0, st.st_atime, st.st_mtime))

        for d in dirs:
            os.makedirs(d, exist_ok=True)

        def get(src, dst, callback):
            sftp.get(src, dst, callback=callback)

        def cleanup(dst):
            try:
                os.remove(dst)
            except OSError:
                pass

        def set_times(dst, atime, mtime):
            if atime is not None and mtime is not None:
                os.utime(dst, (atime, mtime))

        return _run_jobs(jobs, get, cleanup, set_times if preserve_times else None, progress, cancel_event)

    def _plan_remote_dir(self, sftp, rdir, ldir, dirs, jobs, cancel_event) -> None:
        _check_cancel(cancel_event)
        dirs.append(ldir)
        for attr in sorted(sftp.listdir_attr(rdir), key=lambda a: a.filename):
            if not is_safe_name(attr.filename):
                continue
            rchild = remote_join(rdir, attr.filename)
            lchild = os.path.join(ldir, attr.filename)
            mode = attr.st_mode or 0
            if stat.S_ISDIR(mode):
                self._plan_remote_dir(sftp, rchild, lchild, dirs, jobs, cancel_event)
            elif stat.S_ISREG(mode):
                jobs.append((rchild, lchild, attr.st_size or 0, attr.st_atime, attr.st_mtime))
            elif stat.S_ISLNK(mode):
                # Follow links to files; skip links to folders (avoids loops).
                try:
                    target = sftp.stat(rchild)
                except OSError:
                    continue
                if stat.S_ISREG(target.st_mode or 0):
                    jobs.append((rchild, lchild, target.st_size or 0, target.st_atime, target.st_mtime))

    @staticmethod
    def _ensure_remote_dir(sftp, path: str) -> None:
        try:
            st = sftp.stat(path)
        except FileNotFoundError:
            sftp.mkdir(path)
            return
        if not stat.S_ISDIR(st.st_mode or 0):
            raise NotADirectoryError(f"{path} exists and is not a folder.")


def _check_cancel(cancel_event: Optional[threading.Event]) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise TransferCancelled()


def _run_jobs(jobs: List[_Job], copy, cleanup, set_times, progress, cancel_event) -> int:
    total = sum(job[2] for job in jobs)
    done = 0
    for src, dst, size, atime, mtime in jobs:
        _check_cancel(cancel_event)
        base = done

        def callback(transferred, _size, base=base, dst=dst):
            _check_cancel(cancel_event)
            if progress is not None:
                progress(base + transferred, total, dst)

        if progress is not None:
            progress(base, total, dst)
        try:
            copy(src, dst, callback)
        except TransferCancelled:
            cleanup(dst)
            raise
        if set_times is not None:
            set_times(dst, atime, mtime)
        done += size
        if progress is not None:
            progress(done, total, dst)
    return len(jobs)
