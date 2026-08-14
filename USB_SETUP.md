# USB drive setup

How to build a deployment drive from nothing. Do this once per drive, on a
computer you trust; a technician then only needs the drive itself.

Time: about 20 minutes plus however long the downloads take.

You need: a USB drive (16 GB or more), the vendor installers you want to
deploy, and the `USBInstaller` release for each platform you support.

---

## Step 1 — Format the drive

| Filesystem | Windows | macOS | Verdict |
| --- | --- | --- | --- |
| **exFAT** | read/write | read/write | **Use this.** Works on both, no 4 GB file limit. |
| FAT32 | read/write | read/write | 4 GB per-file limit — too small for some installers. |
| NTFS | read/write | read only | Windows-only drives. |
| APFS / HFS+ | — | read/write | macOS-only drives. |

**Windows:** File Explorer → right-click the drive → *Format* → exFAT →
volume label `USB_INSTALLER`.

**macOS:** *Disk Utility* → select the **drive**, not the volume → *Erase* →
Format **ExFAT**, Scheme **GUID Partition Map**, name `USB_INSTALLER`.

> exFAT does not store the Unix execute bit. That is fine here by design:
> macOS shell installers are run as `/bin/sh <script>`, so a missing execute
> bit never causes a failure.

## Step 2 — Create the folder layout

Copy the `repository/` folder from this project to the root of the drive, or
create it by hand:

```
USB_INSTALLER/
├── Launch-Windows.cmd          double-click launcher (Windows)
├── Launch-macOS.command        double-click launcher (macOS)
├── bin/                        the USBInstaller executables
├── config/
│   ├── applications.json       the catalogue — the file you edit
│   └── settings.json           policy: checksums, timeouts, confirmation
├── installers/
│   ├── windows/<app>/…         vendor installers
│   └── macos/<app>/…
├── scripts/                    optional post-install scripts
├── checksums/checksums.json    SHA-256 of every declared installer
├── logs/                       created automatically per run
└── documentation/              notes for whoever uses this drive
```

```bash
# from a clone of this project
cp -r repository/ /Volumes/USB_INSTALLER/        # macOS
xcopy /E /I repository E:\                        # Windows
```

## Step 3 — Put the program on the drive

Copy the built executables into `bin/`:

```
bin/USBInstaller.exe            bin/USBInstaller.app   (or bin/USBInstaller)
bin/repository-manager.exe      bin/repository-manager
```

Both platforms' builds can live on the same drive — the launchers pick the
right one. Either take them from a release, or build them:

```bash
python -m pip install -e ".[build]"
python tools/build.py                 # on Windows → .exe
python tools/build.py --universal2    # on macOS  → Intel + Apple Silicon
```

The target computer does **not** need Python installed — the runtime is inside
the executable.

## Step 4 — Add the installers

Download each vendor installer and save it under `installers/<os>/<app-id>/`.
The exact path must match `config/applications.json`:

```
installers/windows/firefox/FirefoxSetup.exe
installers/macos/firefox/Firefox.dmg
```

Vendor installers are **not** shipped with this project — licensing forbids
redistribution, and you should always fetch them from the vendor yourself.

The catalogue that ships in `repository/config/applications.json` already
describes eight common applications (Firefox, Chrome, 7-Zip, VLC, VS Code,
Acrobat Reader, Zoom, Notepad++). Drop the matching files into place, or edit
the catalogue to match what you actually deploy.

Two ways to add software:

- **Through the GUI** — launch the app and choose *Add Application…*; it copies
  the installer, writes the catalogue entry and records the checksum for you.
  See [USER_GUIDE.md](USER_GUIDE.md#adding-software-to-the-drive).
- **By hand** — edit `applications.json` following
  [ADDING_SOFTWARE.md](ADDING_SOFTWARE.md).

## Step 5 — Record checksums

```bash
repository-manager --repository /Volumes/USB_INSTALLER --checksums
```

This hashes every installer the catalogue declares and writes
`checksums/checksums.json`. Files the catalogue does *not* declare are ignored,
so a stray file cannot be blessed by accident.

Re-run this **every time you replace an installer** — otherwise the drive will
refuse to run the new file, which is exactly what a checksum is for.

## Step 6 — Validate before it leaves your desk

```bash
usbinstaller --repository /Volumes/USB_INSTALLER --verify
```

`--verify` re-hashes everything and reports missing installers, wrong file
types, unsafe paths, broken dependencies and invalid versions. Expected output:

```
✓ applications.json is valid JSON and matches the schema
✓ 8 application(s) found
✓ 8 application ID(s) are unique
✓ 8 windows installer(s) declared
✓ 6 macos installer(s) declared
✓ every declared installer has a checksum

Validation passed.
```

## Step 7 — Test on a real machine

```bash
usbinstaller --repository /Volumes/USB_INSTALLER --list
usbinstaller --repository /Volumes/USB_INSTALLER --install-all --dry-run
```

The dry run exercises detection, prerequisites, checksums and the full
installation plan on a live computer **without changing anything**. If that
looks right, the drive is ready.

Finally, do one genuine install on a spare or virtual machine. A silent-install
switch that is subtly wrong only shows up when the installer actually runs.

---

## Hardening a drive that leaves the building

In `config/settings.json`:

```json
{
  "strict_checksums": true,
  "require_confirmation": true,
  "continue_on_failure": true,
  "log_retention": 50
}
```

`strict_checksums` refuses to run any installer whose SHA-256 is missing or
does not match — the right setting for a drive you hand to someone else.

Consider a hardware-encrypted drive if it carries licensed software, and keep
`checksums.json` in version control so you can diff what changed.

---

## Keeping the drive current

```bash
repository-manager --repository /Volumes/USB_INSTALLER --status
```

```
USB Repository Manager

Repository:   /Volumes/USB_INSTALLER
Applications: 8 (8 enabled)
Installers:   14 declared (8 windows, 6 macos)
Missing:      0
Orphaned:     1
No checksum:  0
```

*Orphaned* means a file is on the drive that no application declares — usually
an installer you replaced without deleting the old one.

Routine refresh, e.g. Firefox 141 → 145:

```bash
cp ~/Downloads/FirefoxSetup145.exe \
   /Volumes/USB_INSTALLER/installers/windows/firefox/FirefoxSetup.exe
# edit "version": "145.0" in config/applications.json
repository-manager --repository /Volumes/USB_INSTALLER --checksums
usbinstaller       --repository /Volumes/USB_INSTALLER --verify
```

Machines on 141 are then offered an upgrade; machines already on 145 or later
are skipped.

## Cloning a drive

The whole drive is just files, so a plain copy works:

```bash
# macOS
sudo cp -Rp /Volumes/USB_INSTALLER/ /Volumes/USB_INSTALLER_2/
# Windows
robocopy E:\ F:\ /MIR
```

Then validate the copy: `usbinstaller --repository F:\ --verify`. Delete
`logs/` on the clone if you do not want the source drive's history travelling
with it.

---

## Setup problems

| Symptom | Cause | Fix |
| --- | --- | --- |
| "Could not locate the software repository" | The executable is not on the drive, or `config/` is missing | Check the layout, or pass `--repository E:\` |
| Validation says "file not found" | Installer path does not match the catalogue | Compare the real path with `installer` in `applications.json` |
| Validation says "no checksum" | New installer not yet hashed | `repository-manager --checksums` |
| "checksum MISMATCH" on a file you replaced | Checksums are stale | `repository-manager --checksums`, then `--verify` |
| macOS will not open the launcher | Gatekeeper, first run of an unsigned app | Right-click → *Open* → *Open*. Never disable Gatekeeper |
| Copy fails at ~4 GB | Drive is FAT32 | Reformat as exFAT |

More in [TROUBLESHOOTING.md](TROUBLESHOOTING.md).
