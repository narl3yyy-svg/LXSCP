"""Saved sessions ("sites"), stored as JSON. Passwords are kept separately (see passwords.py)."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, fields
from typing import List, Optional

from lxscp.core import config_dir
from lxscp.passwords import PasswordStore


@dataclass
class Site:
    name: str
    host: str
    port: int = 22
    username: str = ""
    key_file: str = ""
    remote_dir: str = ""
    local_dir: str = ""
    save_password: bool = False
    save_passphrase: bool = False


class SiteStore:
    def __init__(self, path: Optional[str] = None, passwords: Optional[PasswordStore] = None):
        self.path = path or os.path.join(config_dir(), "sites.json")
        self.passwords = passwords or PasswordStore()

    def load(self) -> List[Site]:
        try:
            with open(self.path, encoding="utf-8") as fh:
                raw = json.load(fh)
        except (OSError, ValueError):
            return []
        known = {f.name for f in fields(Site)}
        sites = []
        for item in raw if isinstance(raw, list) else []:
            if isinstance(item, dict) and item.get("name") and item.get("host"):
                sites.append(Site(**{k: v for k, v in item.items() if k in known}))
        return sorted(sites, key=lambda s: s.name.casefold())

    def save_all(self, sites: List[Site]) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump([asdict(s) for s in sites], fh, indent=2)
        os.replace(tmp, self.path)

    def upsert(self, site: Site, password: str = "", passphrase: str = "") -> None:
        self._store_secrets(site, password, passphrase)
        sites = [s for s in self.load() if s.name != site.name]
        sites.append(site)
        self.save_all(sites)

    def update_secrets(self, name: str, save_password: bool, password: str,
                       save_passphrase: bool, passphrase: str) -> None:
        """Remember (or forget) the secrets of an existing site, keeping its other settings."""
        sites = self.load()
        for site in sites:
            if site.name == name:
                site.save_password, site.save_passphrase = save_password, save_passphrase
                self._store_secrets(site, password, passphrase)
                self.save_all(sites)
                return

    def _store_secrets(self, site: Site, password: str, passphrase: str) -> None:
        site.save_password = bool(site.save_password and password)
        site.save_passphrase = bool(site.save_passphrase and passphrase)
        if site.save_password:
            self.passwords.set(site.name, password)
        else:
            self.passwords.delete(site.name)
        if site.save_passphrase:
            self.passwords.set(passphrase_key(site.name), passphrase)
        else:
            self.passwords.delete(passphrase_key(site.name))

    def password(self, site: Site) -> str:
        return self.passwords.get(site.name) if site.save_password else ""

    def passphrase(self, site: Site) -> str:
        return self.passwords.get(passphrase_key(site.name)) if site.save_passphrase else ""

    def delete(self, name: str) -> None:
        self.save_all([s for s in self.load() if s.name != name])
        self.passwords.delete(name)
        self.passwords.delete(passphrase_key(name))
        if self.last_used() == name:
            self.set_last_used("")

    # The site selected the last time the user logged in.
    def _last_used_path(self) -> str:
        return os.path.join(os.path.dirname(self.path), "last_site")

    def last_used(self) -> str:
        try:
            with open(self._last_used_path(), encoding="utf-8") as fh:
                return fh.read().strip()
        except OSError:
            return ""

    def set_last_used(self, name: str) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self._last_used_path(), "w", encoding="utf-8") as fh:
            fh.write(name)


def passphrase_key(name: str) -> str:
    return f"{name} (key passphrase)"
