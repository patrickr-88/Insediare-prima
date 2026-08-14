# Using the USB installer

For the technician sitting in front of the machine being set up. Setting a
drive up in the first place is [USB_SETUP.md](USB_SETUP.md).

---

## Quick version

1. Plug in the drive.
2. **Windows:** double-click `Launch-Windows.cmd`, approve the UAC prompt.
   **macOS:** double-click `Launch-macOS.command`.
3. Tick the software you want.
4. **Review Installation** — read the plan.
5. **Install**, then read the report.

Nothing is installed until you confirm the plan. Your files are never touched,
nothing is uninstalled, and software already at a newer version is left alone.

---

## Starting it

### Windows

Double-click **`Launch-Windows.cmd`**. Windows shows the standard UAC prompt —
that dialog is the only privilege escalation this tool ever performs, and you
always see it. Approve it, or most installers cannot run.

If SmartScreen warns about an unrecognised app, choose *More info* → *Run
anyway*, or ask whoever prepared the drive to sign the executable.

### macOS

Double-click **`Launch-macOS.command`**.

On the first run macOS may refuse to open software from an unidentified
developer. Right-click the file → **Open** → **Open**, or allow it in *System
Settings → Privacy & Security*. **Do not disable Gatekeeper.**

Installers that need administrator rights show the normal macOS authentication
prompt.

### Without the launchers

```bash
bin/USBInstaller --repository .          # GUI
bin/USBInstaller --repository . --list   # command line
```

---

## The main window

```
WORKSHOP STANDARD BUILD
Windows 11 Pro  ·  x64  ·  RECEPTION-PC  ·  Administrator: yes
Drive: E:\
────────────────────────────────────────────────────────────────────
Available Software                     │ Details
                                       │
Browsers                               │ Mozilla Firefox  141.0
☑ Mozilla Firefox  141.0  🔒           │ Update available (140.0 → 141.0)
  Update available (140.0 → 141.0)     │
☐ Google Chrome  127.0  🔒             │ Web browser
  Up to date (127.0)                   │
                                       │ Category        Browsers
Utilities                              │ On this drive   141.0
☑ 7-Zip  25.01  🔒                     │ Installed       140.0
  Not installed                        │ Location        C:\Program Files\…
                                       │ Installer       installers/windows/…
────────────────────────────────────── │ Type            EXE
2 of 3 selected                        │ Size            62.4 MB
       [All] [None] [Add Application…] │ Arguments       /S
              [Review Installation]    │ Administrator   required
                                       │ Checksum        verified
```

**The header** tells you what the tool detected: operating system, CPU
architecture, computer name, and whether administrator rights are actually
held. If they are not, a warning appears — fix that before installing, or
anything needing elevation will be blocked.

**The list** shows only software that can run on *this* computer. A macOS-only
application simply is not there on Windows; an Apple Silicon build does not
appear on an Intel Mac. `🔒` means administrator rights are required.

**The line under each name** is live status, filled in as the tool checks the
machine:

| Status | Meaning |
| --- | --- |
| Not installed | It will be installed |
| Up to date (141.0) | Same version already present — it will be skipped |
| Update available (140.0 → 141.0) | The drive is newer; it will be upgraded |
| Newer version installed (142.0) | The machine is ahead — never downgraded |
| Installed; versions not comparable | Present, but the versions cannot be compared. Skipped unless forced |
| Installer missing from the drive | The drive is incomplete — tell whoever prepared it |
| Checksum mismatch | The file changed since it was recorded. **It will not run** |

**The Details panel** (click any application) shows exactly what will happen:
which file runs, with which arguments, how big it is, whether it needs
administrator rights, how the tool detects it, whether its checksum is
verified, and what is installed right now — including where.

---

## Installing

1. Tick what you want. **All** / **None** select everything or nothing.
2. Click **Review Installation**.

```
INSTALLATION PLAN

Computer:      RECEPTION-PC
OS:            Windows 11 Pro (10.0.22631)
Architecture:  x64
Administrator: Yes
Free disk:     214.6 GB
Drive:         E:\

Applications:

[INSTALL] Mozilla Firefox 141.0 - not installed
[INSTALL] VLC media player 3.0.21 - not installed
[SKIP]    7-Zip 25.01 - already installed (25.01)
[SKIP]    Google Chrome - newer version already installed (128.0 > 127.0)
[ERROR]   Adobe Acrobat Reader - installer missing

2 to install, 0 to upgrade, 2 to skip, 1 in error
Administrator privileges are required: YES (held)
```

Read it. This is the last point at which nothing has changed. **Cancel** goes
back to the list; **Install** starts.

3. Watch progress. Each application reports as it finishes.

```
Installing 2 of 3
Mozilla Firefox
████████░░░░░░░░░░░░

Mozilla Firefox: SUCCESS
VLC media player: SUCCESS
Adobe Acrobat Reader: FAILED — installer exited with code 1603
```

**A failure never stops the run.** The remaining applications still install.

4. Read the report.

```
INSTALLATION COMPLETE

Mozilla Firefox ....... SUCCESS
VLC media player ...... SUCCESS
7-Zip ................. SKIPPED  (already installed (25.01))
Adobe Acrobat Reader .. FAILED   (installer exited with code 1603)

Successful: 2
Skipped:    1
Failed:     1

Installation log: E:\logs\2026-08-14_13-45-22
```

If anything failed, a **Retry** button re-runs just those applications.

After each installation the tool re-checks the machine to confirm the software
really arrived. An installer that exits "successfully" without installing
anything is reported as a failure — that is deliberate, and it catches a
genuinely common vendor bug.

---

## Adding software to the drive

**Drive → Add Application…**, or the **Add Application…** button.

This changes **the USB drive**, not the computer in front of you. Nothing is
installed by adding.

1. **Choose Windows installer…** / **Choose macOS installer…** and pick the
   file you downloaded. The tool works out the installer type from the
   extension and warns if the file looks like the other platform's.
2. Fill in the details:

| Field | Notes |
| --- | --- |
| Display name | What technicians see, e.g. `7-Zip` |
| ID | Auto-filled from the name (`7-zip`); used on the command line |
| Version | The version **on the drive**. Needed for upgrade detection |
| Category | Groups the list — `Browsers`, `Utilities`, … |
| Description | One line |
| Version scheme | `auto` is usually right; `numeric` for `25.01`, `semver` for `1.92.0` |
| Silent arguments | The vendor's unattended switch, e.g. `/S` or `/VERYSILENT /NORESTART` |
| Requires administrator | On by default; correct for most installers |
| Detect whether installed | Leave on — this is what prevents pointless reinstalls |

3. **Preview** shows exactly what will change, without writing anything.
4. **Add to Drive** copies the installer to
   `installers/<os>/<id>/`, writes the catalogue entry, records the SHA-256,
   and re-validates the whole drive. If anything is wrong the reason appears in
   red and **nothing is written**.

The application then appears in the list immediately, ready to install.

### Getting the silent switch right

This is the one field worth checking by hand. Common ones:

| Installer type | Switch | Seen in |
| --- | --- | --- |
| NSIS | `/S` | 7-Zip, VLC, Notepad++ |
| Inno Setup | `/VERYSILENT /NORESTART` | VS Code |
| MSI | `/qn /norestart` | Chrome Enterprise, Zoom |
| InstallShield | `/s /v"/qn"` | older enterprise software |

Test once from a command prompt: if a window appears, the switch is wrong and
an unattended run will sit waiting for a human who is not there.

Leave it empty if you are unsure — the installer will run interactively and you
can click through it, which is better than a silent run that hangs.

### Removing software from the drive

**Drive → Remove Application…**, pick the ID, optionally tick *Also delete its
installer files*. Again: this edits the drive only. Nothing is uninstalled from
any computer.

---

## The Drive menu

| Item | What it does |
| --- | --- |
| Add Application… | Put new software on the drive (above) |
| Remove Application… | Take software off the drive |
| Validate Drive… | Full check: paths, file types, dependencies, checksums |
| Update Checksums | Re-hash every installer — run after replacing files |
| Reload Drive | Re-read the drive after editing it elsewhere |
| Where Are My Logs? | The current run's log directory |

Help → **This Computer** shows everything detected about the machine, which is
the first thing to paste into a ticket.

---

## Command line

Everything the GUI does is available for scripting:

```bash
usbinstaller --list                          # what can be installed here
usbinstaller --info                          # detected OS, arch, privileges
usbinstaller --install-all --dry-run         # full plan, no changes
usbinstaller --install firefox vlc --yes     # install a selection
usbinstaller --install-all --skip chrome -y
usbinstaller --retry                         # re-run last run's failures
usbinstaller --verify                        # validate and re-hash the drive
usbinstaller --install-all --yes --json      # machine-readable output
```

Exit codes: `0` success · `1` an installation failed · `2` usage or drive
error · `3` validation failed · `4` cancelled · `5` administrator rights
required but not held.

---

## Logs

Every run writes `logs/<date>_<time>/` on the drive:

| File | Contents |
| --- | --- |
| `installation.log` | Timestamped narrative of the whole run |
| `results.json` | Machine-readable outcome per application |
| `plan.json` | What was decided, and why |
| `system.json` | The computer it ran on |

Attach all four when reporting a failure. None of them contain passwords or
licence keys — arguments that look like secrets are redacted before they are
written.

If the drive is write-protected, logs go to the system temporary folder
instead and the run tells you so.

---

## What this tool will not do

- Touch, move or delete your files.
- Uninstall anything.
- Replace newer software with older, unless you explicitly force it.
- Run anything that is not listed in the drive's catalogue.
- Run an installer whose checksum does not match.
- Disable Windows Defender, SmartScreen, Gatekeeper or SIP.
- Download anything — it works entirely offline.
- Ask for administrator rights any way other than through your operating
  system's own prompt.

---

## When something goes wrong

1. Read the failure line in the report — it names the exit code.
2. Click **Retry** for anything transient.
3. Check the log directory named at the end of the run.
4. Look the code up in [TROUBLESHOOTING.md](TROUBLESHOOTING.md) — `1603`,
   `1618`, `3010` and the common macOS failures are all listed there.

Quick ones:

| Symptom | Try this |
| --- | --- |
| Everything fails instantly | You are not administrator — restart elevated |
| "installer missing" | The drive is incomplete; tell whoever prepared it |
| "checksum MISMATCH" | The drive was modified after preparation. Do not override it — get it re-verified |
| A single app fails with 1603 | Often already installed, or a reboot is pending |
| Nothing is listed at all | Check Help → This Computer; the drive may have no software for this platform |
