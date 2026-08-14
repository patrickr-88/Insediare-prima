"""Installation planning: detection, upgrade decisions, ordering, safety."""

from __future__ import annotations

from pathlib import Path

from tests.conftest import app_entry, registry_entry, write_repository
from usbinstaller.detection.base import DetectionContext
from usbinstaller.engine.planner import Planner, render_plan
from usbinstaller.installers.process import RecordingRunner
from usbinstaller.models import Action
from usbinstaller.repository import Repository
from usbinstaller.security.checksums import sha256_file


def make_planner(root: Path, system, registry=(), paths=(), **kwargs) -> Planner:
    repository = Repository.load(root)
    context = DetectionContext(
        system=system,
        runner=RecordingRunner(),
        registry_reader=lambda: list(registry),
        path_exists=lambda p: p in set(paths),
    )
    return Planner(repository=repository, system=system, detection=context, **kwargs)


REGISTRY_DETECT = {"method": "windows_registry", "display_name": "^Firefox"}


class TestBasicPlanning:
    def test_everything_is_planned_for_installation_on_a_clean_machine(
        self, tmp_path, windows_system
    ):
        write_repository(tmp_path, [app_entry("firefox"), app_entry("vlc")])
        plan = make_planner(tmp_path, windows_system).plan()
        assert [i.action for i in plan.items] == [Action.INSTALL, Action.INSTALL]
        assert all(i.reason == "not installed" for i in plan.items)

    def test_only_the_selection_is_planned(self, tmp_path, windows_system):
        write_repository(tmp_path, [app_entry("firefox"), app_entry("vlc")])
        plan = make_planner(tmp_path, windows_system).plan(["vlc"])
        assert [i.app_id for i in plan.items] == ["vlc"]

    def test_unknown_id_becomes_an_error_line_not_an_exception(
        self, tmp_path, windows_system
    ):
        write_repository(tmp_path, [app_entry("firefox")])
        plan = make_planner(tmp_path, windows_system).plan(["ghost"])
        assert plan.items[0].action is Action.ERROR
        assert "no application with id" in plan.items[0].reason

    def test_disabled_applications_are_skipped(self, tmp_path, windows_system):
        write_repository(tmp_path, [app_entry("firefox", enabled=False)])
        plan = make_planner(tmp_path, windows_system).plan(["firefox"])
        assert plan.items[0].action is Action.SKIP
        assert "disabled" in plan.items[0].reason

    def test_incompatible_os_is_skipped(self, tmp_path, macos_system):
        write_repository(tmp_path, [app_entry("npp", macos=False)])
        plan = make_planner(tmp_path, macos_system).plan(["npp"])
        assert plan.items[0].action is Action.SKIP
        assert "no macos installer" in plan.items[0].reason

    def test_incompatible_architecture_is_skipped(self, tmp_path, intel_macos_system):
        write_repository(
            tmp_path,
            [
                app_entry(
                    "armapp",
                    windows=False,
                    macos={
                        "installer": "installers/macos/armapp/a.pkg",
                        "type": "pkg",
                        "architectures": ["arm64"],
                    },
                )
            ],
        )
        plan = make_planner(tmp_path, intel_macos_system).plan(["armapp"])
        assert plan.items[0].action is Action.SKIP
        assert "not supported on x64" in plan.items[0].reason

    def test_available_hides_incompatible_software_entirely(
        self, tmp_path, intel_macos_system
    ):
        write_repository(
            tmp_path,
            [
                app_entry("winonly", macos=False),
                app_entry("both"),
            ],
        )
        assert [a.id for a in make_planner(tmp_path, intel_macos_system).available()] == [
            "both"
        ]


class TestInstallerProblems:
    def test_missing_installer_is_an_error(self, tmp_path, windows_system):
        write_repository(tmp_path, [app_entry("firefox")], create_installers=False)
        plan = make_planner(tmp_path, windows_system).plan(["firefox"])
        assert plan.items[0].action is Action.ERROR
        assert "installer missing" in plan.items[0].reason

    def test_path_traversal_is_an_error(self, tmp_path, windows_system):
        write_repository(
            tmp_path,
            [app_entry("evil", windows={"installer": "../evil.exe", "type": "exe"}, macos=False)],
            create_installers=False,
        )
        plan = make_planner(tmp_path, windows_system).plan(["evil"])
        assert plan.items[0].action is Action.ERROR
        assert "unsafe path" in plan.items[0].reason.lower() or "Refusing" in plan.items[0].reason

    def test_wrong_extension_for_the_declared_type_is_an_error(
        self, tmp_path, windows_system
    ):
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
        plan = make_planner(tmp_path, windows_system).plan(["a"])
        assert plan.items[0].action is Action.ERROR
        assert "not a valid file type" in plan.items[0].reason


class TestChecksumPolicy:
    def _repo_with_bad_checksum(self, tmp_path, **settings):
        write_repository(
            tmp_path,
            [app_entry("a", macos=False)],
            settings=settings or None,
            checksums={"installers/windows/a/setup.exe": {"sha256": "0" * 64}},
        )

    def test_mismatch_blocks_execution(self, tmp_path, windows_system):
        self._repo_with_bad_checksum(tmp_path)
        plan = make_planner(tmp_path, windows_system).plan(["a"])
        assert plan.items[0].action is Action.ERROR
        assert "MISMATCH" in plan.items[0].reason
        assert plan.items[0].checksum_ok is False

    def test_mismatch_can_be_downgraded_to_a_warning(self, tmp_path, windows_system):
        self._repo_with_bad_checksum(tmp_path, block_on_checksum_mismatch=False)
        plan = make_planner(tmp_path, windows_system).plan(["a"])
        assert plan.items[0].action is Action.INSTALL
        assert "checksum mismatch" in plan.items[0].reason

    def test_matching_checksum_passes(self, tmp_path, windows_system):
        write_repository(tmp_path, [app_entry("a", macos=False)])
        digest = sha256_file(tmp_path / "installers/windows/a/setup.exe")
        write_repository(
            tmp_path,
            [app_entry("a", macos=False)],
            checksums={"installers/windows/a/setup.exe": {"sha256": digest}},
        )
        plan = make_planner(tmp_path, windows_system).plan(["a"])
        assert plan.items[0].checksum_ok is True
        assert plan.items[0].action is Action.INSTALL

    def test_missing_checksum_blocks_only_in_strict_mode(self, tmp_path, windows_system):
        write_repository(tmp_path, [app_entry("a", macos=False)])
        assert make_planner(tmp_path, windows_system).plan(["a"]).items[0].action is (
            Action.INSTALL
        )

        write_repository(
            tmp_path, [app_entry("a", macos=False)], settings={"strict_checksums": True}
        )
        item = make_planner(tmp_path, windows_system).plan(["a"]).items[0]
        assert item.action is Action.ERROR
        assert "strict checksum mode" in item.reason

    def test_declared_sha256_is_honoured(self, tmp_path, windows_system):
        write_repository(tmp_path, [app_entry("a", macos=False)])
        digest = sha256_file(tmp_path / "installers/windows/a/setup.exe")
        write_repository(
            tmp_path,
            [
                app_entry(
                    "a",
                    macos=False,
                    windows={
                        "installer": "installers/windows/a/setup.exe",
                        "type": "exe",
                        "sha256": digest,
                    },
                )
            ],
        )
        assert make_planner(tmp_path, windows_system).plan(["a"]).items[0].checksum_ok


class TestUpgradeDecisions:
    def _plan(self, tmp_path, windows_system, usb_version, installed_version, **kwargs):
        write_repository(
            tmp_path,
            [
                app_entry(
                    "firefox",
                    name="Firefox",
                    version=usb_version,
                    macos=False,
                    version_scheme="numeric",
                    detection=REGISTRY_DETECT,
                )
            ],
        )
        registry = (
            [registry_entry("Firefox", installed_version)] if installed_version else []
        )
        planner = make_planner(tmp_path, windows_system, registry=registry)
        return planner.plan(["firefox"], **kwargs).items[0]

    def test_older_installed_version_is_an_upgrade(self, tmp_path, windows_system):
        item = self._plan(tmp_path, windows_system, "141.0", "140.0")
        assert item.action is Action.UPGRADE
        assert "140.0 → USB 141.0" in item.reason

    def test_same_version_is_skipped(self, tmp_path, windows_system):
        item = self._plan(tmp_path, windows_system, "141.0", "141.0")
        assert item.action is Action.SKIP
        assert "already installed" in item.reason

    def test_newer_installed_version_is_never_downgraded(self, tmp_path, windows_system):
        item = self._plan(tmp_path, windows_system, "141.0", "142.0")
        assert item.action is Action.SKIP
        assert "newer version already installed" in item.reason

    def test_force_reinstalls_the_same_version(self, tmp_path, windows_system):
        item = self._plan(tmp_path, windows_system, "141.0", "141.0", force=True)
        assert item.action is Action.INSTALL
        assert "reinstall forced" in item.reason

    def test_force_downgrade_is_explicit_about_what_it_is_doing(
        self, tmp_path, windows_system
    ):
        item = self._plan(tmp_path, windows_system, "141.0", "142.0", force=True)
        assert item.action is Action.INSTALL
        assert "DOWNGRADE forced" in item.reason

    def test_uncomparable_versions_are_skipped_with_an_explanation(
        self, tmp_path, windows_system
    ):
        item = self._plan(tmp_path, windows_system, "141.0", "Gold Master")
        assert item.action is Action.SKIP
        assert "cannot be compared" in item.reason

    def test_nothing_installed_means_install(self, tmp_path, windows_system):
        item = self._plan(tmp_path, windows_system, "141.0", None)
        assert item.action is Action.INSTALL
        assert item.reason == "not installed"

    def test_installed_but_version_unknown_is_not_overwritten(
        self, tmp_path, windows_system
    ):
        """A blank DisplayVersion means "installed, version unknown" — not "absent"."""
        write_repository(
            tmp_path,
            [
                app_entry(
                    "firefox",
                    name="Firefox",
                    version="141.0",
                    macos=False,
                    detection=REGISTRY_DETECT,
                )
            ],
        )
        planner = make_planner(
            tmp_path, windows_system, registry=[registry_entry("Firefox", "")]
        )
        item = planner.plan(["firefox"]).items[0]
        assert item.action is Action.SKIP
        assert "cannot be compared" in item.reason


class TestPrerequisites:
    def _plan_with_prerequisite(self, tmp_path, system, prerequisite, **kwargs):
        write_repository(
            tmp_path, [app_entry("a", macos=False, prerequisites=[prerequisite])]
        )
        return make_planner(tmp_path, system, **kwargs).plan(["a"]).items[0]

    def test_admin_prerequisite_blocks_without_privileges(self, tmp_path):
        from tests.conftest import windows_probe
        from usbinstaller.sysdetect.system import detect_system

        system = detect_system(windows_probe(admin=False))
        item = self._plan_with_prerequisite(tmp_path, system, {"type": "admin"})
        assert item.action is Action.ERROR
        assert "administrator privileges" in item.reason

    def test_min_free_disk(self, tmp_path):
        from tests.conftest import windows_probe
        from usbinstaller.sysdetect.system import detect_system

        system = detect_system(windows_probe(free=100 * 1024 * 1024))
        item = self._plan_with_prerequisite(
            tmp_path, system, {"type": "min_free_disk_mb", "value": 1024}
        )
        assert item.action is Action.ERROR
        assert "requires 1024 MB free" in item.reason

    def test_min_os_version(self, tmp_path, macos_system):
        from tests.conftest import macos_probe
        from usbinstaller.sysdetect.system import detect_system

        old_mac = detect_system(macos_probe(mac_version="12.0"))
        write_repository(
            tmp_path,
            [
                app_entry(
                    "a",
                    windows=False,
                    prerequisites=[{"type": "min_os_version", "value": "13.0"}],
                )
            ],
        )
        item = make_planner(tmp_path, old_mac).plan(["a"]).items[0]
        assert item.action is Action.ERROR
        assert "13.0 or newer" in item.reason

    def test_satisfied_prerequisite_allows_installation(self, tmp_path, windows_system):
        item = self._plan_with_prerequisite(
            tmp_path, windows_system, {"type": "min_free_disk_mb", "value": 1}
        )
        assert item.action is Action.INSTALL

    def test_custom_message_replaces_the_default(self, tmp_path):
        from tests.conftest import windows_probe
        from usbinstaller.sysdetect.system import detect_system

        system = detect_system(windows_probe(admin=False))
        item = self._plan_with_prerequisite(
            tmp_path, system, {"type": "admin", "message": "Ask the site admin."}
        )
        assert item.reason == "Ask the site admin."

    def test_path_prerequisite(self, tmp_path, windows_system):
        write_repository(
            tmp_path,
            [
                app_entry(
                    "a",
                    macos=False,
                    prerequisites=[{"type": "path_exists", "value": "C:\\Runtime"}],
                )
            ],
        )
        item = make_planner(tmp_path, windows_system, paths=["C:\\Runtime"]).plan(["a"]).items[0]
        assert item.action is Action.INSTALL

        item = make_planner(tmp_path, windows_system).plan(["a"]).items[0]
        assert item.action is Action.ERROR


class TestDependencies:
    def test_dependencies_are_installed_first(self, tmp_path, windows_system):
        write_repository(
            tmp_path,
            [app_entry("app", dependencies=["runtime"]), app_entry("runtime")],
        )
        plan = make_planner(tmp_path, windows_system).plan(["app"])
        assert [i.app_id for i in plan.items] == ["runtime", "app"]
        assert "dependency" in plan.items[0].reason

    def test_dependencies_can_be_left_out(self, tmp_path, windows_system):
        write_repository(
            tmp_path,
            [app_entry("app", dependencies=["runtime"]), app_entry("runtime")],
        )
        plan = make_planner(tmp_path, windows_system).plan(
            ["app"], include_dependencies=False
        )
        assert [i.app_id for i in plan.items] == ["app"]

    def test_circular_dependencies_produce_error_lines(self, tmp_path, windows_system):
        write_repository(
            tmp_path,
            [app_entry("a", dependencies=["b"]), app_entry("b", dependencies=["a"])],
        )
        plan = make_planner(tmp_path, windows_system).plan(["a"])
        assert all(i.action is Action.ERROR for i in plan.items)
        assert "circular" in plan.items[0].reason


class TestPlanRendering:
    def test_render_includes_every_section(self, tmp_path, windows_system):
        write_repository(
            tmp_path,
            [
                app_entry("firefox", name="Firefox", version="141.0"),
                app_entry("ghost", macos=False),
            ],
            create_installers=False,
        )
        (tmp_path / "installers/windows/firefox").mkdir(parents=True)
        (tmp_path / "installers/windows/firefox/setup.exe").write_bytes(b"x")
        (tmp_path / "installers/macos/firefox").mkdir(parents=True)
        (tmp_path / "installers/macos/firefox/app.pkg").write_bytes(b"x")

        plan = make_planner(tmp_path, windows_system).plan(dry_run=True)
        text = render_plan(plan)
        assert "INSTALLATION PLAN" in text
        assert "[INSTALL] Firefox 141.0" in text
        assert "[ERROR]" in text
        assert "DRY RUN" in text
        assert "Administrator privileges are required" in text

    def test_plan_reports_admin_requirement(self, tmp_path, windows_system):
        write_repository(
            tmp_path,
            [
                app_entry(
                    "a",
                    macos=False,
                    windows={
                        "installer": "installers/windows/a/setup.exe",
                        "type": "exe",
                        "requires_admin": True,
                    },
                )
            ],
        )
        plan = make_planner(tmp_path, windows_system).plan(["a"])
        assert plan.requires_admin is True
