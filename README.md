# LXSCP

**LXSCP** is a WinSCP-style graphical SFTP client for Linux (Ubuntu/Debian and Arch).
It uses the familiar WinSCP *Commander* layout: your local files on the left, the
remote server on the right, and a toolbar and keyboard shortcuts for moving files
between them over SSH/SFTP.

![LXSCP main window](docs/screenshot.png)

## Features

- Dual-pane commander interface (local | remote) with Name, Size, Type, Changed, Rights and Owner columns
- WinSCP-style **Login** dialog with saved sites
- SSH password, private-key file (with passphrase) and ssh-agent authentication
- Optional **Save password** and **Save passphrase** per site, stored in your desktop keyring
  (GNOME Keyring / KWallet); if you don't save them, LXSCP asks when you connect
- The login dialog reopens on the last site you used, with its saved password/passphrase filled in
- Host key verification: unknown keys show the SHA256 fingerprint and must be accepted;
  changed keys are refused (keys from `~/.ssh/known_hosts` are trusted automatically)
- Upload / download of files and whole folders, with a progress dialog, speed, and **Cancel**
- Drag & drop between the panels, and from your file manager onto the remote panel
- Overwrite confirmation; timestamps are preserved
- Rename, delete (recursive), create folder, properties with **chmod** (checkboxes or octal)
- **Edit remote files**: opens a copy in your default application and uploads it back every time you save
- Path bar, parent/home/refresh buttons, sortable columns (folders always first)
- Open a session from the command line: `lxscp sftp://user@host:2222/var/www`

### Keyboard shortcuts

| Key | Action | Key | Action |
| --- | --- | --- | --- |
| `Ctrl+N` | New session (login) | `F2` | Rename |
| `F5` | Copy selection to the other panel | `F4` | Edit / open file |
| `F7` | New folder | `F8` / `Delete` | Delete |
| `F9` | Properties / permissions | `Ctrl+R` | Refresh |
| `Backspace` | Parent directory | `Enter` | Open folder / file |
| `Tab` | Switch panel | `Ctrl+Q` | Quit |

## Connecting

Click **New Session** (`Ctrl+N`), fill in *Host name*, *Port number*, *User name* and *Password*, then
press **Login**. To keep the connection for next time press **Save**, and tick **Save password** if you
want the password remembered too. For key-based logins, choose a *Private key file* and, if the key is
encrypted, enter its *Key passphrase* and tick **Save passphrase**. Tick **Show** to see what you typed.

Ticking or unticking the save boxes on a saved site and pressing **Login** updates what is stored, so
you don't have to press Save again. Next time you open the login dialog (for example after
**Disconnect**), your last site is already selected with its password and passphrase filled in.

![LXSCP login dialog](docs/login.png)

## Install

Clone the repository first:

```bash
git clone https://github.com/narl3yyy-svg/LXSCP.git
cd LXSCP
```

Then either run `./install.sh` (it detects Ubuntu or Arch and does the steps below), or:

### Ubuntu / Debian (.deb)

```bash
./packaging/debian/build-deb.sh
sudo apt install ./dist/lxscp_0.1.0_all.deb
```

The package depends on `python3-pyqt6`, `python3-paramiko` and `python3-keyring`, which apt installs
automatically (Ubuntu 22.04 or newer).

### Arch Linux (PKGBUILD)

```bash
sudo pacman -S --needed base-devel python-build python-installer python-setuptools python-wheel
cd packaging/arch
makepkg -si
```

After installing, start **LXSCP** from the application menu or run `lxscp`.

### Run from source (any distro)

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m lxscp
```

## Uninstall

- Ubuntu/Debian: `sudo apt remove lxscp`
- Arch: `sudo pacman -R lxscp`

Settings live in `~/.config/lxscp/` (saved sites and accepted host keys). Saved passwords are kept in
the desktop keyring; only if no keyring is available are they written to `~/.config/lxscp/passwords.json`
(readable only by your user).

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -v
```

The test suite starts a throw-away in-process SFTP server (`tests/sftp_server.py`),
so it needs no SSH server or network access. It covers the SFTP session (host keys,
authentication, transfers, cancel, rename/delete/chmod) and drives the real main
window offscreen.

## Notes

LXSCP is an independent open-source project inspired by WinSCP's workflow. It does
not use any WinSCP code or artwork and supports SFTP only.

## License

MIT © narl3yyy-svg
