#!/usr/bin/env bash
# Build an Ubuntu/Debian package: dist/lxscp_<version>_all.deb
# Install it with:  sudo apt install ./dist/lxscp_<version>_all.deb
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
VERSION="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$ROOT/lxscp/__init__.py")"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

PKG="$STAGE/lxscp_${VERSION}_all"
install -d "$PKG/DEBIAN" "$PKG/usr/lib/python3/dist-packages/lxscp/icons"
install -m644 "$ROOT"/lxscp/*.py "$PKG/usr/lib/python3/dist-packages/lxscp/"
install -m644 "$ROOT"/lxscp/icons/* "$PKG/usr/lib/python3/dist-packages/lxscp/icons/"
install -Dm755 "$ROOT/packaging/lxscp-launcher" "$PKG/usr/bin/lxscp"
install -Dm644 "$ROOT/packaging/lxscp.desktop" "$PKG/usr/share/applications/lxscp.desktop"
for size in 16 32 48 64 128 256; do
    install -Dm644 "$ROOT/lxscp/icons/lxscp-$size.png" "$PKG/usr/share/icons/hicolor/${size}x${size}/apps/lxscp.png"
done
install -Dm644 "$ROOT/lxscp/icons/lxscp.svg" "$PKG/usr/share/icons/hicolor/scalable/apps/lxscp.svg"
install -Dm644 "$ROOT/LICENSE" "$PKG/usr/share/doc/lxscp/copyright"

SIZE="$(du -sk "$PKG/usr" | cut -f1)"
cat > "$PKG/DEBIAN/control" <<CONTROL
Package: lxscp
Version: ${VERSION}
Section: net
Priority: optional
Architecture: all
Depends: python3 (>= 3.10), python3-pyqt6, python3-paramiko, python3-keyring
Installed-Size: ${SIZE}
Maintainer: narl3yyy-svg <narl3yyy@gmail.com>
Homepage: https://github.com/narl3yyy-svg/LXSCP
Description: WinSCP-style graphical SFTP client
 LXSCP is a dual-pane (commander style) SFTP/SSH file transfer client
 for Linux, modelled on WinSCP: saved sessions, upload/download with
 progress, drag and drop, rename, delete, new folder, permissions and
 remote file editing.
CONTROL

mkdir -p "$ROOT/dist"
dpkg-deb --root-owner-group --build "$PKG" "$ROOT/dist/lxscp_${VERSION}_all.deb"
echo "Built $ROOT/dist/lxscp_${VERSION}_all.deb"
