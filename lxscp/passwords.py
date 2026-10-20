"""Saved site passwords.

Passwords go into the desktop keyring (GNOME Keyring / KWallet via the
``keyring`` package) when one is available. Without a keyring they fall back
to ``~/.config/lxscp/passwords.json``, readable only by the current user.
"""

from __future__ import annotations

import json
import os
from typing import Dict, Optional

from lxscp.core import config_dir

SERVICE = "lxscp"


def _keyring():
    try:
        import keyring
        from keyring.backends import fail
    except Exception:  # noqa: BLE001 - keyring is optional
        return None
    try:
        backend = keyring.get_keyring()
    except Exception:  # noqa: BLE001
        return None
    if isinstance(backend, fail.Keyring) or getattr(backend, "backends", None) == []:
        return None
    return keyring


class PasswordStore:
    def __init__(self, path: Optional[str] = None, use_keyring: bool = True):
        self.path = path or os.path.join(config_dir(), "passwords.json")
        self._keyring = _keyring() if use_keyring else None

    @property
    def backend_name(self) -> str:
        return "system keyring" if self._keyring else "a private file in ~/.config/lxscp"

    def get(self, site: str) -> str:
        if self._keyring is not None:
            try:
                value = self._keyring.get_password(SERVICE, site)
                if value is not None:
                    return value
            except Exception:  # noqa: BLE001 - locked/broken keyring: use the file
                pass
        return self._load().get(site, "")

    def set(self, site: str, password: str) -> None:
        if self._keyring is not None:
            try:
                self._keyring.set_password(SERVICE, site, password)
                self._remove_from_file(site)
                return
            except Exception:  # noqa: BLE001
                pass
        data = self._load()
        data[site] = password
        self._save(data)

    def delete(self, site: str) -> None:
        if self._keyring is not None:
            try:
                self._keyring.delete_password(SERVICE, site)
            except Exception:  # noqa: BLE001 - not stored there
                pass
        self._remove_from_file(site)

    def _remove_from_file(self, site: str) -> None:
        data = self._load()
        if site in data:
            del data[site]
            self._save(data)

    def _load(self) -> Dict[str, str]:
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return {}
        return {k: v for k, v in data.items() if isinstance(v, str)} if isinstance(data, dict) else {}

    def _save(self, data: Dict[str, str]) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        os.replace(tmp, self.path)
