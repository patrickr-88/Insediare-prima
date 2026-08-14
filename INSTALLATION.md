# Installation and use

## A. Preparing a USB drive (done once, by whoever owns the drive)

### 1. Format the drive

| Filesystem | Windows | macOS | Notes |
| --- | --- | --- | --- |
| **exFAT** | ✅ | ✅ | **Recommended.** Cross-platform, no 4 GB file limit. |
| FAT32 | ✅ | ✅ | 4 GB per-file limit — too small for some installers. |
| NTFS | ✅ | read-only | Fine for Windows-only drives. |
| APFS/HFS+ | ✗ | ✅ | macOS-only drives. |

exFAT does not carry the POSIX execute bit. That is deliberate in the design:
macOS shell installers are run as `/bin/sh <script>`, so a missing execute bit
is never a failure.

### 2. Create the layout

```
USB_INSTALLER/
├── Launch-Windows.cmd
├── Launch-macOS.command
├── bin/                      USBInstaller(.exe/.app), repository-manager
├── config/                   applications.json, settings.json
├── installers/windows/…      vendor installers
├── installers/macos/…
├── scripts/                  optional post-install scripts
├── checksums/                checksums.json
├── logs/                     created automatically
└── documentation/            notes specific to this drive
```

Start from the `repository/` directory in this project — it has the layout, a
settings file and a catalogue covering eight common applications.

```bash
cp -r repository/ /Volumes/USB_INSTALLER/
```

### 3. Add the executables

Copy the release builds into `bin/`:

```
bin/USBInstaller.exe          bin/USBInstaller.app        (or bin/USBInstaller)
bin/repository-manager.exe    bin/repository-manager
```

Both platforms' builds can share one drive. Build them yourself with
`python tools/build.py` (see DEVELOPMENT.md) or take them from a release.

### 4. Add the installers

Download each vendor installer and save it at the path named in
`config/applications.json`, e.g.
`installers/windows/firefox/FirefoxSetup.exe`. Vendor installers are not
redistributed with this project.

### 5. Record checksums and validate

```bash
repository-manager --repository /Volumes/USB_INSTALLER --checksums
usbinstaller       --repository /Volumes/USB_INSTALLER --verify
usbinstaller       --repository /Volumes/USB_INSTALLER --install-all --dry-run
```

The dry run exercises the whole pipeline on a real machine without changing it.

---

## B. Using the drive (the technician's workflow)

### Windows

1. Plug the drive in.
2. Double-click **Launch-Windows.cmd**. Approve the UAC prompt — that dialog is
   the only privilege escalation the tool performs, and you always see it.
3. Review the software list; tick what you need.
4. Click **Review Installation** and read the plan: what will be installed,
   what is skipped and why, what is in error.
5. Confirm. Failures do not stop the run.
6. Read the final report; the log path is printed at the end.

### macOS

1. Plug the drive in.
2. Double-click **Launch-macOS.command**.
   - On first use macOS may refuse to open a file from an unidentified
     developer. Right-click → **Open** → **Open**, or approve it in
     *System Settings → Privacy & Security*. Do **not** disable Gatekeeper.
3. Same flow as Windows. Installers that need administrator rights use the
   standard macOS authentication prompt.

### From a terminal

```bash
usbinstaller --list
usbinstaller --install-all --dry-run
usbinstaller --install firefox vlc 7zip --yes
usbinstaller --install-all --skip chrome --yes
usbinstaller --retry
usbinstaller --info
```

The repository is found automatically: `$USB_INSTALLER_ROOT` if set, otherwise
by walking up from the executable, then from the working directory. Override it
with `--repository /path/to/drive`.

### For automation

```bash
usbinstaller --install-all --yes --json > result.json
echo $?     # 0 ok · 1 an install failed · 2 usage · 3 invalid config
            # 4 cancelled · 5 needs administrator
```

---

## C. Running from source (developers, or a machine that already has Python)

```bash
git clone <repository-url> && cd Insediare-prima
python -m pip install -e ".[dev]"

python tools/make_demo_repository.py build/demo-usb
usbinstaller --repository build/demo-usb --install-all --dry-run
```

Python 3.11 or newer. No third-party runtime dependencies.

---

## D. Where things end up

| What | Where |
| --- | --- |
| Installed software | Wherever the vendor installer puts it (usually `C:\Program Files\…` or `/Applications`) |
| Logs | `<drive>/logs/<YYYY-MM-DD_HH-MM-SS>/` |
| Logs when the drive is read-only | The system temp directory; the run warns and continues |

Each log directory holds `installation.log` (narrative), `results.json`
(machine-readable outcomes), `plan.json` and `system.json` (the machine it ran
on), so a failure report is self-contained.
