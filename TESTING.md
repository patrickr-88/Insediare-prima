# Testing

```bash
python -m pip install -e ".[dev]"

python -m pytest tests -q                         # everything (~5 s)
python -m pytest tests --cov --cov-report=term-missing
python -m pytest tests/unit -q                    # fast unit tests
python -m pytest tests/windows tests/macos -q     # platform suites
```

No test executes a real vendor installer, touches a real registry, or mounts a
real disk image. Both platform suites run on **any** host.

## Layout

| Path | Scope |
| --- | --- |
| `tests/conftest.py` | Fake system probes, synthetic repository builders |
| `tests/unit/` | Config, versioning, dependencies, detection, installers, planner, executor, logging, security, service layer |
| `tests/integration/` | End-to-end technician workflow, CLI, repository manager, dry-run safety |
| `tests/windows/` | Windows-specific behaviour |
| `tests/macos/` | macOS-specific behaviour, Apple Silicon vs Intel |

## How platform behaviour is tested anywhere

Three seams, all injected rather than imported globally:

1. **`SystemProbe`** — every call into `platform`/`os`/`ctypes`. `FakeProbe` in
   `conftest.py` scripts an entire machine (OS, build number, architecture,
   Rosetta translation, admin state, free disk, hostname).
2. **`ProcessRunner`** — every subprocess. `RecordingRunner` returns scripted
   exit codes and records the exact argv of every command that *would* run.
3. **`DetectionContext`** — the registry reader, filesystem probe and plist
   reader are all callables that tests replace.

So `test_windows_behaviour.py` asserts that MSI installs go through
`C:\Windows\System32\msiexec.exe` with `/i` before the package, and
`test_macos_behaviour.py` asserts that disk images are mounted `-readonly
-nobrowse` — on a Linux CI runner, deterministically.

Tests that genuinely need the real OS are marked `@pytest.mark.windows` /
`@pytest.mark.macos` and skipped elsewhere:

```bash
python -m pytest -m windows      # on a real Windows machine
python -m pytest -m macos        # on a real Mac
```

## What is covered

**Configuration** — valid configs, invalid JSON (with position), missing
fields, duplicate ids, bad installer types, missing installers, unsafe paths,
unsupported OS/architecture, unknown and circular dependencies, invalid
versions per scheme, checksum contradictions, unknown settings.

**Detection** — Windows registry (display name, product code, publisher
disambiguation, invalid regex), Windows paths, macOS bundles (XML and binary
plists, user application directories), `pkgutil` receipts, version commands
(including the refusal to resolve bare command names through `PATH`), and the
guarantee that a detector which throws degrades to "unknown" instead of
crashing the run.

**Versions** — older/same/newer/malformed, four schemes plus extraction
patterns, and the rule that anything uncomparable is *unknown*, never *equal*.

**Installers** — every type's command construction; success, non-zero exits,
MSI `3010`, timeouts, missing files, invalid files, cancellation, and DMG
mount/copy/detach including "the image is always detached even when the copy
fails".

**Engine** — dependency ordering (including a 5000-node graph that would blow a
recursive implementation), failure isolation, retry, post-install actions,
verification, cancellation, and log/report generation.

**Security** — path traversal (including symlinks that escape the root),
extension allow-lists, checksum verification and tampering, the no-shell
guarantee (metacharacters arrive as literal argv), environment scrubbing, and
secret redaction.

**Dry run** — proven three ways in
`tests/integration/test_dry_run_safety.py`: no process is spawned, a
before/after filesystem snapshot is byte-identical, and the dry-run plan is
identical to the real plan. A counter-test confirms the same plan *does* change
the machine without `--dry-run`, so the proof is not vacuous.

## Coverage

Target ≥ 90% of the engine; currently ~92% statements and branches. The GUI is
excluded (it needs a display and a human) and is exercised manually — see the
checklist below. CI fails the build under 90%.

```bash
python -m pytest tests --cov --cov-fail-under=90
```

## CI matrix

| Runner | Covers |
| --- | --- |
| `windows-latest` | Windows 11 x64 |
| `macos-13` | macOS Intel |
| `macos-14` | macOS Apple Silicon |
| `ubuntu-latest` | Engine, lint, coverage gate |

GitHub-hosted Apple Silicon runners (`macos-14`) cover the arm64 path,
including a real `platform.machine() == "arm64"`. What CI **cannot** cover is a
genuine Rosetta-translated process (an x86_64 Python on Apple Silicon); that
path is unit-tested through the probe, and verified manually:

```bash
arch -x86_64 /usr/local/bin/python3 -m usbinstaller --info   # must report arm64
```

## Manual GUI checklist

On each platform, against the demo repository:

1. Launch by double-clicking; the header shows the right OS, architecture,
   privilege state and drive.
2. Select/deselect, **All** and **None** update the counter.
3. **Review Installation** shows the plan, including skips and errors.
4. **Cancel** returns to the list and changes nothing.
5. Installing shows live progress and per-application results.
6. A failure is visible, does not stop the run, and offers **Retry**.
7. The final report names the log directory, and that directory exists.

## Adding tests

Build repositories with the `write_repository` / `app_entry` helpers in
`conftest.py`, drive platforms with `windows_probe()` / `macos_probe()`, and
script installer outcomes with `RecordingRunner`. Never call a real installer,
and never assert on the developer's own machine state.
