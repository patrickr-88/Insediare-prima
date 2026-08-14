"""Platform interrogation: OS, architecture, privileges, disk."""

from .privileges import PrivilegeRequirement, elevation_command, requirement_for
from .system import (
    SystemProbe,
    describe,
    detect_arch,
    detect_os,
    detect_system,
    free_disk_bytes,
    is_admin,
)

__all__ = [
    "PrivilegeRequirement",
    "SystemProbe",
    "describe",
    "detect_arch",
    "detect_os",
    "detect_system",
    "elevation_command",
    "free_disk_bytes",
    "is_admin",
    "requirement_for",
]
