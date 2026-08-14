"""macOS installer types: PKG, DMG, ``.app`` bundles and shell scripts."""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from ..errors import InstallerExecutionError
from ..models import OS, Application, PlatformPayload
from .base import InstallContext, Installer, InstallOutcome, register

HDIUTIL = "/usr/bin/hdiutil"
INSTALLER_TOOL = "/usr/sbin/installer"
DITTO = "/usr/bin/ditto"
XATTR = "/usr/bin/xattr"
DEFAULT_APPLICATIONS_DIR = "/Applications"


@register
class MacPkgInstaller(Installer):
    """Apple installer packages, executed with ``/usr/sbin/installer``.

    Always targets ``/`` (a system-wide install). Gatekeeper still evaluates
    the package signature; nothing here weakens that.
    """

    type = "pkg"
    os = OS.MACOS

    def build_command(
        self, installer_path: Path, payload: PlatformPayload, context: InstallContext
    ) -> list[str]:
        target = payload.extra.get("target", "/")
        return [
            INSTALLER_TOOL,
            "-pkg",
            str(installer_path),
            "-target",
            str(target),
            *payload.arguments,
        ]


@register
class MacShellInstaller(Installer):
    """A vendor-supplied ``.sh``/``.command`` installation script.

    Run through ``/bin/sh <script>`` rather than executing the file directly,
    so a missing execute bit on a FAT-formatted USB drive is not a failure —
    and so the script does not need to be marked executable on the drive.
    """

    type = "shell"
    os = OS.MACOS
    requires_admin_by_default = False

    def build_command(
        self, installer_path: Path, payload: PlatformPayload, context: InstallContext
    ) -> list[str]:
        return ["/bin/sh", str(installer_path), *payload.arguments]


@register
class MacAppInstaller(Installer):
    """Install a ``.app`` bundle (or a ``.zip`` containing one) by copying it.

    ``ditto`` is used instead of ``cp`` because it preserves resource forks,
    extended attributes and code signatures — a bundle copied with plain ``cp``
    can fail Gatekeeper validation on first launch.
    """

    type = "app"
    os = OS.MACOS
    requires_admin_by_default = True

    def install(
        self,
        app: Application,
        payload: PlatformPayload,
        installer_path: Path,
        context: InstallContext,
    ) -> InstallOutcome:
        destination_dir = Path(
            payload.extra.get("destination", DEFAULT_APPLICATIONS_DIR)
        )
        commands: list[tuple[str, ...]] = []

        if installer_path.suffix.lower() == ".zip":
            if context.dry_run:
                return InstallOutcome(
                    success=True,
                    message="dry run: archive would be expanded and copied",
                )
            with tempfile.TemporaryDirectory(prefix="usbinstaller-") as tmp:
                unzip = context.runner.run(
                    [DITTO, "-x", "-k", str(installer_path), tmp],
                    timeout=context.timeout_for(payload),
                )
                commands.append(unzip.command)
                if unzip.exit_code != 0:
                    return InstallOutcome(
                        success=False,
                        exit_code=unzip.exit_code,
                        message="failed to expand archive",
                        stdout=unzip.stdout,
                        stderr=unzip.stderr,
                        commands=tuple(commands),
                    )
                bundle = _find_bundle(Path(tmp), payload.app_bundle)
                if bundle is None:
                    return InstallOutcome(
                        success=False,
                        message="no .app bundle found inside the archive",
                        commands=tuple(commands),
                    )
                return _copy_bundle(bundle, destination_dir, payload, context, commands)

        if context.dry_run:
            return InstallOutcome(
                success=True, message=f"dry run: would copy bundle to {destination_dir}"
            )
        return _copy_bundle(installer_path, destination_dir, payload, context, commands)


@register
class MacDmgInstaller(Installer):
    """Disk images: mount read-only, install the payload, always detach.

    The image is attached with ``-nobrowse -readonly`` and, when the vendor
    embeds a licence agreement, ``-quiet`` plus a piped ``Y`` is *not* used —
    an EULA that needs accepting is surfaced as a failure rather than
    auto-accepted on the technician's behalf.
    """

    type = "dmg"
    os = OS.MACOS

    def install(
        self,
        app: Application,
        payload: PlatformPayload,
        installer_path: Path,
        context: InstallContext,
    ) -> InstallOutcome:
        destination_dir = Path(
            payload.extra.get("destination", DEFAULT_APPLICATIONS_DIR)
        )
        if context.dry_run:
            return InstallOutcome(
                success=True,
                message=(
                    f"dry run: would mount {installer_path.name} and install its "
                    f"payload into {destination_dir}"
                ),
            )

        workspace = str(context.workspace or tempfile.gettempdir())
        mountpoint = Path(
            tempfile.mkdtemp(prefix="usbinstaller-dmg-", dir=workspace)
        )
        commands: list[tuple[str, ...]] = []
        attach = context.runner.run(
            [
                HDIUTIL,
                "attach",
                str(installer_path),
                "-nobrowse",
                "-readonly",
                "-noverify",
                "-mountpoint",
                str(mountpoint),
            ],
            timeout=context.timeout_for(payload),
        )
        commands.append(attach.command)
        if attach.exit_code != 0:
            _rmdir(mountpoint)
            return InstallOutcome(
                success=False,
                exit_code=attach.exit_code,
                message="failed to mount disk image",
                stdout=attach.stdout,
                stderr=attach.stderr,
                commands=tuple(commands),
            )

        try:
            bundle = _find_bundle(mountpoint, payload.app_bundle)
            if bundle is not None:
                return _copy_bundle(
                    bundle, destination_dir, payload, context, commands
                )

            package = _find_package(mountpoint)
            if package is not None:
                context.note(f"Disk image contains a package: {package.name}")
                result = context.runner.run(
                    [INSTALLER_TOOL, "-pkg", str(package), "-target", "/"],
                    timeout=context.timeout_for(payload),
                )
                commands.append(result.command)
                outcome = InstallOutcome.from_result(result, payload)
                return InstallOutcome(
                    success=outcome.success,
                    exit_code=outcome.exit_code,
                    message=outcome.message,
                    stdout=outcome.stdout,
                    stderr=outcome.stderr,
                    timed_out=outcome.timed_out,
                    commands=tuple(commands),
                )

            return InstallOutcome(
                success=False,
                message=(
                    "disk image contains neither an .app bundle nor a .pkg; set "
                    "'app_bundle' in the configuration if the payload has an "
                    "unusual name"
                ),
                commands=tuple(commands),
            )
        finally:
            detach = context.runner.run(
                [HDIUTIL, "detach", str(mountpoint), "-force"], timeout=120
            )
            commands.append(detach.command)
            _rmdir(mountpoint)


def _find_bundle(root: Path, preferred: str | None) -> Path | None:
    """Locate the ``.app`` bundle to install inside a mounted image or archive."""
    if preferred:
        candidate = root / preferred
        return candidate if candidate.exists() else None
    try:
        bundles = sorted(p for p in root.iterdir() if p.name.endswith(".app"))
    except OSError:
        return None
    if len(bundles) == 1:
        return bundles[0]
    return bundles[0] if bundles else None


def _find_package(root: Path) -> Path | None:
    try:
        packages = sorted(
            p for p in root.iterdir() if p.suffix.lower() in (".pkg", ".mpkg")
        )
    except OSError:
        return None
    return packages[0] if packages else None


def _copy_bundle(
    bundle: Path,
    destination_dir: Path,
    payload: PlatformPayload,
    context: InstallContext,
    commands: list[tuple[str, ...]],
) -> InstallOutcome:
    """Copy an application bundle into place with ``ditto``.

    Only the application bundle itself is replaced; no user data outside the
    bundle is touched, and nothing is deleted — ``ditto`` overwrites the target
    bundle in place.
    """
    destination = destination_dir / bundle.name
    if not destination_dir.exists():
        raise InstallerExecutionError(
            f"destination directory does not exist: {destination_dir}"
        )

    result = context.runner.run(
        [DITTO, str(bundle), str(destination)],
        timeout=context.timeout_for(payload),
    )
    commands.append(result.command)
    if result.exit_code != 0:
        return InstallOutcome(
            success=False,
            exit_code=result.exit_code,
            message=f"failed to copy {bundle.name} into {destination_dir}",
            stdout=result.stdout,
            stderr=result.stderr,
            commands=tuple(commands),
        )

    # Remove the quarantine flag from the copy we just made, so the technician
    # is not asked to approve an application they deliberately deployed. This
    # affects this bundle only; Gatekeeper itself remains enabled.
    quarantine = context.runner.run(
        [XATTR, "-d", "-r", "com.apple.quarantine", str(destination)], timeout=120
    )
    commands.append(quarantine.command)

    return InstallOutcome(
        success=True,
        exit_code=0,
        message=f"installed to {destination}",
        stdout=result.stdout,
        stderr=result.stderr,
        commands=tuple(commands),
    )


def _rmdir(path: Path) -> None:
    try:
        shutil.rmtree(path, ignore_errors=True)
    except OSError:  # pragma: no cover - defensive
        pass
