"""Catalogue authoring: the write path behind the GUI's Add Application."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.conftest import app_entry, write_repository
from usbinstaller.config.authoring import (
    ApplicationDraft,
    AuthoringError,
    CatalogueEditor,
    InstallerDraft,
    installer_filter,
    safe_filename,
    suggest_detection,
    suggest_id,
    suggest_os,
    suggest_type,
)
from usbinstaller.models import OS, Arch
from usbinstaller.repository import Repository
from usbinstaller.security.checksums import sha256_file


@pytest.fixture
def repo(tmp_path: Path) -> Repository:
    write_repository(
        tmp_path / "usb", [app_entry("existing", name="Existing App")]
    )
    return Repository.load(tmp_path / "usb")


@pytest.fixture
def source_exe(tmp_path: Path) -> Path:
    path = tmp_path / "downloads" / "SetupThing.exe"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"MZ fake installer payload")
    return path


def draft(source: Path, **kwargs) -> ApplicationDraft:
    defaults = dict(
        id="newapp",
        name="New App",
        version="1.0",
        category="Utilities",
        installers=(
            InstallerDraft(os=OS.WINDOWS, source=source, type="exe", arguments=["/S"]),
        ),
    )
    defaults.update(kwargs)
    return ApplicationDraft(**defaults)  # type: ignore[arg-type]


class TestSuggestions:
    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("7-Zip", "7-zip"),
            ("Mozilla Firefox", "mozilla-firefox"),
            ("Notepad++", "notepad"),
            ("  VLC  ", "vlc"),
            ("!!!", "application"),
            ("Adobe Acrobat Reader DC", "adobe-acrobat-reader-dc"),
        ],
    )
    def test_suggest_id(self, name, expected):
        assert suggest_id(name) == expected

    @pytest.mark.parametrize(
        ("filename", "os_", "expected"),
        [
            ("Setup.exe", OS.WINDOWS, "exe"),
            ("App.MSI", OS.WINDOWS, "msi"),
            ("bundle.msix", OS.WINDOWS, "msix"),
            ("install.ps1", OS.WINDOWS, "powershell"),
            ("Firefox.dmg", OS.MACOS, "dmg"),
            ("Thing.pkg", OS.MACOS, "pkg"),
            ("install.sh", OS.MACOS, "shell"),
            ("Setup.exe", OS.MACOS, None),
            ("readme.txt", OS.WINDOWS, None),
        ],
    )
    def test_suggest_type(self, filename, os_, expected):
        assert suggest_type(filename, os_) == expected

    def test_suggest_os(self):
        assert suggest_os("Setup.exe") is OS.WINDOWS
        assert suggest_os("Firefox.dmg") is OS.MACOS
        assert suggest_os("notes.txt") is None

    def test_suggest_detection_is_platform_appropriate(self):
        windows = suggest_detection(OS.WINDOWS, "Notepad++")
        assert windows.method == "windows_registry"
        # The name is regex-escaped, so "++" cannot break the pattern.
        assert windows.options["display_name"] == "^Notepad\\+\\+"

        mac = suggest_detection(OS.MACOS, "Firefox")
        assert mac.method == "macos_app_bundle"
        assert mac.options["bundle"] == "Firefox.app"

    def test_installer_filters_cover_every_type(self):
        patterns = " ".join(p for _label, p in installer_filter(OS.WINDOWS))
        assert "*.exe" in patterns and "*.msi" in patterns and "*.ps1" in patterns


class TestSafeFilename:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Setup.exe", "Setup.exe"),
            ("../../etc/passwd", "passwd"),
            ("C:\\Windows\\System32\\cmd.exe", "cmd.exe"),
            ("weird;name&here.exe", "weird_name_here.exe"),
            ("spaced name (x64).exe", "spaced name (x64).exe"),
        ],
    )
    def test_sanitisation(self, raw, expected):
        assert safe_filename(raw) == expected

    def test_reserved_windows_device_names_are_defused(self):
        assert safe_filename("con.exe") == "con_file.exe"

    def test_empty_name_is_refused(self):
        with pytest.raises(AuthoringError):
            safe_filename("...")


class TestValidation:
    def test_a_good_draft_has_no_problems(self, repo, source_exe):
        assert CatalogueEditor(repo).validate_draft(draft(source_exe)) == []

    def test_duplicate_id_is_reported(self, repo, source_exe):
        problems = CatalogueEditor(repo).validate_draft(
            draft(source_exe, id="existing")
        )
        assert any("already on this drive" in p for p in problems)

    def test_bad_id_characters_are_reported(self, repo, source_exe):
        problems = CatalogueEditor(repo).validate_draft(draft(source_exe, id="New App!"))
        assert any("lower-case letters" in p for p in problems)

    def test_missing_name_and_installers(self, repo, source_exe):
        problems = CatalogueEditor(repo).validate_draft(
            ApplicationDraft(id="x", name="")
        )
        assert any("display name is required" in p for p in problems)
        assert any("at least one installer" in p for p in problems)

    def test_missing_source_file(self, repo, tmp_path):
        problems = CatalogueEditor(repo).validate_draft(
            draft(tmp_path / "nope.exe")
        )
        assert any("does not exist" in p for p in problems)

    def test_empty_source_file(self, repo, tmp_path):
        empty = tmp_path / "empty.exe"
        empty.write_bytes(b"")
        problems = CatalogueEditor(repo).validate_draft(draft(empty))
        assert any("is empty" in p for p in problems)

    def test_extension_must_match_the_type(self, repo, tmp_path):
        source = tmp_path / "payload.dmg"
        source.write_bytes(b"x")
        problems = CatalogueEditor(repo).validate_draft(
            draft(
                source,
                installers=(
                    InstallerDraft(os=OS.WINDOWS, source=source, type="exe"),
                ),
            )
        )
        assert any("does not look like" in p for p in problems)

    def test_type_must_be_valid_for_the_platform(self, repo, source_exe):
        problems = CatalogueEditor(repo).validate_draft(
            draft(
                source_exe,
                installers=(
                    InstallerDraft(os=OS.WINDOWS, source=source_exe, type="dmg"),
                ),
            )
        )
        assert any("not a valid windows installer type" in p for p in problems)

    def test_two_installers_for_one_platform_are_refused(self, repo, source_exe):
        problems = CatalogueEditor(repo).validate_draft(
            draft(
                source_exe,
                installers=(
                    InstallerDraft(os=OS.WINDOWS, source=source_exe, type="exe"),
                    InstallerDraft(os=OS.WINDOWS, source=source_exe, type="exe"),
                ),
            )
        )
        assert any("two windows installers" in p for p in problems)

    def test_invalid_version_for_the_scheme(self, repo, source_exe):
        problems = CatalogueEditor(repo).validate_draft(
            draft(source_exe, version="1.0", version_scheme="semver")
        )
        assert any("is not valid for the 'semver' scheme" in p for p in problems)

    def test_unknown_dependency_is_reported(self, repo, source_exe):
        problems = CatalogueEditor(repo).validate_draft(
            draft(source_exe, dependencies=("ghost",))
        )
        assert any("not on this drive" in p for p in problems)

    def test_self_dependency_is_reported(self, repo, source_exe):
        problems = CatalogueEditor(repo).validate_draft(
            draft(source_exe, dependencies=("newapp",))
        )
        assert any("cannot depend on itself" in p for p in problems)

    def test_existing_dependency_is_accepted(self, repo, source_exe):
        assert (
            CatalogueEditor(repo).validate_draft(
                draft(source_exe, dependencies=("existing",))
            )
            == []
        )


class TestAddApplication:
    def test_installer_is_copied_and_catalogue_updated(self, repo, source_exe):
        result = CatalogueEditor(repo).add_application(draft(source_exe))

        copied = repo.root / "installers" / "windows" / "newapp" / "SetupThing.exe"
        assert copied.exists()
        assert copied.read_bytes() == source_exe.read_bytes()
        assert source_exe.exists()  # the original is left alone

        catalogue = Repository.load(repo.root).catalogue
        entry = catalogue.get("newapp")
        assert entry is not None
        assert entry.name == "New App"
        assert entry.platforms[OS.WINDOWS].installer == (
            "installers/windows/newapp/SetupThing.exe"
        )
        assert entry.platforms[OS.WINDOWS].arguments == ["/S"]
        assert result.app_id == "newapp"

    def test_checksum_is_recorded_so_the_new_file_will_run(self, repo, source_exe):
        CatalogueEditor(repo).add_application(draft(source_exe))
        reloaded = Repository.load(repo.root)
        relative = "installers/windows/newapp/SetupThing.exe"
        assert reloaded.checksums.expected_for(relative) == sha256_file(
            reloaded.resolve(relative)
        )

    def test_the_added_application_validates_and_plans(self, repo, source_exe, windows_system):
        CatalogueEditor(repo).add_application(draft(source_exe))

        from usbinstaller.config.validator import validate_repository

        report = validate_repository(repo.root, check_checksums=True)
        assert report.ok, report.render()

        from tests.conftest import windows_probe
        from usbinstaller.app import InstallerApp

        app = InstallerApp.create(repo.root, probe=windows_probe())
        assert "newapp" in [a.id for a in app.available()]

    def test_both_platforms_at_once(self, repo, source_exe, tmp_path):
        dmg = tmp_path / "downloads" / "Thing.dmg"
        dmg.write_bytes(b"disk image")
        CatalogueEditor(repo).add_application(
            draft(
                source_exe,
                installers=(
                    InstallerDraft(os=OS.WINDOWS, source=source_exe, type="exe"),
                    InstallerDraft(
                        os=OS.MACOS, source=dmg, type="dmg", app_bundle="Thing.app"
                    ),
                ),
            )
        )
        entry = Repository.load(repo.root).catalogue.get("newapp")
        assert set(entry.platforms) == {OS.WINDOWS, OS.MACOS}
        assert entry.platforms[OS.MACOS].app_bundle == "Thing.app"
        assert (repo.root / "installers" / "macos" / "newapp" / "Thing.dmg").exists()

    def test_optional_fields_are_written(self, repo, source_exe):
        CatalogueEditor(repo).add_application(
            draft(
                source_exe,
                description="Does a thing",
                dependencies=("existing",),
                version_scheme="numeric",
                selected_by_default=False,
                installers=(
                    InstallerDraft(
                        os=OS.WINDOWS,
                        source=source_exe,
                        type="exe",
                        requires_admin=False,
                        architectures=(Arch.X64,),
                        detection=suggest_detection(OS.WINDOWS, "New App"),
                        success_exit_codes=(0, 3010),
                    ),
                ),
            )
        )
        entry = Repository.load(repo.root).catalogue.get("newapp")
        payload = entry.platforms[OS.WINDOWS]
        assert entry.description == "Does a thing"
        assert entry.dependencies == ("existing",)
        assert entry.selected_by_default is False
        assert payload.requires_admin is False
        assert payload.architectures == (Arch.X64,)
        assert payload.success_exit_codes == (0, 3010)
        assert payload.detection.method == "windows_registry"

    def test_existing_entries_are_preserved(self, repo, source_exe):
        CatalogueEditor(repo).add_application(draft(source_exe))
        ids = Repository.load(repo.root).catalogue.ids
        assert ids == ("existing", "newapp")

    def test_a_backup_of_the_previous_catalogue_is_kept(self, repo, source_exe):
        CatalogueEditor(repo).add_application(draft(source_exe))
        backup = repo.config_dir / "applications.json.bak"
        assert backup.exists()
        assert "newapp" not in backup.read_text()

    def test_unrelated_json_keys_survive_the_edit(self, repo, source_exe):
        path = repo.config_dir / "applications.json"
        document = json.loads(path.read_text())
        document["notes"] = "site standard build"
        path.write_text(json.dumps(document))

        CatalogueEditor(Repository.load(repo.root)).add_application(draft(source_exe))
        assert json.loads(path.read_text())["notes"] == "site standard build"

    def test_invalid_draft_writes_nothing(self, repo, source_exe):
        editor = CatalogueEditor(repo)
        with pytest.raises(AuthoringError):
            editor.add_application(draft(source_exe, id="existing"))
        assert not (repo.root / "installers" / "windows" / "existing" / "SetupThing.exe").exists()
        assert Repository.load(repo.root).catalogue.ids == ("existing",)

    def test_copied_files_are_removed_if_the_write_fails(
        self, repo, source_exe, monkeypatch
    ):
        """A failure part-way must not leave an orphan installer on the drive."""
        editor = CatalogueEditor(repo)
        monkeypatch.setattr(
            CatalogueEditor,
            "_write_document",
            lambda *_args, **_kw: (_ for _ in ()).throw(OSError("drive full")),
        )
        with pytest.raises(OSError):
            editor.add_application(draft(source_exe))
        assert not (repo.root / "installers" / "windows" / "newapp").exists() or not any(
            (repo.root / "installers" / "windows" / "newapp").iterdir()
        )

    def test_refuses_to_overwrite_an_existing_file(self, repo, source_exe):
        destination = repo.root / "installers" / "windows" / "newapp" / "SetupThing.exe"
        destination.parent.mkdir(parents=True)
        destination.write_bytes(b"something already here")

        with pytest.raises(AuthoringError, match="already exists"):
            CatalogueEditor(repo).add_application(draft(source_exe))
        assert destination.read_bytes() == b"something already here"

    def test_a_file_name_that_would_escape_the_drive_is_neutralised(
        self, repo, tmp_path
    ):
        nasty = tmp_path / "downloads"
        nasty.mkdir(exist_ok=True)
        source = nasty / "evil.exe"
        source.write_bytes(b"x")

        installer = InstallerDraft(os=OS.WINDOWS, source=source, type="exe")
        relative = installer.destination_relative("newapp")
        assert relative == "installers/windows/newapp/evil.exe"
        assert repo.resolve(relative).is_relative_to(repo.root)

    def test_an_installer_already_on_the_drive_is_referenced_not_copied(
        self, repo, tmp_path
    ):
        existing = repo.root / "installers" / "windows" / "manual" / "setup.exe"
        existing.parent.mkdir(parents=True)
        existing.write_bytes(b"already here")

        CatalogueEditor(repo).add_application(
            ApplicationDraft(
                id="manual",
                name="Manual App",
                version="1.0",
                installers=(
                    InstallerDraft(
                        os=OS.WINDOWS,
                        source=Path("installers/windows/manual/setup.exe"),
                        type="exe",
                        already_in_repository=True,
                    ),
                ),
            )
        )
        entry = Repository.load(repo.root).catalogue.get("manual")
        assert entry.platforms[OS.WINDOWS].installer == (
            "installers/windows/manual/setup.exe"
        )
        assert existing.read_bytes() == b"already here"


class TestPreview:
    def test_preview_changes_nothing(self, repo, source_exe):
        result = CatalogueEditor(repo).add_application(draft(source_exe), preview=True)
        assert result.preview is True
        assert result.copied == (
            (str(source_exe), "installers/windows/newapp/SetupThing.exe"),
        )
        assert "would be added" in result.summary
        assert Repository.load(repo.root).catalogue.ids == ("existing",)
        assert not (repo.root / "installers" / "windows" / "newapp").exists()

    def test_preview_still_rejects_an_invalid_draft(self, repo, source_exe):
        with pytest.raises(AuthoringError):
            CatalogueEditor(repo).add_application(
                draft(source_exe, id="existing"), preview=True
            )


class TestUpdateAndRemove:
    def test_update_version(self, repo):
        CatalogueEditor(repo).update_application("existing", version="2.0")
        assert Repository.load(repo.root).catalogue.get("existing").version == "2.0"

    def test_update_can_disable_an_application(self, repo):
        CatalogueEditor(repo).update_application("existing", enabled=False)
        assert Repository.load(repo.root).catalogue.get("existing").enabled is False

    def test_update_rejects_a_change_that_would_break_the_catalogue(self, repo):
        with pytest.raises(AuthoringError, match="would break the catalogue"):
            CatalogueEditor(repo).update_application("existing", version_scheme="vibes")

    def test_update_unknown_id(self, repo):
        with pytest.raises(AuthoringError, match="no application"):
            CatalogueEditor(repo).update_application("ghost", version="1")

    def test_remove_keeps_the_files_by_default(self, repo):
        installer = repo.root / "installers" / "windows" / "existing" / "setup.exe"
        assert installer.exists()

        CatalogueEditor(repo).remove_application("existing")
        assert Repository.load(repo.root).catalogue.ids == ()
        assert installer.exists()

    def test_remove_can_delete_the_files_when_asked(self, repo):
        removed = CatalogueEditor(repo).remove_application("existing", delete_files=True)
        assert "installers/windows/existing/setup.exe" in removed
        assert not (repo.root / "installers" / "windows" / "existing").exists()

    def test_remove_refuses_while_something_depends_on_it(self, tmp_path):
        write_repository(
            tmp_path,
            [app_entry("runtime"), app_entry("app", dependencies=["runtime"])],
        )
        with pytest.raises(AuthoringError, match="required by: app"):
            CatalogueEditor(Repository.load(tmp_path)).remove_application("runtime")

    def test_remove_unknown_id(self, repo):
        with pytest.raises(AuthoringError, match="no application"):
            CatalogueEditor(repo).remove_application("ghost")
