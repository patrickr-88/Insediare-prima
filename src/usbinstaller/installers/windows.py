"""Windows installer types: EXE, MSI, MSIX/APPX and PowerShell scripts."""

from __future__ import annotations

import ntpath
from pathlib import Path

from ..models import OS, Application, PlatformPayload
from .base import InstallContext, Installer, InstallOutcome, register

#: 3010 = "success, reboot required" — a successful install, not a failure.
MSI_REBOOT_REQUIRED = 3010
#: 1641 = "success, installer initiated a reboot".
MSI_REBOOT_INITIATED = 1641


@register
class WindowsExeInstaller(Installer):
    """Vendor ``.exe`` installers.

    Silent-install switches are vendor-specific (``/S``, ``/silent``,
    ``/quiet``…), so they come from configuration rather than being guessed
    here — guessing wrong is how an unattended run turns into a machine sitting
    on a dialog box.
    """

    type = "exe"
    os = OS.WINDOWS

    def build_command(
        self, installer_path: Path, payload: PlatformPayload, context: InstallContext
    ) -> list[str]:
        return [str(installer_path), *payload.arguments]


@register
class WindowsMsiInstaller(Installer):
    """Windows Installer packages, executed through ``msiexec``."""

    type = "msi"
    os = OS.WINDOWS
    #: Applied unless the configuration supplies its own switches.
    default_arguments = ("/qn", "/norestart")

    def build_command(
        self, installer_path: Path, payload: PlatformPayload, context: InstallContext
    ) -> list[str]:
        # ntpath, not pathlib: Windows paths must keep backslashes even when
        # the command is composed on a POSIX host (tests, cross-platform CI).
        msiexec = ntpath.join(
            context.extra.get("system_root", "C:\\Windows"), "System32", "msiexec.exe"
        )
        arguments = payload.arguments or list(self.default_arguments)
        # /i must immediately precede the package path.
        return [msiexec, "/i", str(installer_path), *arguments]

    def install(
        self,
        app: Application,
        payload: PlatformPayload,
        installer_path: Path,
        context: InstallContext,
    ) -> InstallOutcome:
        outcome = super().install(app, payload, installer_path, context)
        if not outcome.success and outcome.exit_code in (
            MSI_REBOOT_REQUIRED,
            MSI_REBOOT_INITIATED,
        ):
            return InstallOutcome(
                success=True,
                exit_code=outcome.exit_code,
                message="installed successfully; a reboot is required to complete",
                stdout=outcome.stdout,
                stderr=outcome.stderr,
                commands=outcome.commands,
            )
        return outcome


@register
class WindowsMsixInstaller(Installer):
    """MSIX/APPX packages, installed with PowerShell's ``Add-AppxPackage``.

    Per-user by design: MSIX provisioning for all users needs a different
    cmdlet and full administrator rights, so a package that must be provisioned
    machine-wide should use a ``powershell`` installer with explicit arguments.
    """

    type = "msix"
    os = OS.WINDOWS
    requires_admin_by_default = False

    def build_command(
        self, installer_path: Path, payload: PlatformPayload, context: InstallContext
    ) -> list[str]:
        return [
            _powershell(context),
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            "Add-AppxPackage",
            "-Path",
            str(installer_path),
            *payload.arguments,
        ]


@register
class WindowsPowerShellInstaller(Installer):
    """A ``.ps1`` script shipped alongside an application.

    ``-ExecutionPolicy Bypass`` applies to this single process only. It does
    not change machine policy and does not disable Defender, SmartScreen or any
    other protection; the script is still subject to AMSI scanning.
    """

    type = "powershell"
    os = OS.WINDOWS

    def build_command(
        self, installer_path: Path, payload: PlatformPayload, context: InstallContext
    ) -> list[str]:
        return [
            _powershell(context),
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(installer_path),
            *payload.arguments,
        ]


def _powershell(context: InstallContext) -> str:
    """Absolute path to PowerShell, never a bare name resolved through PATH."""
    return ntpath.join(
        context.extra.get("system_root", "C:\\Windows"),
        "System32",
        "WindowsPowerShell",
        "v1.0",
        "powershell.exe",
    )
