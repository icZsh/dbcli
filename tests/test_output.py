from __future__ import annotations

import io
import json

from dbcli.output import build_output_config, emit_result, log, result_envelope


class _FakeStream:
    def __init__(self, is_tty: bool) -> None:
        self._is_tty = is_tty

    def isatty(self) -> bool:
        return self._is_tty


def test_ci_is_detected_from_non_tty_stdin() -> None:
    config = build_output_config(stdin=_FakeStream(is_tty=False))

    assert config.ci is True
    assert config.no_progress is True


def test_explicit_ci_forces_no_progress() -> None:
    config = build_output_config(ci=True, stdin=_FakeStream(is_tty=True))

    assert config.ci is True
    assert config.no_progress is True


def test_emit_result_writes_json_to_stdout_only() -> None:
    stdout = io.StringIO()
    stderr = io.StringIO()
    config = build_output_config(json_output=True, stdin=_FakeStream(is_tty=True))
    payload = result_envelope(status="success", command="inspect", exit_code=0)

    emit_result(payload, config, stdout=stdout, stderr=stderr)

    assert json.loads(stdout.getvalue())["command"] == "inspect"
    assert stderr.getvalue() == ""


def test_ci_log_is_single_line_key_value_stderr() -> None:
    stderr = io.StringIO()
    config = build_output_config(ci=True, stdin=_FakeStream(is_tty=True))

    log("info", "loaded rows", config, stderr=stderr, command="load", rows_read=10)

    assert stderr.getvalue() == "level=info message='loaded rows' command=load rows_read=10\n"
