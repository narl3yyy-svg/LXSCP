#!/usr/bin/env bash
# LXSCP installer for Linux (Ubuntu/Debian and Arch Linux).
#
#   ./install.sh              install LXSCP, or update it after a `git pull`
#   ./install.sh --uninstall  remove LXSCP (keeps your saved sites and settings)
#   ./install.sh --purge      remove LXSCP and also delete saved sites, saved
#                             passwords/passphrases and accepted host keys
#
# For macOS use ./installmac.sh instead.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VERSION="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$REPO_DIR/lxscp/__init__.py")"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/lxscp"

info() { printf '==> %s\n' "$*"; }
die() { printf 'Error: %s\n' "$*" >&2; exit 1; }

usage() {
    sed -n '2,10s/^# \{0,1\}//p' "${BASH_SOURCE[0]}"
}

detect_distro() {
    if [ "$(uname -s)" = "Darwin" ]; then
        die "this is the Linux installer. On a Mac run: ./installmac.sh"
    elif command -v apt-get >/dev/null 2>&1; then
        echo debian
    elif command -v pacman >/dev/null 2>&1; then
        echo arch
    else
        die "unsupported distribution (needs apt or pacman). You can still run LXSCP from source, see README.md."
    fi
}

install_lxscp() {
    case "$(detect_distro)" in
        debian)
            info "Ubuntu/Debian detected - building lxscp_${VERSION}_all.deb"
            "$REPO_DIR/packaging/debian/build-deb.sh"
            info "Installing (sudo may ask for your password)"
            # --reinstall so re-running after a `git pull` always installs the new build
            sudo apt-get install -y --reinstall "$REPO_DIR/dist/lxscp_${VERSION}_all.deb"
            ;;
        arch)
            info "Arch Linux detected - installing build tools"
            sudo pacman -S --needed --noconfirm base-devel python-build python-installer python-setuptools python-wheel
            info "Building and installing the package"
            (cd "$REPO_DIR/packaging/arch" && makepkg -sif --noconfirm)
            ;;
    esac
    info "LXSCP $VERSION installed. Start it from your application menu or run: lxscp"
}

uninstall_lxscp() {
    case "$(detect_distro)" in
        debian)
            if dpkg-query -W -f='${Status}' lxscp 2>/dev/null | grep -q "install ok installed"; then
                info "Removing the lxscp package (sudo may ask for your password)"
                sudo apt-get remove -y lxscp
            else
                info "LXSCP is not installed"
            fi
            ;;
        arch)
            if pacman -Q lxscp >/dev/null 2>&1; then
                info "Removing the lxscp package (sudo may ask for your password)"
                sudo pacman -R --noconfirm lxscp
            else
                info "LXSCP is not installed"
            fi
            ;;
    esac
}

purge_settings() {
    if [ -t 0 ]; then
        printf 'This deletes your saved sites, saved passwords/passphrases and accepted host keys.\nContinue? [y/N] '
        read -r answer
        case "$answer" in
            y|Y|yes|YES) ;;
            *) info "Kept your settings"; return ;;
        esac
    fi
    info "Deleting saved passwords from the keyring"
    PYTHONPATH="$REPO_DIR" python3 -m lxscp.passwords --forget-all \
        || printf 'Warning: could not reach the keyring; remove "lxscp" entries with your keyring manager.\n' >&2
    info "Deleting $CONFIG_DIR"
    rm -rf "$CONFIG_DIR"
}

case "${1:-}" in
    "")
        install_lxscp
        ;;
    --uninstall)
        uninstall_lxscp
        info "Done. Your saved sites are kept in $CONFIG_DIR (use --purge to delete them)."
        ;;
    --purge)
        uninstall_lxscp
        purge_settings
        info "Done."
        ;;
    -h|--help)
        usage
        ;;
    *)
        usage >&2
        exit 2
        ;;
esac
