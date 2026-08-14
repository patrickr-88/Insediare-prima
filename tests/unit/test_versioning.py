"""Version comparison: the logic that decides upgrade vs skip vs downgrade."""

from __future__ import annotations

import pytest

from usbinstaller.versioning import Comparison, VersionError, compare, extract, is_valid


class TestNumericScheme:
    @pytest.mark.parametrize(
        ("installed", "available", "expected"),
        [
            ("140.0", "141.0", Comparison.OLDER),
            ("141.0", "141.0", Comparison.SAME),
            ("142.0", "141.0", Comparison.NEWER),
            ("3.0.21", "3.0.21", Comparison.SAME),
            ("3.0.9", "3.0.21", Comparison.OLDER),  # not string ordering
            ("25.01", "25.1", Comparison.SAME),  # 01 == 1 numerically
            ("1.2", "1.2.0", Comparison.SAME),  # padded with zeros
            ("1.2", "1.2.1", Comparison.OLDER),
        ],
    )
    def test_comparison(self, installed, available, expected):
        assert compare(installed, available, "numeric") is expected

    def test_decorated_versions_are_compared_on_their_numbers(self):
        assert compare("3.0.21-win64", "3.0.22-win64", "numeric") is Comparison.OLDER

    def test_non_numeric_input_is_unknown_not_equal(self):
        assert compare("stable", "beta", "numeric") is Comparison.UNKNOWN


class TestSemverScheme:
    @pytest.mark.parametrize(
        ("installed", "available", "expected"),
        [
            ("1.9.0", "1.10.0", Comparison.OLDER),
            ("1.92.0", "1.92.0", Comparison.SAME),
            ("2.0.0", "1.92.0", Comparison.NEWER),
            ("1.0.0-beta", "1.0.0", Comparison.OLDER),
            ("1.0.0", "1.0.0-beta", Comparison.NEWER),
            ("1.0.0-alpha", "1.0.0-beta", Comparison.OLDER),
            ("v1.0.0", "1.0.0", Comparison.SAME),
            ("1.0.0+build1", "1.0.0+build2", Comparison.SAME),
        ],
    )
    def test_comparison(self, installed, available, expected):
        assert compare(installed, available, "semver") is expected

    def test_malformed_semver_is_unknown(self):
        assert compare("1.0", "1.0.0", "semver") is Comparison.UNKNOWN
        assert compare("not-a-version", "1.0.0", "semver") is Comparison.UNKNOWN


class TestOtherSchemes:
    def test_date_scheme(self):
        assert compare("2024-01-01", "2024-06-01", "date") is Comparison.OLDER
        assert compare("20240601", "2024-06-01", "date") is Comparison.SAME
        assert compare("2024.07.01", "2024-06-01", "date") is Comparison.NEWER

    def test_date_scheme_rejects_non_dates(self):
        assert compare("v1", "2024-06-01", "date") is Comparison.UNKNOWN

    def test_string_scheme_only_reports_equality(self):
        assert compare("2024 Release", "2024 Release", "string") is Comparison.SAME
        assert compare("2023 Release", "2024 Release", "string") is Comparison.UNKNOWN

    def test_auto_falls_back_to_equality_for_unparseable_versions(self):
        assert compare("Gold", "Gold", "auto") is Comparison.SAME
        assert compare("Gold", "Platinum", "auto") is Comparison.UNKNOWN

    def test_auto_uses_numbers_when_available(self):
        assert compare("1.0", "2.0", "auto") is Comparison.OLDER

    def test_unknown_scheme_raises(self):
        with pytest.raises(VersionError):
            compare("1", "2", "nonsense")


class TestMissingVersions:
    @pytest.mark.parametrize(
        ("installed", "available"),
        [(None, "1.0"), ("1.0", None), (None, None), ("", "1.0"), ("  ", "1.0")],
    )
    def test_missing_side_is_unknown(self, installed, available):
        assert compare(installed, available, "numeric") is Comparison.UNKNOWN


class TestExtractionPattern:
    def test_pattern_pulls_the_comparable_part_out(self):
        result = compare(
            "Adobe Acrobat Reader 24.002",
            "Adobe Acrobat Reader 24.005",
            "numeric",
            r"(\d+\.\d+)",
        )
        assert result is Comparison.OLDER

    def test_pattern_that_does_not_match_is_unknown(self):
        assert compare("no numbers", "1.0", "numeric", r"(\d+\.\d+)") is Comparison.UNKNOWN

    def test_extract_without_groups_returns_whole_match(self):
        assert extract("version 4.5 final", r"\d+\.\d+") == "4.5"

    def test_extract_passes_through_without_a_pattern(self):
        assert extract("  1.2.3 ") == "1.2.3"
        assert extract(None) is None


class TestIsValid:
    @pytest.mark.parametrize(
        ("value", "scheme", "expected"),
        [
            ("1.0.0", "semver", True),
            ("1.0", "semver", False),
            ("25.01", "numeric", True),
            ("stable", "numeric", False),
            ("2024-06-01", "date", True),
            ("anything", "string", True),
            ("", "numeric", False),
            (None, "numeric", False),
        ],
    )
    def test_is_valid(self, value, scheme, expected):
        assert is_valid(value, scheme) is expected
