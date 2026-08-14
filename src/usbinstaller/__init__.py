"""Portable, configuration-driven software deployment from a USB drive.

The engine (this package) is deliberately independent of the software
repository (the USB drive's ``config/`` + ``installers/`` tree): adding,
replacing or removing an application is a data change, never a code change.
"""

from .errors import ConfigError, InstallerError, RepositoryError, SecurityError
from .models import OS, Action, Application, Arch, InstallationPlan, Status, SystemInfo
from .version import APP_NAME, __version__

__all__ = [
    "APP_NAME",
    "Action",
    "Application",
    "Arch",
    "ConfigError",
    "InstallationPlan",
    "InstallerError",
    "OS",
    "RepositoryError",
    "SecurityError",
    "Status",
    "SystemInfo",
    "__version__",
]
