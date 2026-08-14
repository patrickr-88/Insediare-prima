# Security model

## Threat model

**The USB drive is treated as untrusted input.** A drive can be lost, swapped,
left in a workshop, or written to by a machine that is already compromised —
and the tool that reads it usually runs as Administrator or root. Every control
below exists because of that combination.

Assets to protect: the target computer's integrity, the user's data, and the
technician's credentials.

Adversaries considered:

1. Someone who can write files onto the drive (add a payload, replace an
   installer, drop a symlink).
2. Someone who can edit `applications.json` (point at a system binary, inject
   arguments, escape the repository).
3. A malicious or simply broken vendor installer.
4. A shoulder-surfer or anyone who later reads the logs.

Explicitly **out of scope**: an attacker who already has Administrator/root on
the target machine, and the trustworthiness of the vendor installers themselves
(checksums prove *unchanged since you recorded them*, not *benign*).

---

## Controls

### Only declared installers execute

Nothing runs because it exists on the drive. The engine executes exactly the
files named in `applications.json`, resolved through the path rules below.
Dropping `evil.exe` into `installers/windows/` achieves nothing — the
repository manager will not even checksum it, and `--status` reports it as an
orphan.

### Path containment

Every configured path is resolved by `security/paths.py`:

- absolute paths, drive letters (`C:foo`) and UNC paths are rejected;
- any `..` segment is rejected;
- symlinks are resolved **before** the containment check, so a symlink on the
  drive cannot point at `C:\Windows\System32` or `/usr/bin`;
- the file extension must match the declared installer type (a `.bat` cannot
  masquerade as an `exe`, a `.dll` cannot be executed as an installer).

Tested in `tests/unit/test_security.py::TestPathContainment`.

### Checksums

`checksums/checksums.json` records the SHA-256 of every declared installer.

- A **mismatch blocks execution** by default (`block_on_checksum_mismatch`).
- A **missing** checksum is a warning by default and an error under
  `strict_checksums: true` — recommended for drives that leave the building.
- Checksums can also be pinned inline per platform (`"sha256": "…"`); if the
  inline value and the store disagree, validation fails rather than picking one.
- `repository-manager --checksums` only hashes files the configuration
  declares, so an attacker cannot get a payload blessed by re-running it.

### No shell, ever

Every subprocess is an argv vector with `shell=False`
(`installers/process.py`). Quoting, `;`, `&&`, `|`, `$(…)` and backticks are
inert — they arrive at the target program as literal characters. There is no
code path in which configuration text becomes a shell command.

Additionally: the executable must be an existing file or a known system tool;
bare names are never resolved through the target machine's `PATH`;
`PYTHONPATH`, `LD_PRELOAD` and `DYLD_INSERT_LIBRARIES` are scrubbed from the
child environment; and every process has a timeout after which it is killed.

### Privileges

- The tool never elevates silently and never caches a password.
- Elevation happens only through the operating system's own consent UI (UAC on
  Windows, the macOS authentication prompt), initiated by a human.
- The plan states which applications need administrator rights and whether
  those rights are currently held; if they are not, the run stops with exit
  code `5` rather than half-installing.

### OS security mechanisms are never weakened

The engine does not, and must not, disable or bypass:

Windows Defender · SmartScreen · UAC · machine-wide PowerShell execution policy
· macOS Gatekeeper · SIP · notarisation checks · code-signature validation.

`-ExecutionPolicy Bypass` is applied to a **single PowerShell process** and
does not change machine policy; scripts are still scanned by AMSI. The one
quarantine-related action is `xattr -d com.apple.quarantine` applied *only* to
the specific bundle just deployed — a deliberate deployment the technician
chose, not a system-wide setting. Both properties are asserted by tests
(`tests/windows/…::test_execution_policy_bypass_is_process_scoped_only`,
`tests/macos/…::test_gatekeeper_and_sip_are_never_disabled`).

### No network

The engine makes no network requests of any kind. It does not download
installers, check for updates, or phone home. Everything comes from the drive,
which is what makes it usable on isolated machines — and removes remote code
execution from the threat model entirely.

### Secrets are not logged

Arguments matching password/token/secret/key/serial patterns — including the
`--password value` two-argument form — are redacted before anything is written
to `installation.log` or `results.json`
(`tests/unit/test_logging.py::TestRedaction`). Installer stdout/stderr is
captured and truncated; if a vendor installer prints a credential, that text
would be logged, so avoid passing credentials on the command line at all.

### Data safety

- The tool never reads, moves, renames or deletes user files.
- It never uninstalls anything.
- It never downgrades: an equal or newer installed version is skipped, and an
  uncomparable version is skipped with an explanation, unless the technician
  passes `--force` (which says exactly what it is doing in the plan).
- Log pruning only ever deletes directories this tool created — matching its
  own timestamp naming *and* containing an `installation.log`.

### Phase separation

Discovery, validation and planning are read-only; installation is the only
phase permitted to modify the machine. `--dry-run` therefore has a provable
meaning, and `tests/integration/test_dry_run_safety.py` proves it three ways:
no process is spawned, a before/after filesystem snapshot is identical, and the
dry-run plan matches the real plan exactly.

---

## Residual risks

| Risk | Mitigation | Residual |
| --- | --- | --- |
| A vendor installer is itself malicious | Checksums pin what you tested | Verify vendor signatures before adding to the drive |
| A checksum is recorded *after* tampering | Prepare drives on a trusted machine | Store `checksums.json` under version control and diff it |
| Credentials in `arguments` | Redacted in logs | Plain text remains on the drive — prefer licence files with restrictive permissions |
| Drive lost with sensitive installers | – | Use hardware-encrypted drives for licensed software |
| A malicious `applications.json` naming a legitimate but dangerous installer already on the drive | Path/type/checksum rules limit *what*, not *why* | Treat the config as code: review changes, keep the drive physically controlled |

---

## Reporting a vulnerability

Open a security advisory on the repository rather than a public issue, and
include the repository configuration (with any credentials removed) plus the
`installation.log` from the affected run.
