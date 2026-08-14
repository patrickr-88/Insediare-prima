# Notes for this drive

- Standard build: browsers, media, utilities, Zoom, VS Code.
- Chrome is not selected by default; tick it only when the site standard asks
  for it.
- Everything on this drive installs machine-wide and needs administrator
  rights: on Windows start the launcher with "Run as administrator", on macOS
  approve the authentication prompt.
- If an installation fails, the log directory named at the end of the run
  contains `installation.log` and `results.json` — attach both to the ticket.

## Using this drive

- Full walkthrough: `USER_GUIDE.md` in the project, or ask IT for a printout.
- Double-click `Launch-Windows.cmd` or `Launch-macOS.command`.
- Click any application to see exactly what will run before you commit.
- To put new software on this drive: **Drive → Add Application…**. That edits
  the drive only — it installs nothing on the computer in front of you.
- After adding or replacing an installer by hand, run
  `repository-manager --repository . --checksums`, or the drive will refuse to
  run the changed file.
