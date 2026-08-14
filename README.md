# USB Software Installer

A portable software deployment platform that runs from a USB flash drive and
installs a curated set of applications on **Windows 10/11** and **modern macOS**
(Apple Silicon and Intel), fully offline.

The central design rule: **the engine and the software repository are
separate.** Swapping Firefox 140 for Firefox 145, or adding a new application,
is a data change on the drive — the application's source code never changes.

The graphical interface launches straight from the drive: it reports what the
computer is, what each installer will actually do, and what is already
installed — and it can add new software to the drive without hand-editing JSON.
The command line does the same work headlessly:

```
usbinstaller --list                     # what can be installed here
usbinstaller --install-all --dry-run    # full plan, zero changes
usbinstaller --install firefox vscode   # install a selection
usbinstaller --retry                    # re-run what failed last time
```

---

## Contents

| Document | What it covers |
| --- | --- |
| **[USB_SETUP.md](USB_SETUP.md)** | **Building a deployment drive, step by step** |
| **[USER_GUIDE.md](USER_GUIDE.md)** | **Using the drive and the GUI, for technicians** |
| [INSTALLATION.md](INSTALLATION.md) | Installing and running the tool |
| [CONFIGURATION.md](CONFIGURATION.md) | Full configuration schema reference |
| [ADDING_SOFTWARE.md](ADDING_SOFTWARE.md) | Recipes for adding/replacing software |
| [SECURITY.md](SECURITY.md) | Threat model and the controls that enforce it |
| [TESTING.md](TESTING.md) | How to run and extend the test suite |
| [TROUBLESHOOTING.md](TROUBLESHOOTING.md) | Diagnosing failed installations |
| [DEVELOPMENT.md](DEVELOPMENT.md) | Architecture, building, releasing |

---

## Technology choice

**Python 3.11+, standard library only, frozen with PyInstaller.**

The requirements are unusual in one specific way: this program's real job is to
*launch other programs correctly* and *interrogate two very different operating
systems*. That makes process control, path handling and platform introspection
the hot path — not raw performance or UI polish.

| Criterion | Why Python wins here |
| --- | --- |
| Cross-platform | One codebase; `subprocess`, `pathlib`, `plistlib`, `winreg` are all stdlib |
| Portable from USB | PyInstaller produces a self-contained binary; **no runtime needed on the target machine** |
| Minimal dependencies | Zero third-party runtime packages — nothing to audit or vendor |
| Process execution | `subprocess` gives exact control over argv, timeouts and captured output |
| Windows support | `winreg` reads the uninstall registry directly; `ctypes` reads UAC state and PE version resources |
| macOS support | `plistlib` reads app bundles; `hdiutil`/`installer`/`ditto` are driven directly |
| Testability | The platform seams are ordinary objects, so Windows *and* macOS behaviour is tested on any host |
| Maintenance | An IT team can read and modify it without a toolchain |

Alternatives considered:

- **Go / Rust** — smaller, faster binaries and genuinely attractive here, but
  both make the "mock the platform and test both OSes anywhere" story harder,
  and neither has a stdlib GUI. Rust additionally raises the maintenance bar
  for the IT staff most likely to own this tool.
- **.NET** — excellent on Windows, and self-contained publishing solves the
  runtime problem; the macOS half (DMG mounting, bundle handling) is where it
  stops being idiomatic.
- **Electron / Tauri** — a browser engine to install 7-Zip is the wrong shape,
  and Electron's ~150 MB payload is a poor fit for a deployment drive.

Cost accepted: a frozen Python binary is ~10–15 MB per platform (Go/Rust would
be ~3–5 MB). On a USB drive holding gigabytes of installers, that is noise.

The GUI uses **Tkinter**, which ships with the official Python builds on both
platforms, so the graphical interface adds no dependency at all.

---

## Architecture

Five phases, strictly ordered. Everything before *Installation* is read-only,
which is what makes `--dry-run` a provable property rather than a promise
(see `tests/integration/test_dry_run_safety.py`).

```
Discovery  →  Validation  →  Planning  →  Installation  →  Verification
(read-only)  (read-only)   (read-only)   (the only         (read-only)
                                          writing phase)
```

```
usbinstaller/
├── models.py            Typed domain objects (no I/O — trivially testable)
├── repository.py        Locating and loading the USB repository
├── config/              JSON → typed models, settings, exhaustive validator
├── sysdetect/           OS, architecture, privileges, disk (behind SystemProbe)
├── detection/           "Is it already installed, and at what version?"
├── versioning.py        Configurable version comparison (semver/numeric/date/…)
├── security/            Path containment + SHA-256 checksums
├── installers/          Installer abstraction + one class per package format
├── engine/              Dependency resolution, planning, execution
├── logging_session.py   Per-run structured logs with secret redaction
├── config/authoring.py  The only writer of applications.json (GUI edits)
├── engine/details.py    Per-application facts for the information panel
├── app.py               Service layer shared by both front-ends
├── cli.py               Command-line interface
├── ui/gui.py            Tkinter interface (information, install, extend)
└── repository_manager.py  Drive maintenance (never touches the target machine)
```

Two registries make the system extensible without touching existing code:

- **Installer registry** — `@register` a class with a `type` and an `os`, and
  that package format becomes configurable.
- **Detector registry** — same pattern for "how do I tell whether this is
  already installed".

### USB drive layout

```
USB_INSTALLER/
├── Launch-Windows.cmd          double-click launcher (Windows)
├── Launch-macOS.command        double-click launcher (macOS)
├── bin/                        the frozen executables
├── config/
│   ├── applications.json       the catalogue — the only file you edit
│   └── settings.json           policy: checksums, timeouts, retention
├── installers/
│   ├── windows/<app>/…
│   └── macos/<app>/…
├── scripts/                    optional post-install scripts
├── checksums/checksums.json    SHA-256 of every declared installer
├── logs/<timestamp>/           installation.log, results.json, system.json
└── documentation/              notes for the technicians using this drive
```

---

## Try it in two minutes

The demo repository ships harmless mock installers that only write marker files
into their own directory, so the whole workflow can be exercised safely:

```bash
python -m pip install -e ".[dev]"
python tools/make_demo_repository.py build/demo-usb

usbinstaller --repository build/demo-usb --list
usbinstaller --repository build/demo-usb --install-all --dry-run
usbinstaller --repository build/demo-usb --install-all --yes
usbinstaller --repository build/demo-usb --retry          # one app fails on purpose
```

---

## Command reference

| Command | Effect |
| --- | --- |
| `--list` | Software compatible with this machine |
| `--install-all` | Install everything compatible |
| `--install ID…` | Install named applications (dependencies included) |
| `--skip ID…` | Exclude applications from the run |
| `--retry` | Re-run the applications that failed in the last run |
| `--plan` | Show the installation plan and stop |
| `--dry-run` | Validate, detect and plan; execute nothing |
| `--force` | Install even when an equal/newer version is present |
| `--validate-config` | Validate the repository |
| `--verify` | Validate *and* re-hash every installer |
| `--generate-checksums` | Write/refresh `checksums.json` |
| `--info` | Detected OS, architecture, privileges, disk, drive |
| `--gui` | Launch the graphical interface |
| `--json` | Machine-readable output (for automation) |
| `--yes` | Skip the confirmation prompt |

Exit codes: `0` success · `1` an installation failed · `2` usage/repository
error · `3` validation failed · `4` cancelled · `5` administrator privileges
required but not held.

Drive maintenance is a separate tool that **never touches the target machine**:

```bash
repository-manager --status            # inventory and drift
repository-manager --validate
repository-manager --checksums --prune
repository-manager --verify-checksums
```

---

## Safety guarantees

- User files are never read, moved or deleted; only vendor installers run.
- Nothing is uninstalled, and an equal or newer installed version is never
  downgraded without an explicit `--force`.
- Only installers named in `applications.json` can execute — a file's mere
  presence on the drive grants it nothing.
- Configured paths cannot escape the repository root (symlinks included).
- Installers are checksum-verified; a mismatch blocks execution by default.
- No shell is ever invoked, so configuration cannot inject commands.
- Windows Defender, Gatekeeper and SIP are never disabled or bypassed.
- Administrator privileges are requested only through the operating system's
  own consent UI, and the plan always says which applications need them.

See [SECURITY.md](SECURITY.md) for the full model.

---

## Status

560+ automated tests, 92% statement/branch coverage of the engine, CI on
Windows, macOS Intel and macOS Apple Silicon. See [TESTING.md](TESTING.md).

Licensed under the MIT licence.
