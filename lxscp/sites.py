"""Saved sessions ("sites"), stored as JSON. Passwords are never saved."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, fields
from typing import List, Optional

from lxscp.core import config_dir


@dataclass
class Site:
    name: str
    host: str
    port: int = 22
    username: str = ""
    key_file: str = ""
    remote_dir: str = ""
    local_dir: str = ""


class SiteStore:
    def __init__(self, path: Optional[str] = None):
        self.path = path or os.path.join(config_dir(), "sites.json")

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

    def upsert(self, site: Site) -> None:
        sites = [s for s in self.load() if s.name != site.name]
        sites.append(site)
        self.save_all(sites)

    def delete(self, name: str) -> None:
        self.save_all([s for s in self.load() if s.name != name])
