import os
import sys
import time

import pytest

from factory.errors import Blocked, Interrupted
from factory.process import ProcessRunner


def test_large_stdin_and_both_streams_are_drained_without_deadlock(tmp_path):
    data = "a" * 200_000
    script = (
        "import sys; sys.stderr.write('e'*200000); value=sys.stdin.read(); sys.stdout.write(value)"
    )
    result = ProcessRunner().run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        input_text=data,
        timeout=5,
    )
    assert result.returncode == 0
    assert result.stdout == data
    assert result.stderr == "e" * 200_000


def test_output_log_and_result_redact_credentials(tmp_path):
    env = dict(os.environ) | {"CODEX_API_KEY": "private-test-key-123456"}
    log = tmp_path / "check.log"
    result = ProcessRunner().run(
        [sys.executable, "-c", "import os; print(os.environ['CODEX_API_KEY'])"],
        cwd=tmp_path,
        env=env,
        log_path=log,
    )
    assert "private-test-key" not in result.stdout
    assert "private-test-key" not in log.read_text()
    assert "[REDACTED]" in log.read_text()


def test_exact_bytes_are_available_for_git_artifact_hashes(tmp_path):
    result = ProcessRunner().run(
        [sys.executable, "-c", "import sys; sys.stdout.buffer.write(bytes([255,10]))"],
        cwd=tmp_path,
    )
    assert result.stdout_bytes == b"\xff\n"


@pytest.mark.parametrize("interrupt", [False, True])
def test_timeout_and_guard_cancel_descendants_and_preserve_logs(tmp_path, interrupt):
    marker = tmp_path / "started"
    survivor = tmp_path / "survivor"
    child = (
        "import time,signal,pathlib; signal.signal(signal.SIGTERM,signal.SIG_IGN); "
        f"time.sleep(1); pathlib.Path({str(survivor)!r}).touch()"
    )
    parent = (
        "import sys,subprocess,time,pathlib; "
        f"subprocess.Popen([sys.executable,'-c',{child!r}]); "
        f"pathlib.Path({str(marker)!r}).touch(); "
        "print('stage began',flush=True); time.sleep(60)"
    )

    def guard():
        if marker.exists():
            raise Interrupted("database connection lost")

    runner = ProcessRunner(guard=guard if interrupt else None)
    with pytest.raises(Interrupted if interrupt else Blocked):
        runner.run(
            [sys.executable, "-c", parent],
            cwd=tmp_path,
            timeout=0.3,
            log_path=tmp_path / "stage.log",
        )
    time.sleep(1)
    assert not survivor.exists()
    assert "stage began" in (tmp_path / "stage.log").read_text()


def test_check_false_preserves_failure_code_and_check_true_blocks(tmp_path):
    command = [sys.executable, "-c", "raise SystemExit(7)"]
    assert ProcessRunner().run(command, cwd=tmp_path).returncode == 7
    with pytest.raises(Blocked, match="Command failed \\(7\\)"):
        ProcessRunner().run(command, cwd=tmp_path, check=True)


def test_guard_rejects_launch_before_side_effect(tmp_path):
    def guard():
        raise Interrupted("lost")

    with pytest.raises(Interrupted):
        ProcessRunner(guard).run(["touch", str(tmp_path / "unexpected")], cwd=tmp_path)
    assert not (tmp_path / "unexpected").exists()
