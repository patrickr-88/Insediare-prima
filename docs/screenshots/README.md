# Interface screenshots

Every image here is a real capture of the running application — not a mock-up.
Regenerate them with:

```bash
python tools/capture_screenshots.py docs/screenshots
```

The script builds a throwaway drive with eight applications (whose "installers"
are inert placeholder files), simulates a Windows 11 workstation that already
has Firefox 140, Chrome 127 and VLC 4.0.1 installed, then photographs each
window.

> **A note on appearance:** these were captured on Linux/X11, which is what CI
> and the development container can run headlessly. Tk uses the *native* widget
> theme on each platform, so on Windows and macOS the same layout appears with
> that platform's buttons, fonts and title bars. The content, wording and
> behaviour are identical.

| | |
| --- | --- |
| [`01-main-window.png`](01-main-window.png) | Main window. Detected machine in the header, software grouped by category with live status under each entry, details panel on the right. Firefox shows an available update, Chrome is up to date, Acrobat Reader's installer is missing from the drive. |
| [`02-details-not-installed.png`](02-details-not-installed.png) | The details panel for software that is not installed yet: the exact installer that will run, its type, size, arguments, privilege requirement, detection method and checksum state. |
| [`03-review-installation.png`](03-review-installation.png) | The plan, shown before anything is touched. Install, upgrade, skip and error lines each state their reason. This is the last point at which nothing has changed. |
| [`04-add-application.png`](04-add-application.png) | Adding software to the drive. The installer type was inferred from the file; **Preview** shows exactly what will be written without writing it. |
| [`05-installing.png`](05-installing.png) | Installation in progress. Each application reports as it finishes; a failure does not stop the run. |
| [`06-results.png`](06-results.png) | The final report: successes, skips with reasons, failures with exit codes, the log directory, and a button to retry just the failures. |
| [`07-validate-drive.png`](07-validate-drive.png) | *Drive → Validate Drive…* — here catching the deliberately missing Acrobat Reader installer and an installer with no checksum. |
