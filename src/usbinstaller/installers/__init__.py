"""Installer abstraction and the built-in platform installers.

Importing this package registers every built-in installer type.
"""

from .base import (
    InstallContext,
    Installer,
    InstallOutcome,
    get_installer,
    register,
    registered_types,
)
from .macos import MacAppInstaller, MacDmgInstaller, MacPkgInstaller, MacShellInstaller
from .process import CommandResult, ProcessRunner, RecordingRunner
from .windows import (
    WindowsExeInstaller,
    WindowsMsiInstaller,
    WindowsMsixInstaller,
    WindowsPowerShellInstaller,
)

__all__ = [
    "CommandResult",
    "InstallContext",
    "InstallOutcome",
    "Installer",
    "MacAppInstaller",
    "MacDmgInstaller",
    "MacPkgInstaller",
    "MacShellInstaller",
    "ProcessRunner",
    "RecordingRunner",
    "WindowsExeInstaller",
    "WindowsMsiInstaller",
    "WindowsMsixInstaller",
    "WindowsPowerShellInstaller",
    "get_installer",
    "register",
    "registered_types",
]
