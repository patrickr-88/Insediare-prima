# USB software repository

This directory is the *data* half of the system: the installer application
reads it, and never needs to change when its contents do.

```
config/         applications.json (the catalogue) and settings.json
installers/     the actual installer files, per OS and per application
scripts/        optional pre/post-install scripts referenced by the catalogue
checksums/      checksums.json — SHA-256 of every declared installer
logs/           one directory per run (created automatically)
documentation/  notes for the technicians using this particular drive
```

The catalogue shipped here describes eight common applications but contains no
binaries — vendor installers cannot be redistributed. To make the drive usable:

1. Download each installer from the vendor.
2. Save it at exactly the path named in `config/applications.json`
   (e.g. `installers/windows/firefox/FirefoxSetup.exe`).
3. Record checksums: `repository-manager --repository . --checksums`
4. Validate: `usbinstaller --repository . --validate-config`

See `../docs/ADDING_SOFTWARE.md` for the full procedure, and
`tools/make_demo_repository.py` for a runnable demo repository with harmless
mock installers.
