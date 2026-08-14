# Adding and replacing software

Two ways to do this, both of which are data changes — neither **ever** requires
a code change:

- **In the GUI** — *Drive → Add Application…* picks the installer, copies it
  onto the drive, writes the catalogue entry and records the checksum. See
  [USER_GUIDE.md](USER_GUIDE.md#adding-software-to-the-drive). Best for one-off
  additions at a workbench.
- **By hand**, as described below. Best for bulk edits, scripted drive builds
  and anything you want to review in version control.

By hand, it is exactly two steps:

1. Copy the installer onto the drive.
2. Add or edit an entry in `config/applications.json`.

Then record checksums and validate:

```bash
repository-manager --repository /Volumes/USB --checksums
usbinstaller       --repository /Volumes/USB --validate-config
```

Throughout: `installer` paths are **relative to the repository root** and use
forward slashes. Absolute paths, `..` and drive letters are rejected.

---

## 1. Adding a Windows application

Example: 7-Zip.

```
installers/windows/7zip/7z2501-x64.exe
```

```json
{
  "id": "7zip",
  "name": "7-Zip",
  "version": "25.01",
  "category": "Utilities",
  "version_scheme": "numeric",
  "detection": { "method": "windows_registry", "display_name": "^7-Zip" },
  "windows": {
    "installer": "installers/windows/7zip/7z2501-x64.exe",
    "type": "exe",
    "arguments": ["/S"],
    "architectures": ["x64"],
    "requires_admin": true
  }
}
```

Finding the silent switch is the only research step. Common ones:

| Installer technology | Silent switches | How to recognise it |
| --- | --- | --- |
| NSIS | `/S` | 7-Zip, Notepad++, VLC |
| Inno Setup | `/VERYSILENT /NORESTART` | VS Code, many indie tools |
| InstallShield | `/s /v"/qn"` | Older enterprise software |
| MSI | `/qn /norestart` (via `msiexec`) | `.msi` files |
| Squirrel | `--silent` | Electron apps |

Verify the switch by hand once: `7z2501-x64.exe /S` should install with no
dialog. If it opens a window, the switch is wrong — the tool will faithfully
wait for a human that is not there, until the timeout.

---

## 2. Adding a macOS application

```
installers/macos/firefox/Firefox.dmg
```

```json
{
  "id": "firefox",
  "name": "Mozilla Firefox",
  "version": "141.0",
  "category": "Browsers",
  "version_scheme": "numeric",
  "macos": {
    "installer": "installers/macos/firefox/Firefox.dmg",
    "type": "dmg",
    "app_bundle": "Firefox.app",
    "requires_admin": true,
    "detection": { "method": "macos_app_bundle", "bundle": "Firefox.app" }
  }
}
```

Pick the type by what the vendor ships:

| Vendor ships | `type` | Extra fields |
| --- | --- | --- |
| `.dmg` containing an app | `dmg` | `app_bundle` (needed only if several apps are inside) |
| `.dmg` containing a `.pkg` | `dmg` | none — the package inside is found and run |
| `.pkg` | `pkg` | `target` if not `/` |
| `.zip` of an app bundle | `app` | `app_bundle` |
| `.app` copied onto the drive | `app` | – |
| Vendor shell script | `shell` | `arguments` |

Bundles are copied with `ditto`, which preserves code signatures — a bundle
copied with plain `cp` can fail Gatekeeper validation on first launch.

---

## 3. Replacing an application with a new version

Say Firefox 141 → 145:

```bash
cd /Volumes/USB
rm  installers/windows/firefox/FirefoxSetup.exe
cp ~/Downloads/FirefoxSetup145.exe installers/windows/firefox/FirefoxSetup.exe
```

Then edit two values:

```json
"version": "145.0",
"windows": { "installer": "installers/windows/firefox/FirefoxSetup.exe" }
```

Keeping the *filename* stable means only `version` changes. If you prefer
versioned filenames, update `installer` too.

Finally — and this is the step people forget — refresh the checksums, or the
drive will refuse to run the new file:

```bash
repository-manager --repository /Volumes/USB --checksums
usbinstaller       --repository /Volumes/USB --verify
```

Machines with 141 installed will now be offered an upgrade; machines already on
145 or later are skipped.

---

## 4. Windows-only application

Simply omit the `macos` section:

```json
{
  "id": "notepadplusplus",
  "name": "Notepad++",
  "version": "8.6.9",
  "category": "Utilities",
  "windows": {
    "installer": "installers/windows/notepadplusplus/npp.8.6.9.Installer.x64.exe",
    "type": "exe",
    "arguments": ["/S"]
  }
}
```

It will not appear at all on a Mac — not greyed out, not listed.

## 5. macOS-only application

Omit the `windows` section:

```json
{
  "id": "rectangle",
  "name": "Rectangle",
  "version": "0.79",
  "category": "Utilities",
  "macos": {
    "installer": "installers/macos/rectangle/Rectangle.dmg",
    "type": "dmg",
    "app_bundle": "Rectangle.app",
    "detection": { "method": "macos_app_bundle", "bundle": "Rectangle.app" }
  }
}
```

## 6. Apple Silicon only (or Intel only)

```json
"macos": {
  "installer": "installers/macos/tool/Tool-arm64.dmg",
  "type": "dmg",
  "architectures": ["arm64"]
}
```

Two separate entries can carry the two builds; each machine sees only the one
it can run. A universal build declares both: `["x64", "arm64"]`.

---

## 7. An application needing command-line arguments

Arguments are a **list**, passed to the OS as an argv vector. No shell is
involved, so quoting and metacharacters are inert:

```json
"windows": {
  "installer": "installers/windows/vscode/VSCodeSetup-x64.exe",
  "type": "exe",
  "arguments": [
    "/VERYSILENT",
    "/NORESTART",
    "/MERGETASKS=!runcode,addcontextmenufiles,addtopath"
  ]
}
```

A single string is also accepted and split with POSIX rules — convenient when
copying a command out of vendor documentation:

```json
"arguments": "/sAll /rs /msi EULA_ACCEPT=YES"
```

**Never put a password or licence key in `arguments` if you can avoid it.** If
you must, the logger redacts `--password=…`, `--token=…`, `KEY=…` style
arguments before writing them — but the value still sits in a plain-text file
on the drive.

MSI packages get their arguments after `msiexec /i <package>`:

```json
"windows": {
  "installer": "installers/windows/zoom/ZoomInstallerFull.msi",
  "type": "msi",
  "arguments": ["/qn", "/norestart", "ZoomAutoUpdate=true"]
}
```

## 8. An application requiring administrator privileges

```json
"windows": { "installer": "…", "type": "exe", "requires_admin": true }
```

This is the default for Windows installers and macOS `.pkg` files. The effect:

- the application is marked `[admin]` in the list and the plan;
- before installing, the tool checks whether privileges are actually held;
- if they are not, the run stops with exit code `5` and tells the technician
  exactly how to elevate (right-click → *Run as administrator*, or `sudo`).

The tool never elevates silently. Escalation always goes through the operating
system's own consent UI, initiated by a human.

To require privileges *and* fail the individual application rather than the
whole run, use a prerequisite instead:

```json
"prerequisites": [{ "type": "admin", "message": "Ask IT to run this as admin." }]
```

---

## 9. An application with dependencies

```json
{ "id": "line-of-business-app", "dependencies": ["dotnet-runtime", "vcredist"] }
```

Both dependencies are installed first, in dependency order, even if the
technician only ticked the main application. If a dependency is already present
at a sufficient version, it is skipped as usual.

## 10. Removing an application

Either delete the entry from `applications.json` (and the installer files), or
keep the entry and set `"enabled": false` — useful when a title is temporarily
withdrawn. Then:

```bash
repository-manager --repository /Volumes/USB --checksums --prune
repository-manager --repository /Volumes/USB --status
```

`--status` lists files still on the drive that no application declares, so
stale installers do not accumulate.

---

## Checklist before handing a drive to a technician

```bash
repository-manager --repository /Volumes/USB --checksums
usbinstaller       --repository /Volumes/USB --verify        # re-hashes everything
usbinstaller       --repository /Volumes/USB --list
usbinstaller       --repository /Volumes/USB --install-all --dry-run
repository-manager --repository /Volumes/USB --status
```

The dry run is the real test: it exercises detection, prerequisites, checksums
and the installation plan on a live machine while changing nothing.
