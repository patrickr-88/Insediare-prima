# Development

## Setup

```bash
git clone <repository-url> && cd Insediare-prima
python -m pip install -e ".[dev]"
python -m pytest tests -q
```

Python 3.11+. No third-party runtime dependencies; `dev` adds pytest, coverage,
ruff and mypy, `build` adds PyInstaller.

## Layout

```
src/usbinstaller/
├── models.py              Typed domain objects. No I/O — import-safe anywhere.
├── errors.py              One exception hierarchy for the whole package.
├── version.py             Version and config schema version.
├── repository.py          Locate + load the USB repository.
├── config/
│   ├── catalogue.py       JSON → Application models (the trust boundary).
│   ├── settings.py        settings.json.
│   ├── validator.py       Collects findings; never raises for data problems.
│   └── authoring.py       The ONLY writer of applications.json.
├── sysdetect/
│   ├── system.py          SystemProbe seam + OS/arch/disk detection.
│   └── privileges.py      Admin state; never elevates silently.
├── detection/             Detector registry + one module per platform.
├── versioning.py          Configurable version comparison.
├── security/              Path containment, SHA-256 store.
├── installers/
│   ├── base.py            Installer ABC + (os, type) registry.
│   ├── process.py         The only place a subprocess is ever created.
│   ├── windows.py         exe, msi, msix, powershell.
│   └── macos.py           dmg, pkg, app, shell.
├── engine/
│   ├── dependencies.py    Iterative topological sort + cycle reporting.
│   ├── details.py         Per-application facts for the UI (read-only).
│   ├── planner.py         Read-only: decides what would happen.
│   └── executor.py        The only component that modifies the machine.
├── logging_session.py     Per-run log directory, redaction, retention.
├── app.py                 Service layer shared by CLI and GUI.
├── cli.py                 Command-line interface.
├── ui/gui.py              Tkinter view only — no logic worth testing lives here.
└── repository_manager.py  Drive maintenance.
```

## Design rules

**The engine never knows which applications exist.** Anything that would need a
code change when the software list changes belongs in configuration instead.

**Phases are separated and ordered.** Discovery → Validation → Planning are
read-only; Installation is the only writing phase; Verification is read-only
again. This is what makes `--dry-run` provable rather than promised.

**Dependencies are injected, not imported.** `SystemProbe`, `ProcessRunner` and
`DetectionContext` are constructor arguments, so both platforms' behaviour is
testable on any host and there is no global state to reset between tests.

**Failures are values, not crashes.** The validator returns findings; detectors
that throw degrade to "unknown"; a failed installation is recorded and the run
continues. Exceptions are for programmer errors and genuinely unusable input.

**One subprocess implementation.** Everything goes through `ProcessRunner`:
`shell=False`, argv vectors, validated executables, timeouts, captured and
truncated output, scrubbed environment. Never call `subprocess` directly.

**One catalogue writer.** Everything that changes `applications.json` goes
through `config/authoring.py`, which re-parses the edited document before
writing it, writes atomically with a `.bak`, and rolls back copied files if any
step fails. The GUI holds no such logic — if a behaviour cannot be tested
headlessly, it is in the wrong module.

## Extending

### A new installer type

```python
# src/usbinstaller/installers/windows.py
@register
class WindowsBatchInstaller(Installer):
    type = "cmd"
    os = OS.WINDOWS

    def build_command(self, installer_path, payload, context):
        return [ntpath.join(context.extra["system_root"], "System32", "cmd.exe"),
                "/c", str(installer_path), *payload.arguments]
```

Then add `"cmd"` to `INSTALLER_TYPES[OS.WINDOWS]` in `config/catalogue.py` and
its extensions to `ALLOWED_EXTENSIONS` in `security/paths.py`. Nothing else
changes — the planner, executor, validator and both front-ends pick it up.

Override `install()` instead of `build_command()` when the format needs several
steps (see `MacDmgInstaller`: mount → copy → always detach).

### A new detection method

```python
@register
class MyDetector:
    method = "my_method"

    def detect(self, app, spec, context) -> InstalledApp:
        ...
```

Add the name to `DETECTION_METHODS` in `config/catalogue.py`. Detectors must be
read-only and must never raise for an ordinary "not found".

### A new version scheme

Add it to `SCHEMES` in `versioning.py` and handle it in `compare()` and
`is_valid()`. Return `Comparison.UNKNOWN` rather than guessing.

## Quality gates

```bash
ruff check src tests tools
mypy
python -m pytest tests --cov --cov-fail-under=90
```

Conventions: full type hints; dataclasses for data; docstrings that explain
*why*, not *what*; no module over ~400 lines; no global mutable state.

## Building releases

```bash
python -m pip install -e ".[build]"

python tools/build.py                 # host platform, one-file binaries
python tools/build.py --onedir        # faster start-up
python tools/build.py --universal2    # macOS arm64 + x86_64
```

Produces in `dist/`:

```
USBInstaller(.exe)              GUI + CLI
repository-manager(.exe)        drive maintenance
USBInstaller-1.0.0-Windows-x64.zip
USBInstaller-1.0.0-macOS-universal.zip
```

The target computer needs no Python: the binary bundles CPython and this
package, and there are no third-party runtime dependencies to vendor.

A macOS universal build must be produced on macOS with a universal2 CPython
(the official python.org installer provides one); the Homebrew build is
single-architecture and PyInstaller will say so. Signing and notarising for
distribution outside your organisation is a separate, documented Apple process
and is deliberately not automated here.

### Release process

1. Bump `__version__` in `src/usbinstaller/version.py` and `version` in
   `pyproject.toml`.
2. `python -m pytest tests -q` — green on Windows and macOS.
3. Tag: `git tag v1.0.1 && git push --tags`.
4. `.github/workflows/release.yml` builds both platforms, runs the tests again,
   publishes the zips and a `SHA256SUMS.txt`.
5. Copy the new binaries into `bin/` on the drives, then re-validate each drive:
   `usbinstaller --repository <drive> --verify`.

Version compatibility: `CONFIG_SCHEMA_VERSION` in `version.py` gates
`applications.json`. A drive written for a newer schema is refused with a clear
message rather than partially understood; older schemas keep working.

## Demo repository

```bash
python tools/make_demo_repository.py build/demo-usb --force
```

Mock installers write marker files into the demo directory only. One
application fails on purpose, so failure handling and `--retry` can be
exercised without breaking anything.
