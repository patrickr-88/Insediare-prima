"""Detector interface and registry.

Detection answers three questions for one application: is it installed, which
version, and where. Every detector is read-only — the discovery phase must not
be able to modify the target machine.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from ..errors import InstallerError
from ..installers.process import ProcessRunner
from ..models import Application, DetectionSpec, InstalledApp, SystemInfo


@dataclass
class DetectionContext:
    """Everything a detector may use. No global state, so tests inject freely."""

    system: SystemInfo
    runner: ProcessRunner = field(default_factory=ProcessRunner)
    #: Extra macOS application directories from settings.
    application_dirs: tuple[str, ...] = ("/Applications", "~/Applications")
    #: Injected in tests to avoid touching the real registry/filesystem.
    registry_reader: Callable[[], list[dict[str, str]]] | None = None
    path_exists: Callable[[str], bool] | None = None
    read_plist: Callable[[str], dict] | None = None


class Detector(Protocol):
    """Protocol implemented by every detection method."""

    method: str

    def detect(
        self, app: Application, spec: DetectionSpec, context: DetectionContext
    ) -> InstalledApp: ...


_REGISTRY: dict[str, Detector] = {}

NOT_INSTALLED = InstalledApp(installed=False)


def register(detector):
    """Register a detector under its ``method`` name.

    Accepts either an instance or a class (so it can be used as a class
    decorator); classes are instantiated once and the class is returned
    unchanged, keeping it importable and directly testable.
    """
    instance = detector() if isinstance(detector, type) else detector
    _REGISTRY[instance.method] = instance
    return detector


def get_detector(method: str) -> Detector | None:
    return _REGISTRY.get(method)


def registered_methods() -> tuple[str, ...]:
    return tuple(sorted(_REGISTRY))


class NullDetector:
    """``method: none`` — always reports "not installed, unknown version".

    Used when an application has no reliable detection signal; the planner then
    offers it for installation and says so in the reason column.
    """

    method = "none"

    def detect(
        self, app: Application, spec: DetectionSpec, context: DetectionContext
    ) -> InstalledApp:
        return InstalledApp(
            installed=False,
            method="none",
            detail="no detection method configured",
        )


def detect_installed(
    app: Application, context: DetectionContext, os_=None
) -> InstalledApp:
    """Run the detector configured for *app* on the current platform.

    Detection failures are never fatal: a detector that throws is reported as
    "unknown", which the planner surfaces to the technician instead of silently
    installing over a working application.
    """
    from ..models import OS  # local import keeps module import order simple

    target_os: OS = os_ or context.system.os
    spec = app.detection_for(target_os)
    detector = get_detector(spec.method)
    if detector is None:
        return InstalledApp(
            installed=False,
            method=spec.method,
            detail=f"no detector registered for method {spec.method!r}",
        )
    try:
        return detector.detect(app, spec, context)
    except InstallerError as exc:
        return InstalledApp(installed=False, method=spec.method, detail=str(exc))
    except Exception as exc:  # pragma: no cover - defensive
        return InstalledApp(
            installed=False,
            method=spec.method,
            detail=f"detection failed: {type(exc).__name__}: {exc}",
        )
