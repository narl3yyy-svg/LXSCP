#!/usr/bin/env bash
# Build and install LXSCP as a native package on Ubuntu/Debian or Arch Linux.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

if command -v apt-get >/dev/null 2>&1; then
    echo "==> Ubuntu/Debian detected"
    ./packaging/debian/build-deb.sh
    sudo apt-get install -y "./dist/lxscp_$(sed -n 's/^__version__ = "\(.*\)"/\1/p' lxscp/__init__.py)_all.deb"
elif command -v pacman >/dev/null 2>&1; then
    echo "==> Arch Linux detected"
    sudo pacman -S --needed --noconfirm base-devel python-build python-installer python-setuptools python-wheel
    (cd packaging/arch && makepkg -si --noconfirm)
else
    echo "Unsupported distribution: install python3, PyQt6 and paramiko, then run: python3 -m lxscp" >&2
    exit 1
fi
echo "==> Installed. Start LXSCP from your application menu or run: lxscp"
