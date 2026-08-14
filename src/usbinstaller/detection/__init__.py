"""Installed-application detection.

Importing this package registers every built-in detector. Third parties can add
their own by calling :func:`usbinstaller.detection.base.register`.
"""

from .base import (
    DetectionContext,
    Detector,
    NullDetector,
    detect_installed,
    get_detector,
    register,
    registered_methods,
)
from .generic import CommandVersionDetector, PathExistsDetector
from .macos import MacAppBundleDetector, MacPkgReceiptDetector
from .windows import WindowsPathDetector, WindowsRegistryDetector

#: The detectors in the sibling modules self-register via the ``@register``
#: class decorator; ``NullDetector`` lives in ``base`` and is registered here.
register(NullDetector)

__all__ = [
    "CommandVersionDetector",
    "DetectionContext",
    "Detector",
    "MacAppBundleDetector",
    "MacPkgReceiptDetector",
    "NullDetector",
    "PathExistsDetector",
    "WindowsPathDetector",
    "WindowsRegistryDetector",
    "detect_installed",
    "get_detector",
    "register",
    "registered_methods",
]
