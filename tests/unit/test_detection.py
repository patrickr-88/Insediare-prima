"""Installed-application detection, per method."""

from __future__ import annotations

import plistlib

from tests.conftest import registry_entry
from usbinstaller.detection import (
    CommandVersionDetector,
    MacAppBundleDetector,
    MacPkgReceiptDetector,
    PathExistsDetector,
    WindowsPathDetector,
    WindowsRegistryDetector,
    detect_installed,
    registered_methods,
)
from usbinstaller.detection.base import DetectionContext
from usbinstaller.installers.process import CommandResult, RecordingRunner
from usbinstaller.models import OS, Application, DetectionSpec, PlatformPayload


def make_app(app_id="firefox", name="Mozilla Firefox", detection=None, **kwargs):
    return Application(
        id=app_id,
        name=name,
        detection=detection or DetectionSpec(),
        platforms={
            OS.WINDOWS: PlatformPayload(installer="i/w/a/s.exe", type="exe"),
            OS.MACOS: PlatformPayload(installer="i/m/a/a.pkg", type="pkg"),
        },
        **kwargs,
    )


class TestRegistry:
    def test_all_methods_are_registered(self):
        assert set(registered_methods()) >= {
            "none",
            "windows_registry",
            "windows_path",
            "macos_app_bundle",
            "macos_pkg_receipt",
            "path_exists",
            "command_version",
        }

    def test_none_detector_reports_not_installed(self, detection_context):
        result = detect_installed(make_app(), detection_context, OS.WINDOWS)
        assert result.installed is False
        assert result.method == "none"

    def test_unregistered_method_is_reported_not_raised(self, detection_context):
        app = make_app(detection=DetectionSpec(method="does-not-exist"))
        result = detect_installed(app, detection_context, OS.WINDOWS)
        assert result.installed is False
        assert "no detector registered" in result.detail

    def test_detector_exceptions_are_contained(self, detection_context):
        class Exploding:
            method = "explodes"

            def detect(self, app, spec, context):
                raise RuntimeError("boom")

        from usbinstaller.detection.base import register

        register(Exploding())
        app = make_app(detection=DetectionSpec(method="explodes"))
        result = detect_installed(app, detection_context, OS.WINDOWS)
        assert result.installed is False
        assert "boom" in result.detail


class TestWindowsRegistryDetector:
    def _context(self, entries, system):
        return DetectionContext(system=system, registry_reader=lambda: entries)

    def test_matches_by_display_name_regex(self, windows_system):
        context = self._context(
            [
                registry_entry("Microsoft Edge", "127.0"),
                registry_entry("Mozilla Firefox (x64 en-GB)", "141.0", "C:\\FF"),
            ],
            windows_system,
        )
        spec = DetectionSpec("windows_registry", {"display_name": "^Mozilla Firefox"})
        result = WindowsRegistryDetector().detect(make_app(), spec, context)
        assert result.installed is True
        assert result.version == "141.0"
        assert result.location == "C:\\FF"

    def test_falls_back_to_the_application_name(self, windows_system):
        context = self._context([registry_entry("Mozilla Firefox")], windows_system)
        result = WindowsRegistryDetector().detect(
            make_app(), DetectionSpec("windows_registry"), context
        )
        assert result.installed is True

    def test_no_match_reports_not_installed(self, windows_system):
        context = self._context([registry_entry("Something Else")], windows_system)
        result = WindowsRegistryDetector().detect(
            make_app(), DetectionSpec("windows_registry"), context
        )
        assert result.installed is False

    def test_product_code_match(self, windows_system):
        context = self._context(
            [registry_entry("Zoom", "6.1.6", key="{ZOOM-GUID}")], windows_system
        )
        spec = DetectionSpec("windows_registry", {"product_code": "{zoom-guid}"})
        assert WindowsRegistryDetector().detect(make_app(), spec, context).installed

    def test_publisher_disambiguates(self, windows_system):
        context = self._context(
            [
                registry_entry("Firefox", "1.0", publisher="Impostor Ltd"),
                registry_entry("Firefox", "141.0", publisher="Mozilla"),
            ],
            windows_system,
        )
        spec = DetectionSpec(
            "windows_registry", {"display_name": "Firefox", "publisher": "Mozilla"}
        )
        assert WindowsRegistryDetector().detect(make_app(), spec, context).version == "141.0"

    def test_invalid_regex_is_reported(self, windows_system):
        context = self._context([], windows_system)
        spec = DetectionSpec("windows_registry", {"display_name": "(unclosed"})
        result = WindowsRegistryDetector().detect(make_app(), spec, context)
        assert "invalid detection pattern" in result.detail

    def test_empty_registry_on_a_non_windows_host(self, windows_system):
        context = DetectionContext(system=windows_system, registry_reader=lambda: [])
        assert not WindowsRegistryDetector().detect(
            make_app(), DetectionSpec("windows_registry"), context
        ).installed


class TestPathDetectors:
    def test_windows_path_detector(self, windows_system):
        context = DetectionContext(
            system=windows_system,
            path_exists=lambda p: p == "C:\\Program Files\\7-Zip\\7z.exe",
        )
        spec = DetectionSpec(
            "windows_path", {"paths": ["C:\\nope.exe", "C:\\Program Files\\7-Zip\\7z.exe"]}
        )
        result = WindowsPathDetector().detect(make_app(), spec, context)
        assert result.installed and result.location.endswith("7z.exe")

    def test_path_exists_detector_expands_user_and_env(
        self, macos_system, monkeypatch, tmp_path
    ):
        monkeypatch.setenv("DEMO_HOME", str(tmp_path))
        marker = tmp_path / "marker"
        marker.write_text("x")
        context = DetectionContext(system=macos_system)
        spec = DetectionSpec("path_exists", {"paths": ["$DEMO_HOME/marker"]})
        assert PathExistsDetector().detect(make_app(), spec, context).installed

    def test_path_exists_without_configuration(self, macos_system):
        context = DetectionContext(system=macos_system)
        result = PathExistsDetector().detect(
            make_app(), DetectionSpec("path_exists"), context
        )
        assert not result.installed
        assert "not configured" in result.detail


class TestMacDetectors:
    def test_app_bundle_version_is_read_from_the_plist(self, macos_system, tmp_path):
        bundle = tmp_path / "Firefox.app"
        (bundle / "Contents").mkdir(parents=True)
        with open(bundle / "Contents" / "Info.plist", "wb") as handle:
            plistlib.dump({"CFBundleShortVersionString": "141.0"}, handle)

        context = DetectionContext(
            system=macos_system, application_dirs=(str(tmp_path),)
        )
        result = MacAppBundleDetector().detect(
            make_app(), DetectionSpec("macos_app_bundle", {"bundle": "Firefox.app"}), context
        )
        assert result.installed
        assert result.version == "141.0"

    def test_app_bundle_falls_back_to_cfbundleversion(self, macos_system):
        context = DetectionContext(
            system=macos_system,
            application_dirs=("/Applications",),
            path_exists=lambda p: p == "/Applications/Firefox.app",
            read_plist=lambda p: {"CFBundleVersion": "141"},
        )
        result = MacAppBundleDetector().detect(
            make_app(), DetectionSpec("macos_app_bundle", {"bundle": "Firefox.app"}), context
        )
        assert result.version == "141"

    def test_missing_bundle(self, macos_system):
        context = DetectionContext(system=macos_system, path_exists=lambda p: False)
        result = MacAppBundleDetector().detect(
            make_app(), DetectionSpec("macos_app_bundle"), context
        )
        assert not result.installed

    def test_unreadable_plist_does_not_raise(self, macos_system, tmp_path):
        bundle = tmp_path / "Firefox.app"
        (bundle / "Contents").mkdir(parents=True)
        (bundle / "Contents" / "Info.plist").write_bytes(b"not a plist")
        context = DetectionContext(system=macos_system, application_dirs=(str(tmp_path),))
        result = MacAppBundleDetector().detect(
            make_app(), DetectionSpec("macos_app_bundle", {"bundle": "Firefox.app"}), context
        )
        assert result.installed and result.version is None

    def test_pkg_receipt_parses_pkgutil_output(self, macos_system):
        runner = RecordingRunner(
            {
                "pkgutil": CommandResult(
                    ("pkgutil",),
                    exit_code=0,
                    stdout="package-id: us.zoom.pkg\nversion: 6.1.6\nlocation: /\n",
                )
            }
        )
        context = DetectionContext(system=macos_system, runner=runner)
        spec = DetectionSpec("macos_pkg_receipt", {"package_id": "us.zoom.pkg"})
        result = MacPkgReceiptDetector().detect(make_app(), spec, context)
        assert result.installed and result.version == "6.1.6"

    def test_pkg_receipt_absent(self, macos_system):
        runner = RecordingRunner(default_result=CommandResult(("pkgutil",), exit_code=1))
        context = DetectionContext(system=macos_system, runner=runner)
        spec = DetectionSpec("macos_pkg_receipt", {"package_id": "us.zoom.pkg"})
        assert not MacPkgReceiptDetector().detect(make_app(), spec, context).installed

    def test_pkg_receipt_without_package_id(self, macos_system):
        context = DetectionContext(system=macos_system)
        result = MacPkgReceiptDetector().detect(
            make_app(), DetectionSpec("macos_pkg_receipt"), context
        )
        assert "package_id" in result.detail


class TestCommandVersionDetector:
    def test_parses_a_version_from_output(self, macos_system, tmp_path):
        binary = tmp_path / "code"
        binary.write_text("#!/bin/sh")
        runner = RecordingRunner(
            default_result=CommandResult(("code",), exit_code=0, stdout="1.92.0\nx64\n")
        )
        context = DetectionContext(system=macos_system, runner=runner)
        spec = DetectionSpec("command_version", {"command": [str(binary), "--version"]})
        result = CommandVersionDetector().detect(make_app(), spec, context)
        assert result.installed and result.version == "1.92.0"

    def test_bare_command_names_are_refused(self, macos_system):
        context = DetectionContext(system=macos_system)
        spec = DetectionSpec("command_version", {"command": ["code", "--version"]})
        result = CommandVersionDetector().detect(make_app(), spec, context)
        assert not result.installed
        assert "absolute path" in result.detail

    def test_missing_binary(self, macos_system):
        context = DetectionContext(system=macos_system, path_exists=lambda p: False)
        spec = DetectionSpec("command_version", {"command": ["/usr/bin/nope"]})
        assert not CommandVersionDetector().detect(make_app(), spec, context).installed

    def test_non_zero_exit_is_not_installed(self, macos_system, tmp_path):
        binary = tmp_path / "tool"
        binary.write_text("x")
        runner = RecordingRunner(default_result=CommandResult(("tool",), exit_code=127))
        context = DetectionContext(system=macos_system, runner=runner)
        spec = DetectionSpec("command_version", {"command": [str(binary)]})
        assert not CommandVersionDetector().detect(make_app(), spec, context).installed

    def test_timeout_is_handled(self, macos_system, tmp_path):
        binary = tmp_path / "tool"
        binary.write_text("x")
        runner = RecordingRunner(
            default_result=CommandResult(("tool",), exit_code=-1, timed_out=True)
        )
        context = DetectionContext(system=macos_system, runner=runner)
        spec = DetectionSpec("command_version", {"command": [str(binary)]})
        result = CommandVersionDetector().detect(make_app(), spec, context)
        assert "timed out" in result.detail

    def test_unparseable_output_still_reports_installed(self, macos_system, tmp_path):
        binary = tmp_path / "tool"
        binary.write_text("x")
        runner = RecordingRunner(
            default_result=CommandResult(("tool",), exit_code=0, stdout="unknown build")
        )
        context = DetectionContext(system=macos_system, runner=runner)
        spec = DetectionSpec("command_version", {"command": [str(binary)]})
        result = CommandVersionDetector().detect(make_app(), spec, context)
        assert result.installed and result.version is None


class TestPlatformSpecificDetectionSelection:
    def test_platform_payload_detection_overrides_the_application_default(
        self, windows_system
    ):
        app = Application(
            id="a",
            name="A",
            detection=DetectionSpec("none"),
            platforms={
                OS.WINDOWS: PlatformPayload(
                    installer="i/w/a/s.exe",
                    type="exe",
                    detection=DetectionSpec("windows_registry", {"display_name": "A"}),
                )
            },
        )
        context = DetectionContext(
            system=windows_system, registry_reader=lambda: [registry_entry("A", "2.0")]
        )
        result = detect_installed(app, context, OS.WINDOWS)
        assert result.installed and result.version == "2.0"
