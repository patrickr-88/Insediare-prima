"""Path containment and checksum verification."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from usbinstaller.errors import ChecksumError, ConfigParseError, PathTraversalError
from usbinstaller.security.checksums import ChecksumStore, digest_of, sha256_file
from usbinstaller.security.paths import (
    extension_allowed,
    is_suspicious,
    normalise,
    resolve_within,
)


class TestPathContainment:
    @pytest.mark.parametrize(
        "candidate",
        [
            "../../etc/passwd",
            "installers/../../secrets.txt",
            "/etc/passwd",
            "C:\\Windows\\System32\\cmd.exe",
            "\\\\server\\share\\evil.exe",
            "C:relative.exe",
            "",
            "   ",
            "installers/windows/\x00evil.exe",
        ],
    )
    def test_unsafe_paths_are_rejected(self, tmp_path: Path, candidate: str):
        assert is_suspicious(candidate) is not None
        with pytest.raises(PathTraversalError):
            resolve_within(tmp_path, candidate)

    @pytest.mark.parametrize(
        "candidate",
        [
            "installers/windows/firefox/setup.exe",
            "installers\\windows\\firefox\\setup.exe",
            "./installers/macos/vlc/vlc.dmg",
            "scripts/macos/post-install.sh",
        ],
    )
    def test_safe_paths_resolve_under_the_root(self, tmp_path: Path, candidate: str):
        resolved = resolve_within(tmp_path, candidate)
        assert resolved.is_relative_to(tmp_path.resolve())

    def test_windows_separators_are_normalised(self):
        assert normalise("installers\\windows\\app\\x.exe").as_posix() == (
            "installers/windows/app/x.exe"
        )

    def test_symlink_escaping_the_repository_is_rejected(self, tmp_path: Path):
        """A symlink on the USB drive must not become a way out of the root."""
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "payload.exe").write_bytes(b"x")
        root = tmp_path / "usb"
        (root / "installers").mkdir(parents=True)
        (root / "installers" / "link").symlink_to(outside)

        with pytest.raises(PathTraversalError):
            resolve_within(root, "installers/link/payload.exe")

    def test_symlink_inside_the_repository_is_allowed(self, tmp_path: Path):
        root = tmp_path / "usb"
        real = root / "installers" / "real"
        real.mkdir(parents=True)
        (real / "setup.exe").write_bytes(b"x")
        (root / "installers" / "alias").symlink_to(real)

        resolved = resolve_within(root, "installers/alias/setup.exe")
        assert resolved.is_relative_to(root.resolve())


class TestExtensionAllowList:
    @pytest.mark.parametrize(
        ("filename", "installer_type", "expected"),
        [
            ("setup.exe", "exe", True),
            ("setup.EXE", "exe", True),
            ("setup.msi", "msi", True),
            ("setup.exe", "msi", False),
            ("payload.dmg", "dmg", True),
            ("payload.pkg", "pkg", True),
            ("script.ps1", "powershell", True),
            ("script.sh", "shell", True),
            ("evil.bat", "exe", False),
            ("evil.dll", "exe", False),
            ("app.msix", "msix", True),
            ("anything.exe", "unknown-type", False),
        ],
    )
    def test_extensions(self, filename, installer_type, expected):
        assert extension_allowed(filename, installer_type) is expected


class TestChecksumStore:
    def test_digest_matches_hashlib(self, tmp_path: Path):
        target = tmp_path / "f.bin"
        target.write_bytes(b"hello world")
        import hashlib

        assert sha256_file(target) == hashlib.sha256(b"hello world").hexdigest()

    def test_verify_ok(self, tmp_path: Path):
        target = tmp_path / "setup.exe"
        target.write_bytes(b"payload")
        store = ChecksumStore({"setup.exe": {"sha256": sha256_file(target)}})
        assert store.verify("setup.exe", target).ok

    def test_verify_detects_tampering(self, tmp_path: Path):
        target = tmp_path / "setup.exe"
        target.write_bytes(b"payload")
        store = ChecksumStore({"setup.exe": {"sha256": sha256_file(target)}})
        target.write_bytes(b"payload-with-malware")

        result = store.verify("setup.exe", target)
        assert result.status == "mismatch"
        assert "MISMATCH" in result.message

    def test_verify_strict_raises_on_mismatch(self, tmp_path: Path):
        target = tmp_path / "setup.exe"
        target.write_bytes(b"a")
        store = ChecksumStore({"setup.exe": {"sha256": "0" * 64}})
        with pytest.raises(ChecksumError):
            store.verify_strict("setup.exe", target)

    def test_missing_entry_and_missing_file_are_distinguished(self, tmp_path: Path):
        target = tmp_path / "setup.exe"
        target.write_bytes(b"a")
        empty = ChecksumStore()
        assert empty.verify("setup.exe", target).status == "missing_entry"
        assert empty.verify("gone.exe", tmp_path / "gone.exe").status == "missing_file"

    def test_lookup_by_relative_path_and_by_bare_name(self, tmp_path: Path):
        target = tmp_path / "setup.exe"
        target.write_bytes(b"a")
        digest = sha256_file(target)
        by_path = ChecksumStore({"installers/windows/app/setup.exe": {"sha256": digest}})
        assert by_path.expected_for("installers/windows/app/setup.exe") == digest
        assert by_path.expected_for("installers\\windows\\app\\setup.exe") == digest

        by_name = ChecksumStore({"setup.exe": {"sha256": digest}})
        assert by_name.expected_for("installers/windows/app/setup.exe") == digest

    def test_bare_string_entries_are_accepted(self, tmp_path: Path):
        path = tmp_path / "checksums.json"
        path.write_text(json.dumps({"setup.exe": "AB" * 32}))
        store = ChecksumStore.load(path)
        assert store.expected_for("setup.exe") == "ab" * 32

    def test_invalid_store_is_rejected(self, tmp_path: Path):
        path = tmp_path / "checksums.json"
        path.write_text("{ not json")
        with pytest.raises(ConfigParseError):
            ChecksumStore.load(path)

        path.write_text(json.dumps({"setup.exe": {"size": 12}}))
        with pytest.raises(ConfigParseError):
            ChecksumStore.load(path)

    def test_missing_store_loads_empty(self, tmp_path: Path):
        assert len(ChecksumStore.load(tmp_path / "nope.json")) == 0

    def test_update_prune_and_save_roundtrip(self, tmp_path: Path):
        installer = tmp_path / "installers" / "a" / "setup.exe"
        installer.parent.mkdir(parents=True)
        installer.write_bytes(b"payload")

        store = ChecksumStore({"installers/old/gone.exe": {"sha256": "0" * 64}})
        digest = store.update("installers/a/setup.exe", installer)
        removed = store.prune(["installers/a/setup.exe"])
        assert removed == ["installers/old/gone.exe"]

        out = tmp_path / "checksums" / "checksums.json"
        store.save(out)
        reloaded = ChecksumStore.load(out)
        assert reloaded.expected_for("installers/a/setup.exe") == digest
        assert len(reloaded) == 1

    def test_directory_bundles_are_hashed_recursively(self, tmp_path: Path):
        bundle = tmp_path / "App.app"
        (bundle / "Contents").mkdir(parents=True)
        (bundle / "Contents" / "Info.plist").write_bytes(b"<plist/>")
        first = digest_of(bundle)

        (bundle / "Contents" / "extra").write_bytes(b"new file")
        assert digest_of(bundle) != first
