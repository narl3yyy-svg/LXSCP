"""A small in-process SSH/SFTP server used by the tests.

It serves a local directory as the remote file system root and accepts a
single user/password. Never use this outside of tests.
"""

from __future__ import annotations

import os
import socket
import threading

import paramiko
from paramiko import SFTPAttributes, SFTPHandle, SFTPServer, SFTPServerInterface
from paramiko.sftp import SFTP_OK

USERNAME = "tester"
PASSWORD = "secret"


class _Server(paramiko.ServerInterface):
    def check_auth_password(self, username, password):
        if username == USERNAME and password == PASSWORD:
            return paramiko.AUTH_SUCCESSFUL
        return paramiko.AUTH_FAILED

    def check_auth_publickey(self, username, key):
        return paramiko.AUTH_FAILED

    def get_allowed_auths(self, username):
        return "password"

    def check_channel_request(self, kind, chanid):
        return paramiko.OPEN_SUCCEEDED


class _Handle(SFTPHandle):
    def stat(self):
        try:
            return SFTPAttributes.from_stat(os.fstat(self.readfile.fileno()))
        except OSError as e:
            return SFTPServer.convert_errno(e.errno)

    def chattr(self, attr):
        try:
            SFTPServer.set_file_attr(self.filename, attr)
            return SFTP_OK
        except OSError as e:
            return SFTPServer.convert_errno(e.errno)


def make_sftp_interface(root: str):
    class _SFTP(SFTPServerInterface):
        ROOT = root

        def _real(self, path):
            return self.ROOT + self.canonicalize(path)

        def list_folder(self, path):
            real = self._real(path)
            try:
                out = []
                for name in os.listdir(real):
                    attr = SFTPAttributes.from_stat(os.lstat(os.path.join(real, name)))
                    attr.filename = name
                    out.append(attr)
                return out
            except OSError as e:
                return SFTPServer.convert_errno(e.errno)

        def stat(self, path):
            try:
                return SFTPAttributes.from_stat(os.stat(self._real(path)))
            except OSError as e:
                return SFTPServer.convert_errno(e.errno)

        def lstat(self, path):
            try:
                return SFTPAttributes.from_stat(os.lstat(self._real(path)))
            except OSError as e:
                return SFTPServer.convert_errno(e.errno)

        def open(self, path, flags, attr):
            real = self._real(path)
            try:
                binary = getattr(os, "O_BINARY", 0)
                mode = getattr(attr, "st_mode", None) or 0o666
                fd = os.open(real, flags | binary, mode)
            except OSError as e:
                return SFTPServer.convert_errno(e.errno)
            if flags & os.O_CREAT and attr is not None:
                attr._flags &= ~attr.FLAG_PERMISSIONS
                SFTPServer.set_file_attr(real, attr)
            if flags & os.O_WRONLY:
                fstr = "ab" if flags & os.O_APPEND else "wb"
            elif flags & os.O_RDWR:
                fstr = "a+b" if flags & os.O_APPEND else "r+b"
            else:
                fstr = "rb"
            try:
                f = os.fdopen(fd, fstr)
            except OSError as e:
                return SFTPServer.convert_errno(e.errno)
            handle = _Handle(flags)
            handle.filename = real
            handle.readfile = f
            handle.writefile = f
            return handle

        def remove(self, path):
            try:
                os.remove(self._real(path))
            except OSError as e:
                return SFTPServer.convert_errno(e.errno)
            return SFTP_OK

        def rename(self, oldpath, newpath):
            try:
                os.rename(self._real(oldpath), self._real(newpath))
            except OSError as e:
                return SFTPServer.convert_errno(e.errno)
            return SFTP_OK

        def mkdir(self, path, attr):
            try:
                os.mkdir(self._real(path))
                if attr is not None:
                    SFTPServer.set_file_attr(self._real(path), attr)
            except OSError as e:
                return SFTPServer.convert_errno(e.errno)
            return SFTP_OK

        def rmdir(self, path):
            try:
                os.rmdir(self._real(path))
            except OSError as e:
                return SFTPServer.convert_errno(e.errno)
            return SFTP_OK

        def chattr(self, path, attr):
            try:
                SFTPServer.set_file_attr(self._real(path), attr)
            except OSError as e:
                return SFTPServer.convert_errno(e.errno)
            return SFTP_OK

        def symlink(self, target_path, path):
            try:
                os.symlink(target_path, self._real(path))
            except OSError as e:
                return SFTPServer.convert_errno(e.errno)
            return SFTP_OK

        def readlink(self, path):
            try:
                return os.readlink(self._real(path))
            except OSError as e:
                return SFTPServer.convert_errno(e.errno)

    return _SFTP


class SFTPTestServer:
    def __init__(self, root: str):
        self.root = root
        self.host_key = paramiko.RSAKey.generate(2048)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.port = self.sock.getsockname()[1]
        self.transports = []
        self._stop = False
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        while not self._stop:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            transport = paramiko.Transport(conn)
            transport.add_server_key(self.host_key)
            transport.set_subsystem_handler("sftp", SFTPServer, make_sftp_interface(self.root))
            try:
                transport.start_server(server=_Server())
            except Exception:
                transport.close()
                continue
            self.transports.append(transport)

    def stop(self):
        self._stop = True
        try:
            self.sock.close()
        except OSError:
            pass
        for t in self.transports:
            t.close()
