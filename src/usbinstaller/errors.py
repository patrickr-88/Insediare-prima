"""Exception hierarchy for the installer engine.

Every error raised deliberately by this package derives from ``InstallerError``
so callers can distinguish expected failures (bad config, missing installer)
from genuine bugs.
"""

from __future__ import annotations


class InstallerError(Exception):
    """Base class for all errors raised by the installer engine."""


class ConfigError(InstallerError):
    """Configuration could not be read, parsed or validated."""


class ConfigParseError(ConfigError):
    """The configuration file is not valid JSON."""


class ConfigSchemaError(ConfigError):
    """The configuration parsed, but does not satisfy the schema."""


class RepositoryError(InstallerError):
    """The USB repository could not be located or is structurally invalid."""


class SecurityError(InstallerError):
    """An operation was refused because it violates a security control."""


class PathTraversalError(SecurityError):
    """A configured path escapes the repository root."""


class ChecksumError(SecurityError):
    """An installer failed checksum verification."""


class DependencyError(InstallerError):
    """Dependencies are unresolvable (missing or circular)."""


class CircularDependencyError(DependencyError):
    """A dependency cycle was detected."""


class InstallerExecutionError(InstallerError):
    """An installer process failed to start or execute."""


class InstallerTimeout(InstallerExecutionError):
    """An installer process exceeded its timeout and was terminated."""


class UnsupportedInstallerType(InstallerError):
    """No handler is registered for the requested installer type."""
