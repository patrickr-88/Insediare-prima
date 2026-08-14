#!/usr/bin/env python3
"""Render the GUI and capture screenshots for the documentation.

Builds a throwaway repository with realistic (but entirely fake) installers,
simulates a Windows machine that already has some of the software, then opens
each window in turn and photographs it.

Nothing outside the output directory and a temporary folder is touched; the
"installers" are inert files that are never executed.

Usage::

    # Linux (headless): needs tkinter, Xvfb and mss
    sudo apt-get install -y python3-tk xvfb
    python -m pip install mss
    xvfb-run -a --server-args="-screen 0 1500x1100x24" \\
        python tools/capture_screenshots.py docs/screenshots

    # Optional: run a window manager first (openbox, matchbox) so the captures
    # include title bars, as they do on a real desktop.

    # Windows / macOS: no Xvfb needed, and the result matches what technicians
    # actually see, because Tk uses the platform's native widget theme.
    python tools/capture_screenshots.py docs/screenshots
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

#: (id, name, version, category, description, installed version or None)
CATALOGUE = [
    ("firefox", "Mozilla Firefox", "141.0", "Browsers", "Web browser", "140.0"),
    ("chrome", "Google Chrome", "127.0", "Browsers", "Web browser", "127.0"),
    ("7zip", "7-Zip", "25.01", "Compression", "File archiver", None),
    ("vlc", "VLC media player", "3.0.21", "Media", "Plays almost anything", "4.0.1"),
    ("vscode", "Visual Studio Code", "1.92.0", "Development", "Source code editor", None),
    (
        "adobe-reader",
        "Adobe Acrobat Reader",
        "24.002",
        "Documents",
        "PDF reader",
        None,
    ),
    ("zoom", "Zoom Workplace", "6.1.6", "Communication", "Video conferencing", None),
    ("notepadplusplus", "Notepad++", "8.6.9", "Editors", "Text editor", None),
]

#: This one's installer is deliberately absent, to show an error state.
MISSING_INSTALLER = "adobe-reader"


def build_repository(root: Path) -> Path:
    """A realistic-looking drive whose installers are inert placeholder files."""
    (root / "config").mkdir(parents=True, exist_ok=True)
    (root / "checksums").mkdir(parents=True, exist_ok=True)

    entries = []
    for app_id, name, version, category, description, _installed in CATALOGUE:
        relative = f"installers/windows/{app_id}/{app_id}-setup.exe"
        if app_id != MISSING_INSTALLER:
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            # Padded so the details panel shows a plausible size.
            path.write_bytes(b"MZ placeholder installer\n" + b"\0" * (7_400_000))
        entries.append(
            {
                "id": app_id,
                "name": name,
                "version": version,
                "category": category,
                "description": description,
                "version_scheme": "numeric",
                "selected_by_default": app_id != "chrome",
                "detection": {
                    "method": "windows_registry",
                    "display_name": f"^{name}",
                },
                "windows": {
                    "installer": relative,
                    "type": "exe",
                    "arguments": ["/S"],
                    "architectures": ["x64"],
                    "requires_admin": True,
                },
            }
        )

    (root / "config" / "applications.json").write_text(
        json.dumps({"schema_version": 1, "applications": entries}, indent=2),
        encoding="utf-8",
    )
    (root / "config" / "settings.json").write_text(
        json.dumps(
            {"repository_name": "Workshop Standard Build", "require_confirmation": True},
            indent=2,
        ),
        encoding="utf-8",
    )

    from usbinstaller.repository import Repository
    from usbinstaller.repository_manager import RepositoryManager

    RepositoryManager(Repository.load(root)).generate_checksums()
    return root


def fake_registry() -> list[dict[str, str]]:
    """Uninstall entries for the applications we pretend are already installed."""
    return [
        {
            "key": f"{{{app_id}}}",
            "hive": "HKLM",
            "DisplayName": name,
            "DisplayVersion": installed,
            "InstallLocation": f"C:\\Program Files\\{name}",
            "Publisher": "Vendor",
        }
        for app_id, name, _v, _c, _d, installed in CATALOGUE
        if installed
    ]


class Camera:
    """Captures a Tk window by its on-screen geometry."""

    def __init__(self, output: Path) -> None:
        self.output = output
        self.output.mkdir(parents=True, exist_ok=True)
        import mss

        self.sct = mss.mss()

    def shoot(self, window, name: str, *, settle: float = 0.6, margin: int = 30) -> Path:
        """Photograph *window*; *margin* leaves room for title-bar decoration."""
        window.update_idletasks()
        window.update()
        time.sleep(settle)
        window.update()

        # winfo_rootx/y are the *client* area; a window manager draws its
        # decoration above and around that, so grab a little extra.
        left = max(window.winfo_rootx() - margin, 0)
        top = max(window.winfo_rooty() - margin, 0)
        box = {
            "left": left,
            "top": top,
            "width": window.winfo_width() + (window.winfo_rootx() - left) + margin,
            "height": window.winfo_height() + (window.winfo_rooty() - top) + margin,
        }
        shot = self.sct.grab(box)

        from PIL import Image

        image = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
        path = self.output / f"{name}.png"
        image.save(path, optimize=True)
        print(f"  {path}  ({image.width}×{image.height})")
        return path


def capture(output: Path, workdir: Path) -> list[Path]:

    from usbinstaller.app import InstallerApp
    from usbinstaller.config.authoring import suggest_type
    from usbinstaller.models import OS
    from usbinstaller.ui import gui as gui_module
    from usbinstaller.ui.gui import (
        AddApplicationDialog,
        InstallerWindow,
        PlanDialog,
        ProgressWindow,
        TextDialog,
    )

    root = build_repository(workdir / "USB_INSTALLER")
    downloads = workdir / "Downloads"
    downloads.mkdir(parents=True, exist_ok=True)
    # Deliberately something the demo catalogue does not already contain, so
    # the dialog shows its success preview rather than a duplicate-ID refusal.
    picked = downloads / "putty-64bit-0.81-installer.msi"
    picked.write_bytes(b"MZ placeholder\n" + b"\0" * 4_200_000)

    # Pretend to be a Windows 11 workstation with some software already on it.
    sys.path.insert(0, str(ROOT / "tests"))
    import usbinstaller.app as app_module
    from conftest import windows_probe  # noqa: E402
    from usbinstaller.sysdetect.system import detect_system as real_detect

    app_module.detect_system = (
        lambda _probe=None, repository_path=None: real_detect(
            windows_probe(hostname="RECEPTION-PC"), repository_path=repository_path
        )
    )

    app = InstallerApp.create(root, with_log=True)
    app.detection.registry_reader = fake_registry

    camera = Camera(output)
    shots: list[Path] = []

    window = InstallerWindow(app)
    window.update()
    # Let the background detection finish so the status lines are populated.
    time.sleep(1.5)
    window.update()
    window.show_details("firefox")
    print("Capturing:")
    shots.append(camera.shoot(window, "01-main-window"))

    # The details panel for an application that is not installed yet.
    window.show_details("vscode")
    shots.append(camera.shoot(window, "02-details-not-installed"))

    # Modal dialogs block in wait_window; bypass that so we can photograph them.
    original_wait = window.wait_window
    window.wait_window = lambda *a, **k: None  # type: ignore[assignment]

    plan = app.plan([a.id for a in app.available()])
    plan_dialog = PlanDialog(window, gui_module.render_plan(plan), dry_run=False)
    shots.append(camera.shoot(plan_dialog, "03-review-installation"))
    plan_dialog.destroy()

    add = AddApplicationDialog(window, app)
    add.name_var.set("PuTTY")
    add.version_var.set("0.81")
    add.category_var.set("Utilities")
    add.description_var.set("SSH and telnet client")
    add.arguments_var.set("/qn /norestart")
    add.sources[OS.WINDOWS] = picked
    add.type_vars[OS.WINDOWS].set(suggest_type(picked, OS.WINDOWS) or "msi")
    add.file_labels[OS.WINDOWS].config(text=picked.name, foreground="")
    add._preview()
    shots.append(camera.shoot(add, "04-add-application"))
    add.destroy()

    window.wait_window = original_wait  # type: ignore[assignment]

    # Progress and the final report, driven with representative outcomes.
    progress = ProgressWindow(window, 5)
    progress.set_progress(4, 5, "Visual Studio Code")
    for line in (
        "Mozilla Firefox: SUCCESS",
        "7-Zip: SUCCESS",
        "Zoom Workplace: SUCCESS",
        "Adobe Acrobat Reader: FAILED — installer exited with code 1603",
    ):
        progress.append(line)
    shots.append(camera.shoot(progress, "05-installing"))

    progress.finish(
        "INSTALLATION COMPLETE\n\n"
        "Mozilla Firefox ......... SUCCESS\n"
        "7-Zip ................... SUCCESS\n"
        "Zoom Workplace .......... SUCCESS\n"
        "Visual Studio Code ...... SUCCESS\n"
        "Adobe Acrobat Reader .... FAILED   (installer exited with code 1603)\n"
        "Google Chrome ........... SKIPPED  (already installed (127.0))\n"
        "VLC media player ........ SKIPPED  (newer version already installed)\n\n"
        "Successful: 4\n"
        "Skipped:    2\n"
        "Failed:     1\n\n"
        "Failed applications: adobe-reader\n"
        f"Installation log: {app.log.directory if app.log else ''}",
        failed=["adobe-reader"],
    )
    shots.append(camera.shoot(progress, "06-results"))
    progress.destroy()

    report = TextDialog(window, "Drive Validation", app.validate().render())
    shots.append(camera.shoot(report, "07-validate-drive"))
    report.destroy()

    window.destroy()
    if app.log is not None:
        app.log.close()
    return shots


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("output", nargs="?", default="docs/screenshots")
    args = parser.parse_args(argv)

    import tempfile

    with tempfile.TemporaryDirectory(prefix="usbinstaller-shots-") as tmp:
        shots = capture(Path(args.output).resolve(), Path(tmp))
    print(f"\n{len(shots)} screenshot(s) written to {Path(args.output).resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
