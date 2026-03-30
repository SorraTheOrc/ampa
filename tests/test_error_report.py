"""Tests for ampa.error_report module."""

import argparse
import datetime as dt
import json
import io
import sys
from unittest import mock

import pytest

from ampa.error_report import (
    ErrorReport,
    build_error_report,
    render_error_report,
    render_error_report_json,
)


# ---------------------------------------------------------------------------
# build_error_report
# ---------------------------------------------------------------------------


class TestBuildErrorReport:
    """Tests for build_error_report()."""

    def test_basic_build(self):
        """Report should capture the exception type and message."""
        exc = ValueError("something broke")
        report = build_error_report(exc, command="run", args={"id": "foo"})

        assert isinstance(report, ErrorReport)
        assert report.command == "run"
        assert report.error_type == "ValueError"
        assert report.error_message == "something broke"
        assert report.args == {"id": "foo"}
        assert report.exit_code == 1

    def test_custom_exit_code(self):
        exc = RuntimeError("boom")
        report = build_error_report(exc, exit_code=42)
        assert report.exit_code == 42

    def test_default_command_and_args(self):
        """When command and args are omitted, sensible defaults are used."""
        exc = RuntimeError("oops")
        report = build_error_report(exc)
        assert report.command == ""
        assert report.args == {}

    def test_traceback_captured(self):
        """When the exception has a traceback, it should be captured."""
        try:
            raise RuntimeError("with traceback")
        except RuntimeError as exc:
            report = build_error_report(exc, command="test")

        assert report.traceback is not None
        assert "RuntimeError: with traceback" in report.traceback

    def test_traceback_none_when_no_traceback(self):
        """An exception without __traceback__ should yield None."""
        exc = RuntimeError("no tb")
        exc.__traceback__ = None
        report = build_error_report(exc, command="test")
        assert report.traceback is None

    def test_timestamp_is_utc_iso(self):
        exc = RuntimeError("ts")
        report = build_error_report(exc)
        # Should be parseable ISO-8601 and have UTC timezone
        parsed = dt.datetime.fromisoformat(report.timestamp)
        assert parsed.tzinfo is not None

    def test_hostname_populated(self):
        exc = RuntimeError("host")
        report = build_error_report(exc)
        assert report.hostname  # non-empty string

    def test_python_version_populated(self):
        exc = RuntimeError("ver")
        report = build_error_report(exc)
        assert report.python_version
        # Should look like "3.x.y"
        parts = report.python_version.split(".")
        assert len(parts) >= 2

    def test_platform_populated(self):
        exc = RuntimeError("plat")
        report = build_error_report(exc)
        assert report.platform  # non-empty


# ---------------------------------------------------------------------------
# ErrorReport serialisation
# ---------------------------------------------------------------------------


class TestErrorReportSerialisation:
    """Tests for ErrorReport.to_dict() and to_json()."""

    def _make_report(self, **kwargs) -> ErrorReport:
        defaults = dict(
            command="test",
            error_type="RuntimeError",
            error_message="boom",
            traceback=None,
            timestamp="2026-01-01T00:00:00+00:00",
            hostname="testhost",
            python_version="3.10.0",
            platform="Linux",
            args={"x": 1},
            exit_code=1,
        )
        defaults.update(kwargs)
        return ErrorReport(**defaults)

    def test_to_dict(self):
        report = self._make_report()
        d = report.to_dict()
        assert isinstance(d, dict)
        assert d["command"] == "test"
        assert d["error_type"] == "RuntimeError"
        assert d["exit_code"] == 1

    def test_to_json(self):
        report = self._make_report()
        text = report.to_json()
        parsed = json.loads(text)
        assert parsed["error_message"] == "boom"

    def test_immutability(self):
        report = self._make_report()
        with pytest.raises(AttributeError):
            report.command = "changed"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# render_error_report (human-readable)
# ---------------------------------------------------------------------------


class TestRenderErrorReport:
    """Tests for render_error_report()."""

    def _make_report(self, **kwargs) -> ErrorReport:
        defaults = dict(
            command="run",
            error_type="RuntimeError",
            error_message="something went wrong",
            traceback="Traceback (most recent call last):\n  ...\nRuntimeError: something went wrong",
            timestamp="2026-01-15T08:30:00+00:00",
            hostname="myhost",
            python_version="3.10.5",
            platform="Linux-5.15-x86_64",
            args={"command_id": "test-cmd"},
            exit_code=1,
        )
        defaults.update(kwargs)
        return ErrorReport(**defaults)

    def test_contains_header_and_footer(self):
        text = render_error_report(self._make_report())
        assert "=== AMPA Error Report ===" in text
        assert "=========================" in text

    def test_contains_all_context_fields(self):
        report = self._make_report()
        text = render_error_report(report)
        assert "Command:   run" in text
        assert "RuntimeError: something went wrong" in text
        assert "Timestamp:" in text
        assert "Host:      myhost" in text
        assert "Python:    3.10.5" in text
        assert "Platform:  Linux-5.15-x86_64" in text
        assert "Exit code: 1" in text

    def test_args_included(self):
        report = self._make_report(args={"foo": "bar"})
        text = render_error_report(report)
        assert "Args:" in text
        assert '"foo"' in text

    def test_args_omitted_when_empty(self):
        report = self._make_report(args={})
        text = render_error_report(report)
        assert "Args:" not in text

    def test_traceback_hidden_by_default(self):
        report = self._make_report()
        text = render_error_report(report, verbose=False)
        assert "--- traceback ---" not in text

    def test_traceback_shown_when_verbose(self):
        report = self._make_report()
        text = render_error_report(report, verbose=True)
        assert "--- traceback ---" in text
        assert "RuntimeError: something went wrong" in text

    def test_unknown_command_fallback(self):
        report = self._make_report(command="")
        text = render_error_report(report)
        assert "Command:   (unknown)" in text

    def test_file_parameter_writes_to_stream(self):
        report = self._make_report()
        buf = io.StringIO()
        text = render_error_report(report, file=buf)
        written = buf.getvalue()
        assert written.strip() == text.strip()

    def test_no_file_returns_text_only(self):
        report = self._make_report()
        text = render_error_report(report)
        assert isinstance(text, str)
        assert len(text) > 0


# ---------------------------------------------------------------------------
# render_error_report_json
# ---------------------------------------------------------------------------


class TestRenderErrorReportJson:
    """Tests for render_error_report_json()."""

    def _make_report(self) -> ErrorReport:
        return ErrorReport(
            command="delegation",
            error_type="OSError",
            error_message="disk full",
            traceback=None,
            timestamp="2026-02-01T12:00:00+00:00",
            hostname="host1",
            python_version="3.11.2",
            platform="Linux-6.0",
            args={"verbose": True},
            exit_code=2,
        )

    def test_valid_json(self):
        text = render_error_report_json(self._make_report())
        parsed = json.loads(text)
        assert parsed["command"] == "delegation"
        assert parsed["exit_code"] == 2

    def test_file_parameter_writes_to_stream(self):
        buf = io.StringIO()
        text = render_error_report_json(self._make_report(), file=buf)
        written = buf.getvalue()
        assert json.loads(written)["error_type"] == "OSError"
        # Verify round-trip: text is valid JSON and re-serialises identically
        assert json.loads(text) == json.loads(
            json.dumps(json.loads(text), indent=2, sort_keys=True)
        )


# ---------------------------------------------------------------------------
# Integration: commands call the helper on unhandled errors
# ---------------------------------------------------------------------------


class TestCommandsCallErrorReport:
    """Verify that CLI commands use the error report helper."""

    def test_main_catches_unhandled_error_human(self):
        """main() should render an error report on unhandled exceptions."""
        from ampa.scheduler_cli import main

        test_args = ["ampa", "list"]
        with mock.patch("sys.argv", test_args):
            with mock.patch(
                "ampa.scheduler_cli._cli_list", side_effect=RuntimeError("test boom")
            ):
                buf = io.StringIO()
                with mock.patch("sys.stderr", buf):
                    with pytest.raises(SystemExit) as exc_info:
                        main()
                assert exc_info.value.code == 1
                output = buf.getvalue()
                assert "=== AMPA Error Report ===" in output
                assert "RuntimeError: test boom" in output
                assert "Command:   list" in output

    def test_main_catches_unhandled_error_json(self):
        """main() should render JSON error report when --json flag is set."""
        from ampa.scheduler_cli import main

        test_args = ["ampa", "list", "--json"]
        with mock.patch("sys.argv", test_args):
            with mock.patch(
                "ampa.scheduler_cli._cli_list", side_effect=RuntimeError("json boom")
            ):
                buf = io.StringIO()
                with mock.patch("sys.stderr", buf):
                    with pytest.raises(SystemExit) as exc_info:
                        main()
                assert exc_info.value.code == 1
                output = buf.getvalue()
                parsed = json.loads(output)
                assert parsed["error_type"] == "RuntimeError"
                assert parsed["error_message"] == "json boom"
                assert parsed["command"] == "list"

    def test_main_passes_through_system_exit(self):
        """SystemExit should not be caught and rendered as an error report."""
        from ampa.scheduler_cli import main

        test_args = ["ampa", "list"]
        with mock.patch("sys.argv", test_args):
            with mock.patch("ampa.scheduler_cli._cli_list", side_effect=SystemExit(3)):
                with pytest.raises(SystemExit) as exc_info:
                    main()
                # Should be the original SystemExit, not our wrapper
                assert exc_info.value.code == 3

    def test_cli_run_execute_once_uses_error_report(self):
        """_cli_run should produce an error report when start_command raises."""
        from ampa.scheduler_cli import _cli_run

        args = argparse.Namespace(
            command_id="test-cmd",
            json=False,
            verbose=False,
            format="normal",
            watch=None,
        )
        with mock.patch("ampa.scheduler_cli.load_scheduler") as mock_load:
            mock_scheduler = mock.MagicMock()
            mock_scheduler.store.get_command.return_value = mock.MagicMock(
                command_id="test-cmd",
                title="Test",
                command_type="shell",
                command="echo hi",
            )
            mock_scheduler.start_command.side_effect = RuntimeError("executor boom")
            mock_load.return_value = mock_scheduler

            buf = io.StringIO()
            with mock.patch("sys.stderr", buf):
                exit_code = _cli_run(args)

            assert exit_code == 1
            output = buf.getvalue()
            assert "=== AMPA Error Report ===" in output
            assert "RuntimeError: executor boom" in output
