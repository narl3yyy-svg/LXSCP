#!/bin/bash
# LXSCP installer for macOS (Apple Silicon and Intel, macOS 11 Big Sur or newer).
#
#   ./installmac.sh              install LXSCP, or update it after a `git pull`
#   ./installmac.sh --uninstall  remove LXSCP (keeps your saved sites and settings)
#   ./installmac.sh --purge      remove LXSCP and also delete saved sites, saved
#                                passwords/passphrases and accepted host keys
#
# LXSCP is installed as /Applications/LXSCP.app (or ~/Applications/LXSCP.app if
# /Applications is not writable). No administrator password is needed. Python 3.10+
# is required; if it is missing and Homebrew is installed, the script installs it.
#
# For Linux use ./install.sh instead.
#
# Note: written for the bash 3.2 that ships with macOS - keep it that way.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
VERSION="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$REPO_DIR/lxscp/__init__.py")"
SUPPORT_DIR="$HOME/Library/Application Support/LXSCP"
VENV="$SUPPORT_DIR/venv"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/lxscp"
PREFS_DOMAIN="com.lxscp.lxscp"
APP_LOCATIONS="/Applications $HOME/Applications"
CLI_DIRS="/opt/homebrew/bin /usr/local/bin"
APP_PATH=""

info() { printf '==> %s\n' "$*"; }
warn() { printf 'Warning: %s\n' "$*" >&2; }
die() { printf 'Error: %s\n' "$*" >&2; exit 1; }

usage() {
    sed -n '2,15s/^# \{0,1\}//p' "$0"
}

require_macos() {
    [ "$(uname -s)" = "Darwin" ] || die "this is the macOS installer. On Linux run: ./install.sh"
}

require_macos_version() {
    local major
    major="$(sw_vers -productVersion | cut -d. -f1)"
    if [ "$major" -lt 11 ]; then
        die "LXSCP needs macOS 11 (Big Sur) or newer; this Mac runs $(sw_vers -productVersion)."
    fi
}

python_ok() {
    "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1
}

find_python() {
    local candidate path
    for candidate in python3.13 python3.12 python3.11 python3.10 \
        /opt/homebrew/bin/python3 /usr/local/bin/python3 \
        /Library/Frameworks/Python.framework/Versions/3.1[0-9]/bin/python3 \
        python3; do
        path="$(command -v "$candidate" 2>/dev/null)" || continue
        # Apple's /usr/bin/python3 opens an "install developer tools" popup when the
        # Command Line Tools are missing, so only try it when they are installed.
        if [ "$path" = "/usr/bin/python3" ] && ! xcode-select -p >/dev/null 2>&1; then
            continue
        fi
        if python_ok "$path"; then
            echo "$path"
            return 0
        fi
    done
    return 1
}

find_brew() {
    local b
    for b in brew /opt/homebrew/bin/brew /usr/local/bin/brew; do
        if command -v "$b" >/dev/null 2>&1; then
            command -v "$b"
            return 0
        fi
    done
    return 1
}

get_python() {
    local py brew
    if py="$(find_python)"; then
        echo "$py"
        return 0
    fi
    if brew="$(find_brew)"; then
        info "Python 3.10 or newer not found - installing Python 3.12 with Homebrew" >&2
        "$brew" install python@3.12 >&2
        py="$("$brew" --prefix)/opt/python@3.12/bin/python3.12"
        python_ok "$py" || die "Homebrew's Python could not be started ($py)."
        echo "$py"
        return 0
    fi
    die "Python 3.10 or newer is required.
Install it from https://www.python.org/downloads/macos/ (or install Homebrew from https://brew.sh),
then run ./installmac.sh again."
}

app_dir() {
    if [ -w /Applications ]; then
        echo "/Applications/LXSCP.app"
    else
        mkdir -p "$HOME/Applications"
        echo "$HOME/Applications/LXSCP.app"
    fi
}

remove_apps() {
    local dir
    for dir in $APP_LOCATIONS; do
        if [ -d "$dir/LXSCP.app" ]; then
            info "Removing $dir/LXSCP.app"
            rm -rf "$dir/LXSCP.app"
        fi
    done
}

remove_cli_links() {
    local dir
    for dir in $CLI_DIRS; do
        if [ -L "$dir/lxscp" ] && readlink "$dir/lxscp" | grep -q "LXSCP.app"; then
            info "Removing $dir/lxscp"
            rm -f "$dir/lxscp"
        fi
    done
}

# Creates the .app bundle and sets APP_PATH.
build_app() {
    local app tmp lsregister
    app="$(app_dir)"
    remove_apps
    info "Creating $app"
    mkdir -p "$app/Contents/MacOS" "$app/Contents/Resources"

    cat > "$app/Contents/MacOS/LXSCP" <<LAUNCHER
#!/bin/bash
exec "$VENV/bin/python" -m lxscp "\$@"
LAUNCHER
    chmod 755 "$app/Contents/MacOS/LXSCP"

    cat > "$app/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key>
    <string>LXSCP</string>
    <key>CFBundleDisplayName</key>
    <string>LXSCP</string>
    <key>CFBundleIdentifier</key>
    <string>io.github.narl3yyy-svg.lxscp</string>
    <key>CFBundleExecutable</key>
    <string>LXSCP</string>
    <key>CFBundleIconFile</key>
    <string>lxscp</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleShortVersionString</key>
    <string>$VERSION</string>
    <key>CFBundleVersion</key>
    <string>$VERSION</string>
    <key>LSMinimumSystemVersion</key>
    <string>11.0</string>
    <key>LSApplicationCategoryType</key>
    <string>public.app-category.utilities</string>
    <key>NSHighResolutionCapable</key>
    <true/>
</dict>
</plist>
PLIST

    tmp="$(mktemp -d)"
    if "$VENV/bin/python" "$REPO_DIR/packaging/macos/make_iconset.py" \
            "$REPO_DIR/lxscp/icons/lxscp.svg" "$tmp/lxscp.iconset" \
        && iconutil -c icns "$tmp/lxscp.iconset" -o "$app/Contents/Resources/lxscp.icns"; then
        :
    else
        warn "could not create the app icon; LXSCP will still work"
    fi
    rm -rf "$tmp"

    # Make Finder/Launchpad notice the new or updated app.
    touch "$app"
    lsregister="/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister"
    if [ -x "$lsregister" ]; then
        "$lsregister" -f "$app" >/dev/null 2>&1 || true
    fi
    APP_PATH="$app"
}

link_cli() {
    local app="$1" dir
    remove_cli_links
    for dir in $CLI_DIRS; do
        if [ -d "$dir" ] && [ -w "$dir" ]; then
            ln -sf "$app/Contents/MacOS/LXSCP" "$dir/lxscp"
            info "Command line launcher: $dir/lxscp"
            return 0
        fi
    done
    info "No writable bin folder for a 'lxscp' command; open the app with: open -a LXSCP"
}

install_lxscp() {
    local py
    require_macos
    require_macos_version
    py="$(get_python)"
    info "Using $("$py" -V 2>&1) at $py"

    mkdir -p "$SUPPORT_DIR"
    if [ -d "$VENV" ] && ! python_ok "$VENV/bin/python"; then
        info "Recreating the Python environment (Python was upgraded or moved)"
        rm -rf "$VENV"
    fi
    if [ ! -d "$VENV" ]; then
        info "Creating the Python environment in $VENV"
        "$py" -m venv "$VENV"
    fi

    info "Installing LXSCP $VERSION and its dependencies (PyQt6, paramiko, keyring)"
    "$VENV/bin/python" -m pip install --quiet --upgrade pip
    "$VENV/bin/python" -m pip install --quiet --upgrade "$REPO_DIR"
    # Always replace LXSCP itself, even when the version number did not change.
    "$VENV/bin/python" -m pip install --quiet --force-reinstall --no-deps "$REPO_DIR"
    "$VENV/bin/python" -c "import lxscp.app" || die "LXSCP was installed but cannot be started."

    build_app
    link_cli "$APP_PATH"

    info "LXSCP $VERSION installed."
    info "Start it from Launchpad/Applications, or run: open -a LXSCP"
}

uninstall_lxscp() {
    require_macos
    osascript -e 'tell application "LXSCP" to quit' >/dev/null 2>&1 || true
    remove_apps
    remove_cli_links
    if [ -d "$SUPPORT_DIR" ]; then
        info "Removing $SUPPORT_DIR"
        rm -rf "$SUPPORT_DIR"
    fi
}

purge_settings() {
    local answer
    if [ -t 0 ]; then
        printf 'This deletes your saved sites, saved passwords/passphrases and accepted host keys.\nContinue? [y/N] '
        read -r answer
        case "$answer" in
            y|Y|yes|YES) ;;
            *) info "Kept your settings"; return 1 ;;
        esac
    fi
    return 0
}

forget_passwords() {
    # Runs before the Python environment is removed: it holds the keyring module.
    info "Deleting saved passwords from the Keychain"
    if [ -x "$VENV/bin/python" ]; then
        "$VENV/bin/python" -m lxscp.passwords --forget-all \
            || warn "could not reach the Keychain; delete 'lxscp' items in Keychain Access"
    else
        while security delete-generic-password -s lxscp >/dev/null 2>&1; do :; done
    fi
}

purge_config() {
    info "Deleting $CONFIG_DIR"
    rm -rf "$CONFIG_DIR"
    defaults delete "$PREFS_DOMAIN" >/dev/null 2>&1 || true
    rm -f "$HOME/Library/Preferences/$PREFS_DOMAIN.plist"
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
        require_macos
        if purge_settings; then
            forget_passwords
            uninstall_lxscp
            purge_config
        else
            uninstall_lxscp
        fi
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
