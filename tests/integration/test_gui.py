"""The GUI's behaviour, tested through the layers it actually calls.

The widget tree needs a display, so it is exercised manually (see the checklist
in TESTING.md). Everything the GUI *does* — gather information, add an
application to the drive, reload, plan, install — lives in the service layer
and is tested here, end to end, exactly in the order the dialogs call it.

A separate smoke test imports and builds the window when a display happens to
be available, so the widget code cannot rot unnoticed.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

from tests.conftest import app_entry, macos_probe, windows_probe, write_repository
from usbinstaller.app import InstallerApp
from usbinstaller.config.authoring import (
    ApplicationDraft,
    AuthoringError,
    InstallerDraft,
    suggest_detection,
    suggest_id,
    suggest_type,
)
from usbinstaller.models import OS, Action, Status

HAS_TKINTER = importlib.util.find_spec("tkinter") is not None
HAS_DISPLAY = bool(os.environ.get("DISPLAY") or os.name == "nt")

needs_gui = pytest.mark.skipif(
    not (HAS_TKINTER and HAS_DISPLAY),
    reason="requires tkinter and a display",
)


@pytest.fixture
def drive(tmp_path: Path) -> Path:
    """A small drive with one Windows application already on it."""
    root = tmp_path / "usb"
    write_repository(
        root,
        [app_entry("firefox", name="Firefox", version="141.0", macos=False)],
        settings={"require_confirmation": False, "repository_name": "Test Drive"},
    )
    return root


@pytest.fixture
def downloaded_installer(tmp_path: Path) -> Path:
    path = tmp_path / "Downloads" / "7z2501-x64.exe"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"MZ mock 7-zip installer")
    return path


class TestAddApplicationFlow:
    """The exact sequence AddApplicationDialog performs when 'Add to Drive' is clicked."""

    def test_a_technician_adds_software_and_can_immediately_install_it(
        self, drive, downloaded_installer, tmp_path
    ):
        app = InstallerApp.create(drive, probe=windows_probe())
        assert [a.id for a in app.available()] == ["firefox"]

        # 1. The dialog infers these from the chosen file and typed name.
        assert suggest_type(downloaded_installer, OS.WINDOWS) == "exe"
        app_id = suggest_id("7-Zip")
        assert app_id == "7-zip"

        draft = ApplicationDraft(
            id=app_id,
            name="7-Zip",
            version="25.01",
            category="Utilities",
            description="File archiver",
            version_scheme="numeric",
            installers=(
                InstallerDraft(
                    os=OS.WINDOWS,
                    source=downloaded_installer,
                    type="exe",
                    arguments=["/S"],
                    requires_admin=True,
                    detection=suggest_detection(OS.WINDOWS, "7-Zip"),
                ),
            ),
        )

        # 2. Preview changes nothing.
        preview = app.editor.add_application(draft, preview=True)
        assert "would be added" in preview.summary
        assert [a.id for a in app.reload().available()] == ["firefox"]

        # 3. Add to Drive.
        app.editor.add_application(draft)

        # 4. The window reloads the drive and the new entry appears.
        app = app.reload()
        assert sorted(a.id for a in app.available()) == ["7-zip", "firefox"]

        # 5. Its details panel is populated and honest about what will run.
        details = app.details_for("7-zip")
        assert details.compatible is True
        assert details.installer_path == "installers/windows/7-zip/7z2501-x64.exe"
        assert details.arguments == ("/S",)
        assert details.requires_admin is True
        assert details.checksum_state == "recorded"
        assert "7-Zip" in details.render()

        # 6. It plans like anything else on the drive.
        item = next(i for i in app.plan(["7-zip"]).items if i.app_id == "7-zip")
        assert item.action is Action.INSTALL

    def test_the_drive_still_validates_after_a_gui_edit(self, drive, downloaded_installer):
        app = InstallerApp.create(drive, probe=windows_probe())
        app.editor.add_application(
            ApplicationDraft(
                id="7-zip",
                name="7-Zip",
                version="25.01",
                installers=(
                    InstallerDraft(
                        os=OS.WINDOWS, source=downloaded_installer, type="exe"
                    ),
                ),
            )
        )
        report = app.reload().validate(check_checksums=True)
        # The pre-existing entry has no checksum, so warnings are expected; what
        # matters is that the edit introduced no errors.
        assert report.ok, report.render()

    def test_an_added_application_really_installs(self, tmp_path):
        """Add a runnable mock installer through the API, then install it."""
        root = tmp_path / "usb"
        target = tmp_path / "target"
        target.mkdir()
        write_repository(
            root, [app_entry("keep", windows=False)], settings={"require_confirmation": False}
        )

        script = tmp_path / "Downloads" / "install-tool.sh"
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text(f"#!/bin/sh\necho '2.0' > '{target}/tool.marker'\n")

        app = InstallerApp.create(root, probe=macos_probe())
        app.editor.add_application(
            ApplicationDraft(
                id="tool",
                name="Tool",
                version="2.0",
                installers=(
                    InstallerDraft(
                        os=OS.MACOS,
                        source=script,
                        type="shell",
                        requires_admin=False,
                    ),
                ),
            )
        )

        app = app.reload()
        result = app.install(app.plan(["tool"]))
        assert result.items[0].status is Status.SUCCESS
        assert (target / "tool.marker").read_text().strip() == "2.0"

    def test_a_rejected_draft_leaves_the_drive_untouched(self, drive, downloaded_installer):
        """What the dialog shows in red, and why nothing is written."""
        app = InstallerApp.create(drive, probe=windows_probe())
        before = (drive / "config" / "applications.json").read_text()

        duplicate = ApplicationDraft(
            id="firefox",
            name="Firefox again",
            installers=(
                InstallerDraft(os=OS.WINDOWS, source=downloaded_installer, type="exe"),
            ),
        )
        problems = app.editor.validate_draft(duplicate)
        assert any("already on this drive" in p for p in problems)

        with pytest.raises(AuthoringError):
            app.editor.add_application(duplicate)
        assert (drive / "config" / "applications.json").read_text() == before

    def test_remove_then_reload(self, drive):
        app = InstallerApp.create(drive, probe=windows_probe())
        app.editor.remove_application("firefox", delete_files=True)
        assert app.reload().available() == ()


class TestGuiSmoke:
    def test_the_gui_module_imports_when_tkinter_is_present(self):
        if not HAS_TKINTER:
            pytest.skip("tkinter is not installed in this environment")
        from usbinstaller.ui import gui

        assert callable(gui.run_gui)
        assert hasattr(gui, "AddApplicationDialog")
        assert hasattr(gui, "InstallerWindow")

    def test_the_cli_falls_back_gracefully_without_tkinter(self, drive, capsys, monkeypatch):
        """--gui on a machine without tkinter must explain itself, not traceback."""
        import sys

        from usbinstaller import cli

        # Setting the entry to None makes the import machinery raise
        # ImportError, exactly as a Python build without tkinter would.
        monkeypatch.setitem(sys.modules, "usbinstaller.ui.gui", None)
        code = cli.main(["--repository", str(drive), "--gui"])
        assert code == cli.EXIT_USAGE
        assert "command line" in capsys.readouterr().err

    @needs_gui
    def test_the_main_window_builds(self, drive):
        from usbinstaller.ui.gui import InstallerWindow

        app = InstallerApp.create(drive, probe=windows_probe())
        window = InstallerWindow(app)
        try:
            window.update_idletasks()
            assert window.selected_ids() == ["firefox"]
            assert "1 of 1 selected" in window.count_label.cget("text")
            window.show_details("firefox")
        finally:
            window.destroy()
