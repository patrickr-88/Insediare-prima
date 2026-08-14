# Troubleshooting

Start here every time:

```bash
usbinstaller --info                 # what the tool thinks this machine is
usbinstaller --validate-config      # is the drive sound?
usbinstaller --verify               # …and are the installers unmodified?
usbinstaller --install-all --dry-run
```

Then read the last run's log: `<drive>/logs/<timestamp>/installation.log` and
`results.json`. Every failure records the application, installer, exit code,
error text, timestamps, duration, OS and architecture.

---

## The application will not start

**"Could not locate the software repository"** — the executable is not on the
drive, or the drive layout is wrong. Confirm `config/applications.json` exists,
or point at it explicitly:

```bash
usbinstaller --repository E:\ --list
export USB_INSTALLER_ROOT=/Volumes/USB_INSTALLER   # or set on Windows
```

**macOS refuses to open the launcher** — right-click → *Open* → *Open*, or
approve it in *System Settings → Privacy & Security*. Never disable Gatekeeper.

**The GUI does not appear** — Tkinter is missing from that Python build (common
on Linux, rare on Windows/macOS). Use the CLI; every GUI feature has a
command-line equivalent.

---

## Nothing is listed

`--list` shows only software compatible with **this** machine. Check with
`usbinstaller --info` that the OS and architecture are what you expect, then:

- the application may have no section for this OS (by design);
- its `architectures` may exclude this machine (e.g. `x64` only on an
  Apple Silicon Mac);
- it may be `"enabled": false`;
- `--validate-config` may be reporting errors that hide it.

---

## An application is skipped

The plan always says why:

| Reason | Meaning | What to do |
| --- | --- | --- |
| `already installed (141.0)` | Same version present | Nothing; `--force` to reinstall |
| `newer version already installed` | Machine has a newer build | Nothing — downgrades are refused by design |
| `version cannot be compared` | Detection found it but the versions are not comparable | Set a `version_scheme`/`version_pattern`, or use `--force` |
| `no macos installer defined` | Windows-only application | Expected |
| `not supported on x64` | Architecture mismatch | Add the right build |
| `disabled in configuration` | `"enabled": false` | Re-enable it |

---

## An application is in error before anything runs

| Reason | Cause | Fix |
| --- | --- | --- |
| `installer missing` | The file is not at the configured path | Copy it, or fix `installer` |
| `checksum MISMATCH` | The file changed since checksums were recorded | Re-download it, then `repository-manager --checksums` |
| `no SHA-256 checksum recorded and strict checksum mode is enabled` | New installer, no checksum | `repository-manager --checksums` |
| `not a valid file type for installer type` | e.g. `.msi` declared as `exe` | Correct `type` |
| `unsafe path` | `..`, absolute path or drive letter in `installer` | Use a repository-relative path |
| `administrator privileges are required but not held` | Prerequisite unmet | Re-launch elevated |
| `requires 1024 MB free` | Not enough disk | Free space |

---

## An installation fails

Look up the exit code in `results.json`.

### Windows

| Code | Meaning | Action |
| --- | --- | --- |
| `1602` | User cancelled | A dialog appeared — the silent switch is probably wrong |
| `1603` | Fatal error during installation | Run the installer by hand; often "already installed" or a pending reboot |
| `1618` | Another installation is in progress | Wait for Windows Update to finish, then `--retry` |
| `1619` / `1620` | Package could not be opened | Corrupt download — re-copy and re-checksum |
| `1638` | Another version is already installed | Remove it first, or install the matching version |
| `3010` | Success, reboot required | Not a failure; declare `"success_exit_codes": [0, 3010]` if you see it flagged |
| `-1` | Timed out | Raise `timeout_seconds`, or the installer is waiting on a dialog |

### macOS

| Symptom | Cause | Action |
| --- | --- | --- |
| `failed to mount disk image` | Corrupt DMG, or it needs a licence agreement accepted | Re-download; EULA-gated images are reported rather than auto-accepted |
| `disk image contains neither an .app bundle nor a .pkg` | Unusual payload name | Set `app_bundle` |
| `installer` exits `1` | Package refused the target | Try `sudo installer -pkg <file> -target /` by hand |
| `destination directory does not exist` | Custom `destination` is wrong | Fix or remove it |

### "The installer reported success but the application was not detected afterwards"

The installer exited `0` without installing — a genuinely common vendor bug,
and exactly what post-install verification exists to catch. Either the
detection rule is wrong (test it with `--dry-run` on a machine that *does* have
the software), or the installer silently failed (run it by hand and watch).

---

## Everything fails immediately

- Wrong privileges: `--info` shows `Administrator: No`.
- Antivirus is quarantining files from removable media.
- The drive is failing: `usbinstaller --verify` will show mismatches on files
  nobody edited. Copy the repository to a local disk and run from there.

---

## Logs cannot be written

The run continues and warns: logs go to the system temp directory instead.
Causes: a write-protected drive, a read-only mount, or no permission on the
`logs/` directory. Copy the log directory off before unplugging.

---

## Getting help

Attach to the ticket: `installation.log`, `results.json`, `system.json` from
the failing run, plus the output of `usbinstaller --validate-config`. Those
four files describe the machine, the drive and the failure completely — none of
them contain credentials.
