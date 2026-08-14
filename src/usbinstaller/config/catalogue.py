"""Parse ``config/applications.json`` into typed :class:`Application` models.

The loader is deliberately strict: it is the trust boundary between an
untrusted USB drive and code that launches processes. Anything it cannot
understand raises :class:`ConfigSchemaError` with the JSON path of the offending
value, rather than being coerced or dropped.

Parsing does **no** filesystem access — existence of installers is a *validator*
concern (see :mod:`usbinstaller.config.validator`), which keeps this module
trivially unit-testable.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..errors import ConfigParseError, ConfigSchemaError
from ..models import (
    OS,
    Application,
    Arch,
    DetectionSpec,
    PlatformPayload,
    PostInstallAction,
    Prerequisite,
)
from ..version import CONFIG_SCHEMA_VERSION

#: Installer types understood by each platform. Extending the engine with a new
#: installer type means adding it here and registering a handler.
INSTALLER_TYPES: dict[OS, tuple[str, ...]] = {
    OS.WINDOWS: ("exe", "msi", "msix", "powershell"),
    OS.MACOS: ("dmg", "pkg", "app", "shell"),
}

#: Detection methods understood by the detection engine.
DETECTION_METHODS = (
    "none",
    "windows_registry",
    "windows_path",
    "macos_app_bundle",
    "macos_pkg_receipt",
    "path_exists",
    "command_version",
)

PREREQUISITE_TYPES = (
    "admin",
    "min_os_version",
    "min_free_disk_mb",
    "path_exists",
)

POST_INSTALL_TYPES = ("script", "message")

_PLATFORM_KEYS = {"windows": OS.WINDOWS, "macos": OS.MACOS}


def _require(data: Any, kind: type, where: str) -> Any:
    if not isinstance(data, kind) or (kind is not bool and isinstance(data, bool)):
        raise ConfigSchemaError(f"{where}: expected {kind.__name__}")
    return data


def _str_list(value: Any, where: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return list(value)
    raise ConfigSchemaError(f"{where}: expected a string or a list of strings")


def _parse_arguments(value: Any, where: str) -> list[str]:
    """Installer arguments must be a pre-tokenised list of strings.

    A single string is accepted for authoring convenience and split with POSIX
    rules, but the result is still passed to the OS as an argv vector — the
    engine never invokes a shell, so no quoting or metacharacter can inject a
    second command.
    """
    import shlex

    if value is None:
        return []
    if isinstance(value, list):
        if not all(isinstance(v, str) for v in value):
            raise ConfigSchemaError(f"{where}: every argument must be a string")
        return list(value)
    if isinstance(value, str):
        try:
            return shlex.split(value)
        except ValueError as exc:
            raise ConfigSchemaError(f"{where}: cannot parse arguments ({exc})") from exc
    raise ConfigSchemaError(f"{where}: expected a string or a list of strings")


def _parse_detection(value: Any, where: str) -> DetectionSpec:
    if value is None:
        return DetectionSpec()
    if isinstance(value, str):
        method, options = value, {}
    elif isinstance(value, dict):
        method = value.get("method", "none")
        if not isinstance(method, str):
            raise ConfigSchemaError(f"{where}.method: expected a string")
        options = {k: v for k, v in value.items() if k != "method"}
    else:
        raise ConfigSchemaError(f"{where}: expected an object")
    if method not in DETECTION_METHODS:
        raise ConfigSchemaError(
            f"{where}.method: unknown detection method {method!r} "
            f"(expected one of {', '.join(DETECTION_METHODS)})"
        )
    return DetectionSpec(method=method, options=options)


def _parse_prerequisites(value: Any, where: str) -> tuple[Prerequisite, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ConfigSchemaError(f"{where}: expected a list")
    out: list[Prerequisite] = []
    for index, raw in enumerate(value):
        loc = f"{where}[{index}]"
        if isinstance(raw, str):
            raw = {"type": raw}
        if not isinstance(raw, dict):
            raise ConfigSchemaError(f"{loc}: expected an object or a string")
        ptype = raw.get("type")
        if not isinstance(ptype, str) or ptype not in PREREQUISITE_TYPES:
            raise ConfigSchemaError(
                f"{loc}.type: unknown prerequisite {ptype!r} "
                f"(expected one of {', '.join(PREREQUISITE_TYPES)})"
            )
        message = raw.get("message")
        if message is not None and not isinstance(message, str):
            raise ConfigSchemaError(f"{loc}.message: expected a string")
        out.append(Prerequisite(type=ptype, value=raw.get("value"), message=message))
    return tuple(out)


def _parse_post_install(value: Any, where: str) -> tuple[PostInstallAction, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ConfigSchemaError(f"{where}: expected a list")
    out: list[PostInstallAction] = []
    for index, raw in enumerate(value):
        loc = f"{where}[{index}]"
        if not isinstance(raw, dict):
            raise ConfigSchemaError(f"{loc}: expected an object")
        atype = raw.get("type")
        if atype not in POST_INSTALL_TYPES:
            raise ConfigSchemaError(
                f"{loc}.type: unknown post-install action {atype!r} "
                f"(expected one of {', '.join(POST_INSTALL_TYPES)})"
            )
        script = raw.get("script")
        if atype == "script":
            if not isinstance(script, str) or not script.strip():
                raise ConfigSchemaError(f"{loc}.script: required for script actions")
        message = raw.get("message")
        if atype == "message" and not isinstance(message, str):
            raise ConfigSchemaError(f"{loc}.message: required for message actions")
        timeout = raw.get("timeout_seconds")
        if timeout is not None and (
            isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0
        ):
            raise ConfigSchemaError(f"{loc}.timeout_seconds: expected a positive int")
        out.append(
            PostInstallAction(
                type=atype,
                script=script if isinstance(script, str) else None,
                arguments=_parse_arguments(raw.get("arguments"), f"{loc}.arguments"),
                message=message if isinstance(message, str) else None,
                timeout_seconds=timeout,
            )
        )
    return tuple(out)


def _parse_architectures(value: Any, where: str) -> tuple[Arch, ...]:
    names = _str_list(value, where)
    out: list[Arch] = []
    for name in names:
        arch = Arch.parse(name)
        if arch is Arch.UNKNOWN:
            raise ConfigSchemaError(f"{where}: unsupported architecture {name!r}")
        out.append(arch)
    return tuple(dict.fromkeys(out))


def _parse_exit_codes(value: Any, where: str) -> tuple[int, ...]:
    if value is None:
        return (0,)
    if not isinstance(value, list) or not value:
        raise ConfigSchemaError(f"{where}: expected a non-empty list of integers")
    for item in value:
        if isinstance(item, bool) or not isinstance(item, int):
            raise ConfigSchemaError(f"{where}: expected integers")
    return tuple(dict.fromkeys(int(i) for i in value))


def _parse_payload(raw: Any, os_: OS, where: str) -> PlatformPayload:
    if not isinstance(raw, dict):
        raise ConfigSchemaError(f"{where}: expected an object")

    installer = raw.get("installer")
    if not isinstance(installer, str) or not installer.strip():
        raise ConfigSchemaError(f"{where}.installer: required, must be a non-empty path")

    itype = raw.get("type")
    if not isinstance(itype, str):
        raise ConfigSchemaError(f"{where}.type: required, must be a string")
    itype = itype.strip().lower()
    allowed = INSTALLER_TYPES[os_]
    if itype not in allowed:
        raise ConfigSchemaError(
            f"{where}.type: unsupported installer type {itype!r} for {os_.value} "
            f"(expected one of {', '.join(allowed)})"
        )

    sha256 = raw.get("sha256")
    if sha256 is not None:
        if not isinstance(sha256, str) or len(sha256) != 64:
            raise ConfigSchemaError(f"{where}.sha256: expected a 64-character hex digest")
        try:
            int(sha256, 16)
        except ValueError as exc:
            raise ConfigSchemaError(f"{where}.sha256: not hexadecimal") from exc
        sha256 = sha256.lower()

    timeout = raw.get("timeout_seconds")
    if timeout is not None and (
        isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0
    ):
        raise ConfigSchemaError(f"{where}.timeout_seconds: expected a positive integer")

    requires_admin = raw.get("requires_admin", os_ is OS.WINDOWS or itype == "pkg")
    if not isinstance(requires_admin, bool):
        raise ConfigSchemaError(f"{where}.requires_admin: expected a boolean")

    app_bundle = raw.get("app_bundle")
    if app_bundle is not None and not isinstance(app_bundle, str):
        raise ConfigSchemaError(f"{where}.app_bundle: expected a string")

    known = {
        "installer",
        "type",
        "arguments",
        "architectures",
        "requires_admin",
        "sha256",
        "timeout_seconds",
        "success_exit_codes",
        "detection",
        "post_install",
        "app_bundle",
    }
    return PlatformPayload(
        installer=installer.strip(),
        type=itype,
        arguments=_parse_arguments(raw.get("arguments"), f"{where}.arguments"),
        architectures=_parse_architectures(
            raw.get("architectures"), f"{where}.architectures"
        ),
        requires_admin=requires_admin,
        sha256=sha256,
        timeout_seconds=timeout,
        success_exit_codes=_parse_exit_codes(
            raw.get("success_exit_codes"), f"{where}.success_exit_codes"
        ),
        detection=(
            _parse_detection(raw["detection"], f"{where}.detection")
            if "detection" in raw
            else None
        ),
        post_install=_parse_post_install(
            raw.get("post_install"), f"{where}.post_install"
        ),
        app_bundle=app_bundle,
        extra={k: v for k, v in raw.items() if k not in known},
    )


def parse_application(raw: Any, where: str) -> Application:
    """Parse one entry of the ``applications`` array."""
    if not isinstance(raw, dict):
        raise ConfigSchemaError(f"{where}: expected an object")

    app_id = raw.get("id")
    if not isinstance(app_id, str) or not app_id.strip():
        raise ConfigSchemaError(f"{where}.id: required, must be a non-empty string")
    app_id = app_id.strip()
    if not all(ch.isalnum() or ch in "-_." for ch in app_id):
        raise ConfigSchemaError(
            f"{where}.id: {app_id!r} may only contain letters, digits, '-', '_' and '.'"
        )

    name = raw.get("name", app_id)
    if not isinstance(name, str) or not name.strip():
        raise ConfigSchemaError(f"{where}.name: expected a non-empty string")

    for key in ("version", "description", "category", "version_pattern"):
        if key in raw and raw[key] is not None and not isinstance(raw[key], str):
            raise ConfigSchemaError(f"{where}.{key}: expected a string")

    for key in ("enabled", "selected_by_default"):
        if key in raw and not isinstance(raw[key], bool):
            raise ConfigSchemaError(f"{where}.{key}: expected a boolean")

    scheme = raw.get("version_scheme", "auto")
    if not isinstance(scheme, str):
        raise ConfigSchemaError(f"{where}.version_scheme: expected a string")
    from ..versioning import SCHEMES

    if scheme.lower() not in SCHEMES:
        raise ConfigSchemaError(
            f"{where}.version_scheme: unknown scheme {scheme!r} "
            f"(expected one of {', '.join(SCHEMES)})"
        )

    pattern = raw.get("version_pattern")
    if isinstance(pattern, str):
        import re

        try:
            re.compile(pattern)
        except re.error as exc:
            raise ConfigSchemaError(
                f"{where}.version_pattern: invalid regular expression ({exc})"
            ) from exc

    platforms: dict[OS, PlatformPayload] = {}
    for key, os_ in _PLATFORM_KEYS.items():
        if raw.get(key) is not None:
            platforms[os_] = _parse_payload(raw[key], os_, f"{where}.{key}")
    if not platforms:
        raise ConfigSchemaError(
            f"{where}: at least one platform section "
            f"({' or '.join(_PLATFORM_KEYS)}) is required"
        )

    dependencies = tuple(
        dict.fromkeys(_str_list(raw.get("dependencies"), f"{where}.dependencies"))
    )
    if app_id in dependencies:
        raise ConfigSchemaError(f"{where}.dependencies: {app_id!r} depends on itself")

    return Application(
        id=app_id,
        name=name.strip(),
        version=(raw.get("version") or "").strip(),
        description=(raw.get("description") or "").strip(),
        category=(raw.get("category") or "Uncategorised").strip(),
        enabled=bool(raw.get("enabled", True)),
        version_scheme=scheme.lower(),
        version_pattern=pattern if isinstance(pattern, str) else None,
        dependencies=dependencies,
        prerequisites=_parse_prerequisites(
            raw.get("prerequisites"), f"{where}.prerequisites"
        ),
        detection=_parse_detection(raw.get("detection"), f"{where}.detection"),
        platforms=platforms,
        selected_by_default=bool(raw.get("selected_by_default", True)),
    )


@dataclass(frozen=True)
class Catalogue:
    """The parsed application catalogue."""

    applications: tuple[Application, ...]
    schema_version: int = CONFIG_SCHEMA_VERSION
    source: Path | None = None

    def __iter__(self) -> Iterator[Application]:
        return iter(self.applications)

    def __len__(self) -> int:
        return len(self.applications)

    def get(self, app_id: str) -> Application | None:
        return next((a for a in self.applications if a.id == app_id), None)

    @property
    def ids(self) -> tuple[str, ...]:
        return tuple(a.id for a in self.applications)

    def enabled(self) -> tuple[Application, ...]:
        return tuple(a for a in self.applications if a.enabled)

    def for_platform(self, os_: OS, arch: Arch) -> tuple[Application, ...]:
        """Applications installable on this exact machine.

        This is the filter that keeps incompatible installers off the screen:
        an ARM-only package never appears on an Intel Mac, and a Windows-only
        application never appears on macOS.
        """
        return tuple(a for a in self.enabled() if a.supports(os_, arch))

    def categories(self) -> tuple[str, ...]:
        return tuple(sorted({a.category for a in self.applications}))

    def select(self, ids: Iterable[str]) -> tuple[Application, ...]:
        wanted = list(dict.fromkeys(ids))
        missing = [i for i in wanted if self.get(i) is None]
        if missing:
            raise ConfigSchemaError(f"unknown application id(s): {', '.join(missing)}")
        return tuple(self.get(i) for i in wanted)  # type: ignore[misc]


def parse_catalogue(data: Any, source: Path | None = None) -> Catalogue:
    """Parse an already-decoded ``applications.json`` document."""
    if not isinstance(data, dict):
        raise ConfigSchemaError("applications.json must contain a JSON object")

    schema_version = data.get("schema_version", CONFIG_SCHEMA_VERSION)
    if isinstance(schema_version, bool) or not isinstance(schema_version, int):
        raise ConfigSchemaError("schema_version: expected an integer")
    if schema_version > CONFIG_SCHEMA_VERSION:
        raise ConfigSchemaError(
            f"schema_version {schema_version} is newer than this build supports "
            f"({CONFIG_SCHEMA_VERSION}); update the installer application"
        )

    raw_apps = data.get("applications")
    if not isinstance(raw_apps, list):
        raise ConfigSchemaError("applications: required, must be a list")

    apps = [
        parse_application(raw, f"applications[{index}]")
        for index, raw in enumerate(raw_apps)
    ]

    seen: dict[str, int] = {}
    for index, app in enumerate(apps):
        if app.id in seen:
            raise ConfigSchemaError(
                f"applications[{index}].id: duplicate application id {app.id!r} "
                f"(first defined at applications[{seen[app.id]}])"
            )
        seen[app.id] = index

    return Catalogue(
        applications=tuple(apps), schema_version=schema_version, source=source
    )


def load_catalogue(path: Path) -> Catalogue:
    """Read and parse ``applications.json`` from disk."""
    if not path.exists():
        raise ConfigParseError(f"configuration file not found: {path}")
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigParseError(f"cannot read {path}: {exc}") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConfigParseError(
            f"{path} is not valid JSON: {exc.msg} (line {exc.lineno}, column {exc.colno})"
        ) from exc
    return parse_catalogue(data, source=path)
