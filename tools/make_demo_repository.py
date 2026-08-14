#!/usr/bin/env python3
"""Build a runnable demo USB repository with harmless mock installers.

The demo lets a technician (or CI) exercise the whole workflow — detection,
planning, confirmation, installation, verification, logging, retry — without
downloading vendor installers and without changing anything on the machine.

Every mock "installer" is a small PowerShell/shell script that writes a marker
file into ``<repository>/demo-state/`` and exits with a configured code. Nothing
is installed, no system location is written to, and the applications detect
themselves by the presence of their own marker file.

Usage::

    python tools/make_demo_repository.py build/demo-usb
    usbinstaller --repository build/demo-usb --list
    usbinstaller --repository build/demo-usb --install-all --dry-run
    usbinstaller --repository build/demo-usb --install-all --yes
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from usbinstaller.repository import Repository  # noqa: E402
from usbinstaller.repository_manager import RepositoryManager  # noqa: E402

#: (id, name, version, category, exit_code, description)
DEMO_APPS = [
    ("demo-browser", "Demo Browser", "141.0", "Browsers", 0, "Pretends to be a browser"),
    ("demo-media", "Demo Media Player", "3.0.21", "Media", 0, "Pretends to play media"),
    ("demo-archiver", "Demo Archiver", "25.01", "Utilities", 0, "Pretends to zip things"),
    ("demo-editor", "Demo Editor", "1.92.0", "Development", 0, "Pretends to edit code"),
    (
        "demo-broken",
        "Demo Broken App",
        "2.0",
        "Diagnostics",
        1,
        "Always fails — use it to see failure handling and --retry",
    ),
]

SHELL_TEMPLATE = """#!/bin/sh
# Mock installer for {app_id}. Writes a marker file and exits 0.
set -e
STATE_DIR="$(cd "$(dirname "$0")/../../../demo-state" && pwd)"
mkdir -p "$STATE_DIR"
printf '%s\\n' "{version}" > "$STATE_DIR/{app_id}.version"
echo "mock installer for {app_id} finished"
"""

#: The failing mock installs nothing, so a retry starts from a clean state.
FAILING_SHELL_TEMPLATE = """#!/bin/sh
echo "mock installer for {app_id} could not complete" >&2
exit {exit_code}
"""

POWERSHELL_TEMPLATE = """# Mock installer for {app_id}. Writes a marker file and exits 0.
$stateDir = Join-Path (Resolve-Path "$PSScriptRoot\\..\\..\\..") "demo-state"
New-Item -ItemType Directory -Force -Path $stateDir | Out-Null
Set-Content -Path (Join-Path $stateDir "{app_id}.version") -Value "{version}"
Write-Output "mock installer for {app_id} finished"
"""

FAILING_POWERSHELL_TEMPLATE = """# Mock installer for {app_id}.
# Fails without installing anything.
Write-Error "mock installer for {app_id} could not complete"
exit {exit_code}
"""


def build(target: Path, *, force: bool = False) -> Path:
    if target.exists():
        if not force:
            raise SystemExit(
                f"{target} already exists; pass --force to rebuild it from scratch"
            )
        shutil.rmtree(target)

    for directory in (
        "config",
        "checksums",
        "logs",
        "scripts/windows",
        "scripts/macos",
        "documentation",
        "demo-state",
    ):
        (target / directory).mkdir(parents=True, exist_ok=True)

    applications = []
    for app_id, name, version, category, exit_code, description in DEMO_APPS:
        win_dir = target / "installers" / "windows" / app_id
        mac_dir = target / "installers" / "macos" / app_id
        win_dir.mkdir(parents=True, exist_ok=True)
        mac_dir.mkdir(parents=True, exist_ok=True)

        windows_template = (
            FAILING_POWERSHELL_TEMPLATE if exit_code else POWERSHELL_TEMPLATE
        )
        macos_template = FAILING_SHELL_TEMPLATE if exit_code else SHELL_TEMPLATE

        win_script = win_dir / f"install-{app_id}.ps1"
        win_script.write_text(
            windows_template.format(
                app_id=app_id, version=version, exit_code=exit_code
            ),
            encoding="utf-8",
        )
        mac_script = mac_dir / f"install-{app_id}.sh"
        mac_script.write_text(
            macos_template.format(app_id=app_id, version=version, exit_code=exit_code),
            encoding="utf-8",
        )
        mac_script.chmod(0o755)

        # Absolute marker path, baked in when the demo is generated: detection
        # paths are target-machine paths, not repository-relative ones.
        marker = str(target / "demo-state" / f"{app_id}.version")
        applications.append(
            {
                "id": app_id,
                "name": name,
                "version": version,
                "description": description,
                "category": category,
                "version_scheme": "numeric",
                # Presence is enough to say "installed"; the per-platform
                # overrides below also read the *version* out of the marker, so
                # the demo exercises upgrade/skip decisions rather than just
                # "something is there".
                "detection": {"method": "path_exists", "paths": [marker]},
                "windows": {
                    "installer": f"installers/windows/{app_id}/install-{app_id}.ps1",
                    "type": "powershell",
                    "requires_admin": False,
                    "detection": {
                        "method": "command_version",
                        "command": [
                            "C:\\Windows\\System32\\cmd.exe",
                            "/c",
                            "type",
                            marker,
                        ],
                    },
                },
                "macos": {
                    "installer": f"installers/macos/{app_id}/install-{app_id}.sh",
                    "type": "shell",
                    "requires_admin": False,
                    "detection": {
                        "method": "command_version",
                        "command": ["/bin/cat", marker],
                    },
                },
            }
        )

    # A dependency chain, so the demo also shows installation ordering.
    applications[3]["dependencies"] = ["demo-archiver"]

    (target / "config" / "applications.json").write_text(
        json.dumps({"schema_version": 1, "applications": applications}, indent=2) + "\n",
        encoding="utf-8",
    )
    (target / "config" / "settings.json").write_text(
        json.dumps(
            {
                "repository_name": "Demo Repository",
                "strict_checksums": False,
                "require_confirmation": True,
                "continue_on_failure": True,
                "default_timeout_seconds": 120,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (target / "documentation" / "README.md").write_text(
        "This is a generated demo repository. The installers are mock scripts "
        "that only write files into demo-state/. Delete the whole directory when "
        "you are finished with it.\n",
        encoding="utf-8",
    )

    manager = RepositoryManager(Repository.load(target))
    manager.generate_checksums()
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("target", nargs="?", default="build/demo-usb")
    parser.add_argument("--force", action="store_true", help="overwrite an existing demo")
    args = parser.parse_args(argv)

    target = build(Path(args.target).resolve(), force=args.force)
    print(f"Demo repository created at {target}")
    print("\nTry:")
    print(f"  usbinstaller --repository {target} --list")
    print(f"  usbinstaller --repository {target} --install-all --dry-run")
    print(f"  usbinstaller --repository {target} --install-all --yes")
    print(f"  usbinstaller --repository {target} --retry")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
