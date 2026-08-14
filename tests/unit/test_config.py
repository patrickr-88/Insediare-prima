"""Configuration parsing, settings and validation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.conftest import app_entry, write_repository
from usbinstaller.config.catalogue import (
    load_catalogue,
    parse_application,
    parse_catalogue,
)
from usbinstaller.config.settings import Settings
from usbinstaller.config.validator import validate_repository
from usbinstaller.errors import ConfigParseError, ConfigSchemaError
from usbinstaller.models import OS, Arch
from usbinstaller.repository import Repository


class TestValidConfiguration:
    def test_minimal_application_parses(self):
        app = parse_application(
            {
                "id": "firefox",
                "name": "Mozilla Firefox",
                "windows": {"installer": "installers/windows/f/s.exe", "type": "exe"},
            },
            "applications[0]",
        )
        assert app.id == "firefox"
        assert app.enabled is True
        assert app.platforms[OS.WINDOWS].type == "exe"
        assert app.category == "Uncategorised"

    def test_full_application_parses_every_field(self):
        app = parse_application(
            {
                "id": "vscode",
                "name": "Visual Studio Code",
                "version": "1.92.0",
                "description": "Editor",
                "category": "Development",
                "enabled": True,
                "selected_by_default": False,
                "version_scheme": "semver",
                "version_pattern": r"(\d+\.\d+\.\d+)",
                "dependencies": ["dotnet"],
                "prerequisites": [{"type": "min_free_disk_mb", "value": 500}],
                "detection": {"method": "windows_registry", "display_name": "^Code"},
                "windows": {
                    "installer": "installers/windows/vscode/setup.exe",
                    "type": "exe",
                    "arguments": ["/VERYSILENT"],
                    "architectures": ["x64", "arm64"],
                    "requires_admin": True,
                    "sha256": "ab" * 32,
                    "timeout_seconds": 600,
                    "success_exit_codes": [0, 3010],
                    "post_install": [{"type": "message", "message": "done"}],
                },
                "macos": {
                    "installer": "installers/macos/vscode/VSCode.zip",
                    "type": "app",
                    "app_bundle": "Visual Studio Code.app",
                },
            },
            "applications[0]",
        )
        payload = app.platforms[OS.WINDOWS]
        assert app.version_scheme == "semver"
        assert app.dependencies == ("dotnet",)
        assert app.prerequisites[0].value == 500
        assert payload.architectures == (Arch.X64, Arch.ARM64)
        assert payload.success_exit_codes == (0, 3010)
        assert payload.sha256 == "ab" * 32
        assert payload.post_install[0].message == "done"
        assert app.platforms[OS.MACOS].app_bundle == "Visual Studio Code.app"
        assert app.selected_by_default is False

    def test_string_arguments_are_tokenised(self):
        app = parse_application(
            {
                "id": "a",
                "windows": {
                    "installer": "i/w/a/s.exe",
                    "type": "exe",
                    "arguments": '/S /D="C:\\Program Files\\App"',
                },
            },
            "app",
        )
        assert app.platforms[OS.WINDOWS].arguments == ["/S", "/D=C:\\Program Files\\App"]

    def test_architecture_aliases_are_accepted(self):
        app = parse_application(
            {
                "id": "a",
                "macos": {
                    "installer": "i/m/a/a.pkg",
                    "type": "pkg",
                    "architectures": ["x86_64", "apple_silicon"],
                },
            },
            "app",
        )
        assert app.platforms[OS.MACOS].architectures == (Arch.X64, Arch.ARM64)


class TestInvalidConfiguration:
    def test_invalid_json_is_reported_with_position(self, tmp_path: Path):
        path = tmp_path / "applications.json"
        path.write_text('{"applications": [ }')
        with pytest.raises(ConfigParseError) as exc:
            load_catalogue(path)
        assert "line" in str(exc.value)

    def test_missing_file(self, tmp_path: Path):
        with pytest.raises(ConfigParseError):
            load_catalogue(tmp_path / "nope.json")

    def test_top_level_must_be_an_object(self):
        with pytest.raises(ConfigSchemaError):
            parse_catalogue([])

    def test_applications_must_be_a_list(self):
        with pytest.raises(ConfigSchemaError):
            parse_catalogue({"applications": {}})

    def test_duplicate_ids_are_rejected(self):
        with pytest.raises(ConfigSchemaError) as exc:
            parse_catalogue(
                {"applications": [app_entry("dup"), app_entry("dup")]}
            )
        assert "duplicate application id" in str(exc.value)

    def test_missing_id(self):
        with pytest.raises(ConfigSchemaError, match="id"):
            parse_application({"windows": {"installer": "a.exe", "type": "exe"}}, "app")

    def test_id_character_set_is_restricted(self):
        with pytest.raises(ConfigSchemaError):
            parse_application(
                {"id": "bad id!", "windows": {"installer": "a.exe", "type": "exe"}},
                "app",
            )

    def test_application_without_any_platform_is_rejected(self):
        with pytest.raises(ConfigSchemaError, match="platform"):
            parse_application({"id": "a", "name": "A"}, "app")

    def test_unsupported_installer_type(self):
        with pytest.raises(ConfigSchemaError, match="unsupported installer type"):
            parse_application(
                {"id": "a", "windows": {"installer": "a.deb", "type": "deb"}}, "app"
            )

    def test_macos_type_on_windows_section_is_rejected(self):
        with pytest.raises(ConfigSchemaError):
            parse_application(
                {"id": "a", "windows": {"installer": "a.dmg", "type": "dmg"}}, "app"
            )

    def test_missing_installer_path(self):
        with pytest.raises(ConfigSchemaError, match="installer"):
            parse_application({"id": "a", "windows": {"type": "exe"}}, "app")

    def test_unsupported_architecture(self):
        with pytest.raises(ConfigSchemaError, match="architecture"):
            parse_application(
                {
                    "id": "a",
                    "windows": {
                        "installer": "a.exe",
                        "type": "exe",
                        "architectures": ["powerpc"],
                    },
                },
                "app",
            )

    def test_unknown_detection_method(self):
        with pytest.raises(ConfigSchemaError, match="detection method"):
            parse_application(
                {
                    "id": "a",
                    "detection": {"method": "read_the_tea_leaves"},
                    "windows": {"installer": "a.exe", "type": "exe"},
                },
                "app",
            )

    def test_unknown_prerequisite_type(self):
        with pytest.raises(ConfigSchemaError, match="prerequisite"):
            parse_application(
                {
                    "id": "a",
                    "prerequisites": [{"type": "phase_of_the_moon"}],
                    "windows": {"installer": "a.exe", "type": "exe"},
                },
                "app",
            )

    def test_invalid_version_scheme(self):
        with pytest.raises(ConfigSchemaError, match="scheme"):
            parse_application(
                {
                    "id": "a",
                    "version_scheme": "vibes",
                    "windows": {"installer": "a.exe", "type": "exe"},
                },
                "app",
            )

    def test_invalid_version_pattern(self):
        with pytest.raises(ConfigSchemaError, match="regular expression"):
            parse_application(
                {
                    "id": "a",
                    "version_pattern": "(unclosed",
                    "windows": {"installer": "a.exe", "type": "exe"},
                },
                "app",
            )

    def test_invalid_sha256(self):
        with pytest.raises(ConfigSchemaError, match="sha256"):
            parse_application(
                {
                    "id": "a",
                    "windows": {"installer": "a.exe", "type": "exe", "sha256": "abc"},
                },
                "app",
            )

    def test_self_dependency_is_rejected(self):
        with pytest.raises(ConfigSchemaError, match="depends on itself"):
            parse_application(
                {
                    "id": "a",
                    "dependencies": ["a"],
                    "windows": {"installer": "a.exe", "type": "exe"},
                },
                "app",
            )

    def test_newer_schema_version_is_refused(self):
        with pytest.raises(ConfigSchemaError, match="newer than this build"):
            parse_catalogue({"schema_version": 99, "applications": []})

    def test_non_integer_timeout(self):
        with pytest.raises(ConfigSchemaError, match="timeout"):
            parse_application(
                {
                    "id": "a",
                    "windows": {
                        "installer": "a.exe",
                        "type": "exe",
                        "timeout_seconds": -5,
                    },
                },
                "app",
            )


class TestSettings:
    def test_defaults_when_file_absent(self, tmp_path: Path):
        settings = Settings.load(tmp_path / "settings.json")
        assert settings.strict_checksums is False
        assert settings.continue_on_failure is True

    def test_values_are_read_and_typechecked(self, tmp_path: Path):
        path = tmp_path / "settings.json"
        path.write_text(json.dumps({"strict_checksums": True, "log_retention": 5}))
        settings = Settings.load(path)
        assert settings.strict_checksums is True
        assert settings.log_retention == 5

    @pytest.mark.parametrize(
        "payload",
        [
            {"strict_checksums": "yes"},
            {"log_retention": "many"},
            {"log_retention": -1},
            {"repository_name": 42},
            {"macos_application_dirs": "/Applications"},
        ],
    )
    def test_wrong_types_are_rejected(self, payload):
        with pytest.raises(ConfigSchemaError):
            Settings.from_dict(payload)

    def test_unknown_keys_are_ignored_but_reportable(self):
        settings = Settings.from_dict({"nonsense": True})
        assert settings.unknown_keys({"nonsense": True}) == ["nonsense"]

    def test_invalid_json_raises(self, tmp_path: Path):
        path = tmp_path / "settings.json"
        path.write_text("{oops")
        with pytest.raises(ConfigParseError):
            Settings.load(path)


class TestCatalogueQueries:
    def test_platform_filtering_hides_incompatible_software(self, tmp_path: Path):
        write_repository(
            tmp_path,
            [
                app_entry("win-only", macos=False),
                app_entry("mac-only", windows=False),
                app_entry(
                    "arm-only",
                    macos={
                        "installer": "installers/macos/arm/a.pkg",
                        "type": "pkg",
                        "architectures": ["arm64"],
                    },
                    windows=False,
                ),
                app_entry("disabled", enabled=False),
            ],
        )
        catalogue = Repository.load(tmp_path).catalogue

        windows = {a.id for a in catalogue.for_platform(OS.WINDOWS, Arch.X64)}
        assert windows == {"win-only"}

        intel_mac = {a.id for a in catalogue.for_platform(OS.MACOS, Arch.X64)}
        assert intel_mac == {"mac-only"}

        arm_mac = {a.id for a in catalogue.for_platform(OS.MACOS, Arch.ARM64)}
        assert arm_mac == {"mac-only", "arm-only"}

    def test_select_rejects_unknown_ids(self, simple_repo):
        with pytest.raises(ConfigSchemaError, match="unknown application"):
            simple_repo.catalogue.select(["firefox", "ghost"])

    def test_categories_are_sorted(self, simple_repo):
        assert simple_repo.catalogue.categories() == ("Testing",)


class TestValidator:
    def test_clean_repository_passes(self, tmp_path: Path):
        write_repository(tmp_path, [app_entry("firefox")])
        report = validate_repository(tmp_path)
        assert report.ok
        assert "1 application(s) found" in report.render()

    def test_missing_installer_is_an_error(self, tmp_path: Path):
        write_repository(tmp_path, [app_entry("firefox")], create_installers=False)
        report = validate_repository(tmp_path)
        assert not report.ok
        assert any("file not found" in f.message for f in report.errors)

    def test_wrong_extension_for_type_is_an_error(self, tmp_path: Path):
        write_repository(
            tmp_path,
            [
                app_entry(
                    "a",
                    windows={"installer": "installers/windows/a/setup.msi", "type": "exe"},
                    macos=False,
                )
            ],
        )
        report = validate_repository(tmp_path)
        assert any("valid extension" in f.message for f in report.errors)

    def test_empty_installer_is_an_error(self, tmp_path: Path):
        write_repository(tmp_path, [app_entry("a")], create_installers=False)
        installer = tmp_path / "installers" / "windows" / "a" / "setup.exe"
        installer.parent.mkdir(parents=True)
        installer.write_bytes(b"")
        (tmp_path / "installers" / "macos" / "a").mkdir(parents=True)
        (tmp_path / "installers" / "macos" / "a" / "app.pkg").write_bytes(b"x")
        report = validate_repository(tmp_path)
        assert any("is empty" in f.message for f in report.errors)

    def test_unknown_dependency_is_an_error(self, tmp_path: Path):
        write_repository(tmp_path, [app_entry("a", dependencies=["ghost"])])
        report = validate_repository(tmp_path)
        assert any("unknown application" in f.message for f in report.errors)

    def test_circular_dependency_is_an_error(self, tmp_path: Path):
        write_repository(
            tmp_path,
            [
                app_entry("a", dependencies=["b"]),
                app_entry("b", dependencies=["a"]),
            ],
        )
        report = validate_repository(tmp_path)
        assert any("circular dependency" in f.message for f in report.errors)

    def test_missing_checksum_is_a_warning_by_default(self, tmp_path: Path):
        write_repository(tmp_path, [app_entry("a")])
        report = validate_repository(tmp_path)
        assert report.ok
        assert any("no checksum" in f.message for f in report.warnings)

    def test_missing_checksum_is_an_error_in_strict_mode(self, tmp_path: Path):
        write_repository(tmp_path, [app_entry("a")], settings={"strict_checksums": True})
        report = validate_repository(tmp_path)
        assert not report.ok

    def test_checksum_mismatch_detected_when_verifying(self, tmp_path: Path):
        write_repository(
            tmp_path,
            [app_entry("a", macos=False)],
            checksums={"installers/windows/a/setup.exe": {"sha256": "0" * 64}},
        )
        report = validate_repository(tmp_path, check_checksums=True)
        assert any("MISMATCH" in f.message for f in report.errors)

    def test_declared_and_stored_checksum_disagreement_is_an_error(self, tmp_path: Path):
        write_repository(
            tmp_path,
            [
                app_entry(
                    "a",
                    windows={
                        "installer": "installers/windows/a/setup.exe",
                        "type": "exe",
                        "sha256": "ab" * 32,
                    },
                    macos=False,
                )
            ],
            checksums={"installers/windows/a/setup.exe": {"sha256": "cd" * 32}},
        )
        report = validate_repository(tmp_path)
        assert any("disagrees" in f.message for f in report.errors)

    def test_path_traversal_in_configuration_is_an_error(self, tmp_path: Path):
        write_repository(
            tmp_path,
            [
                app_entry(
                    "a",
                    windows={"installer": "../../../evil.exe", "type": "exe"},
                    macos=False,
                )
            ],
            create_installers=False,
        )
        report = validate_repository(tmp_path)
        assert any("unsafe path" in f.message for f in report.errors)

    def test_invalid_version_for_scheme_is_an_error(self, tmp_path: Path):
        write_repository(
            tmp_path, [app_entry("a", version="1.0", version_scheme="semver")]
        )
        report = validate_repository(tmp_path)
        assert any("not valid for scheme" in f.message for f in report.errors)

    def test_missing_version_is_a_warning(self, tmp_path: Path):
        write_repository(tmp_path, [app_entry("a", version="")])
        report = validate_repository(tmp_path)
        assert any("no version declared" in f.message for f in report.warnings)

    def test_unknown_settings_key_is_a_warning(self, tmp_path: Path):
        write_repository(tmp_path, [app_entry("a")], settings={"typo_here": 1})
        report = validate_repository(tmp_path)
        assert any("unknown setting" in f.message for f in report.warnings)

    def test_missing_config_file(self, tmp_path: Path):
        report = validate_repository(tmp_path)
        assert not report.ok
        assert "missing configuration file" in report.errors[0].message

    def test_invalid_json_is_reported_not_raised(self, tmp_path: Path):
        (tmp_path / "config").mkdir()
        (tmp_path / "config" / "applications.json").write_text("{ nope")
        report = validate_repository(tmp_path)
        assert not report.ok
        assert "not valid JSON" in report.errors[0].message

    def test_missing_post_install_script_is_an_error(self, tmp_path: Path):
        write_repository(
            tmp_path,
            [
                app_entry(
                    "a",
                    macos=False,
                    windows={
                        "installer": "installers/windows/a/setup.exe",
                        "type": "exe",
                        "post_install": [
                            {"type": "script", "script": "scripts/windows/after.ps1"}
                        ],
                    },
                )
            ],
        )
        report = validate_repository(tmp_path)
        assert any("post-install script not found" in f.message for f in report.errors)

    def test_report_serialises_to_json(self, tmp_path: Path):
        write_repository(tmp_path, [app_entry("a")])
        payload = validate_repository(tmp_path).to_dict()
        assert payload["ok"] is True
        assert payload["counts"]["error"] == 0
