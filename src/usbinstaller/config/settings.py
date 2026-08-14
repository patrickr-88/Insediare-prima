"""Global runtime settings (``config/settings.json``).

Every field has a safe default, so a repository without a settings file still
works. Unknown keys are reported as warnings by the validator rather than
silently ignored, because a typo in a security setting must not fail open.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

from ..errors import ConfigParseError, ConfigSchemaError


@dataclass(frozen=True)
class Settings:
    """Repository-wide behaviour switches."""

    #: Refuse to run any installer whose SHA-256 is missing or mismatched.
    strict_checksums: bool = False
    #: Warn (but continue) when a checksum is missing. Mismatches always warn.
    warn_on_missing_checksum: bool = True
    #: A mismatched checksum blocks execution even outside strict mode.
    block_on_checksum_mismatch: bool = True
    #: Default per-installer timeout; may be overridden per platform payload.
    default_timeout_seconds: int = 1800
    #: Keep at most this many log session directories (0 = keep everything).
    log_retention: int = 50
    #: Minimum free space required on the system drive before installing.
    minimum_free_disk_mb: int = 2048
    #: Require an explicit confirmation before the installation phase.
    require_confirmation: bool = True
    #: Continue installing remaining applications after a failure.
    continue_on_failure: bool = True
    #: Automatically install missing dependencies that are in the catalogue.
    auto_include_dependencies: bool = True
    #: Number of automatic retries for a failed installer (0 = none).
    auto_retry_count: int = 0
    #: Where logs are written, relative to the repository root.
    log_directory: str = "logs"
    #: Repository display name shown in the UI.
    repository_name: str = "USB Software Installer"
    #: Bytes of installer stdout/stderr kept in the log per application.
    output_capture_bytes: int = 8192
    #: Extra directories searched for macOS applications during detection.
    macos_application_dirs: tuple[str, ...] = field(
        default_factory=lambda: ("/Applications", "~/Applications")
    )

    @classmethod
    def field_names(cls) -> set[str]:
        return {f.name for f in fields(cls)}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Settings:
        """Build settings from a mapping, type-checking every known key."""
        if not isinstance(data, dict):
            raise ConfigSchemaError("settings.json must contain a JSON object")
        known = cls.field_names()
        kwargs: dict[str, Any] = {}
        defaults = cls()
        for key, value in data.items():
            if key not in known:
                continue  # reported by the validator
            current = getattr(defaults, key)
            if isinstance(current, bool):
                if not isinstance(value, bool):
                    raise ConfigSchemaError(f"settings.{key} must be a boolean")
                kwargs[key] = value
            elif isinstance(current, int):
                if isinstance(value, bool) or not isinstance(value, int):
                    raise ConfigSchemaError(f"settings.{key} must be an integer")
                if value < 0:
                    raise ConfigSchemaError(f"settings.{key} must not be negative")
                kwargs[key] = value
            elif isinstance(current, tuple):
                if not isinstance(value, list) or not all(
                    isinstance(v, str) for v in value
                ):
                    raise ConfigSchemaError(f"settings.{key} must be a list of strings")
                kwargs[key] = tuple(value)
            else:
                if not isinstance(value, str):
                    raise ConfigSchemaError(f"settings.{key} must be a string")
                kwargs[key] = value
        return cls(**kwargs)

    @classmethod
    def load(cls, path: Path) -> Settings:
        """Load settings, falling back to defaults when the file is absent."""
        if not path.exists():
            return cls()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ConfigParseError(f"{path} is not valid JSON: {exc}") from exc
        return cls.from_dict(raw)

    def unknown_keys(self, data: dict[str, Any]) -> list[str]:
        return sorted(k for k in data if k not in self.field_names())

    def to_dict(self) -> dict[str, Any]:
        return {
            f.name: (
                list(getattr(self, f.name))
                if isinstance(getattr(self, f.name), tuple)
                else getattr(self, f.name)
            )
            for f in fields(self)
        }
