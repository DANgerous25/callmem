"""Tests for code anchor parsing and validation (src/callmem/core/anchors.py)."""

from __future__ import annotations

from callmem.core.anchors import (
    is_within_root,
    parse_file_anchors,
    validate_anchor,
)


class TestParseFileAnchors:
    def test_parses_path_with_line_number(self) -> None:
        anchors = parse_file_anchors(
            "Refactor _build_footer_parts in briefing.py:340 to split "
            "body/footer"
        )
        assert ("briefing.py", 340) in anchors

    def test_parses_nested_path_without_line_number(self) -> None:
        anchors = parse_file_anchors(
            "See src/callmem/core/repository.py for the SQL"
        )
        assert ("src/callmem/core/repository.py", None) in anchors

    def test_dedupes_by_path_keeping_first_line_number(self) -> None:
        anchors = parse_file_anchors(
            "First mentioned at foo.py:10, later again at foo.py:99"
        )
        assert anchors == [("foo.py", 10)]

    def test_ignores_prose_that_looks_like_an_abbreviation(self) -> None:
        anchors = parse_file_anchors("e.g. this is not a file, i.e. really")
        assert anchors == []

    def test_empty_text_returns_empty_list(self) -> None:
        assert parse_file_anchors("") == []
        assert parse_file_anchors(None) == []  # type: ignore[arg-type]

    def test_multiple_distinct_files(self) -> None:
        anchors = parse_file_anchors(
            "Changed config.yaml:12 and also src/callmem/core/anchors.py"
        )
        assert set(anchors) == {
            ("config.yaml", 12),
            ("src/callmem/core/anchors.py", None),
        }


class TestIsWithinRoot:
    def test_relative_path_inside_root(self) -> None:
        assert is_within_root("src/foo.py", "/home/x/project") is True

    def test_absolute_path_inside_root(self) -> None:
        assert (
            is_within_root("/home/x/project/src/foo.py", "/home/x/project")
            is True
        )

    def test_traversal_outside_root_is_rejected(self) -> None:
        assert is_within_root("../../etc/passwd", "/home/x/project") is False

    def test_absolute_path_outside_root_is_rejected(self) -> None:
        assert is_within_root("/etc/passwd", "/home/x/project") is False

    def test_empty_inputs_are_rejected(self) -> None:
        assert is_within_root("", "/home/x/project") is False
        assert is_within_root("src/foo.py", "") is False


class TestValidateAnchor:
    def test_existing_file_is_valid(self, tmp_path) -> None:
        (tmp_path / "foo.py").write_text("x = 1\n")
        assert validate_anchor("foo.py", str(tmp_path)) is True

    def test_missing_file_is_invalid(self, tmp_path) -> None:
        assert validate_anchor("gone.py", str(tmp_path)) is False

    def test_no_project_root_is_unvalidated(self) -> None:
        assert validate_anchor("foo.py", None) is None

    def test_outside_root_is_unvalidated_and_never_statted(
        self, tmp_path, monkeypatch,
    ) -> None:
        """Security boundary: a path outside the project root must never
        be stat'd, not even to report it as missing."""
        from pathlib import Path

        calls: list[Path] = []
        real_exists = Path.exists

        def counting_exists(self: Path) -> bool:
            calls.append(self)
            return real_exists(self)

        monkeypatch.setattr(Path, "exists", counting_exists)

        result = validate_anchor("../../etc/passwd", str(tmp_path))

        assert result is None
        assert calls == []

    def test_symlink_inside_root_pointing_outside_is_unvalidated(
        self, tmp_path, monkeypatch,
    ) -> None:
        """A symlink can lexically sit inside the root (is_within_root
        passes on the symlink's own path) while its target resolves
        outside it — the resolved outside target must never be stat'd."""
        from pathlib import Path

        outside_dir = tmp_path.parent / f"{tmp_path.name}_outside"
        outside_dir.mkdir()
        outside_target = outside_dir / "secret.py"
        outside_target.write_text("secret = 1\n")

        link = tmp_path / "link.py"
        link.symlink_to(outside_target)

        calls: list[str] = []
        real_exists = Path.exists

        def counting_exists(self: Path) -> bool:
            calls.append(str(self))
            return real_exists(self)

        monkeypatch.setattr(Path, "exists", counting_exists)

        result = validate_anchor("link.py", str(tmp_path))

        assert result is None
        assert not any("secret.py" in c for c in calls)

    def test_symlink_inside_root_pointing_inside_root_still_validates(
        self, tmp_path,
    ) -> None:
        """A symlink whose resolved target is also inside the root is a
        legitimate anchor and should validate normally."""
        real_file = tmp_path / "real.py"
        real_file.write_text("x = 1\n")
        link = tmp_path / "link.py"
        link.symlink_to(real_file)

        assert validate_anchor("link.py", str(tmp_path)) is True

    def test_stat_oserror_is_unvalidated_not_raised(
        self, tmp_path, monkeypatch,
    ) -> None:
        from pathlib import Path

        def raising_exists(self: Path) -> bool:
            raise OSError("simulated stat failure")

        monkeypatch.setattr(Path, "exists", raising_exists)

        assert validate_anchor("foo.py", str(tmp_path)) is None
