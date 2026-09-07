"""Bounded subprocesses with durable output and worker-lifetime cancellation."""

from __future__ import annotations

import os
import selectors
import signal
import subprocess
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from factory.errors import Blocked


@dataclass(frozen=True)
class ProcessResult:
    returncode: int
    stdout: str
    stderr: str
    stdout_bytes: bytes | None = None


def _sensitive_values(env: Mapping[str, str]) -> list[str]:
    markers = ("KEY", "TOKEN", "SECRET", "PASSWORD", "DATABASE_URL")
    return sorted(
        {v for k, v in env.items() if len(v) >= 8 and any(m in k.upper() for m in markers)},
        key=len,
        reverse=True,
    )


def _redact(text: str, secrets: list[str]) -> str:
    for value in secrets:
        text = text.replace(value, "[REDACTED]")
    return text


class ProcessRunner:
    """A guard failure cancels the whole process group, never just its leader."""

    def __init__(self, guard: Callable[[], None] | None = None):
        self.guard = guard

    @property
    def check_alive(self) -> Callable[[], None] | None:
        return self.guard

    @check_alive.setter
    def check_alive(self, value: Callable[[], None] | None) -> None:
        self.guard = value

    @staticmethod
    def _stop_group(process: subprocess.Popen) -> None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            process.wait()
            return
        try:
            process.wait(timeout=0.3)
        except subprocess.TimeoutExpired:
            pass
        # Descendants can survive after their parent exits or ignore SIGTERM.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()

    def run(
        self,
        argv: Sequence[str],
        cwd: Path,
        env: Mapping[str, str] | None = None,
        timeout: int | float = 600,
        log_path: Path | None = None,
        input_text: str | None = None,
        check: bool = False,
    ) -> ProcessResult:
        if not argv or timeout <= 0:
            raise ValueError("A command and positive timeout are required")
        if self.guard:
            self.guard()
        effective_env = dict(os.environ if env is None else env)
        secrets = _sensitive_values(dict(os.environ) | effective_env)
        output: dict[str, bytearray] = {"stdout": bytearray(), "stderr": bytearray()}
        if log_path:
            log_path.parent.mkdir(parents=True, exist_ok=True)
        # File-backed stdin cannot deadlock while a verbose child fills its output pipes.
        with tempfile.TemporaryFile() as stdin:
            if input_text is not None:
                stdin.write(input_text.encode())
                stdin.seek(0)
            try:
                process = subprocess.Popen(
                    list(argv),
                    cwd=cwd,
                    env=effective_env,
                    stdin=stdin,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    start_new_session=True,
                )
            except OSError as exc:
                raise Blocked(f"Cannot launch {Path(argv[0]).name}: {exc.strerror}") from exc
            started = time.monotonic()
            pending: dict[str, str] = {"stdout": "", "stderr": ""}
            longest_secret = max((len(v) for v in secrets), default=0)
            log = log_path.open("w", encoding="utf-8") if log_path else None

            def write_log(stream: str, chunk: bytes = b"", final: bool = False) -> None:
                if not log:
                    return
                pending[stream] += chunk.decode("utf-8", errors="replace")
                # Hold a trailing line (and a secret-sized suffix) across pipe reads.
                end = len(pending[stream]) if final else pending[stream].rfind("\n") + 1
                if not final:
                    end = min(end, max(0, len(pending[stream]) - longest_secret))
                    end = pending[stream].rfind("\n", 0, end) + 1
                if end:
                    log.write(f"[{stream}] " + _redact(pending[stream][:end], secrets))
                    pending[stream] = pending[stream][end:]
                    log.flush()

            try:
                with selectors.DefaultSelector() as selector:
                    for name, pipe in (("stdout", process.stdout), ("stderr", process.stderr)):
                        assert pipe is not None
                        os.set_blocking(pipe.fileno(), False)
                        selector.register(pipe, selectors.EVENT_READ, name)
                    while selector.get_map() or process.poll() is None:
                        if self.guard:
                            self.guard()
                        if time.monotonic() - started >= timeout:
                            raise Blocked(f"Timeout after {timeout:g}s: {Path(argv[0]).name}")
                        for key, _ in selector.select(timeout=0.1):
                            chunk = os.read(key.fd, 65536)
                            if not chunk:
                                selector.unregister(key.fileobj)
                                continue
                            output[key.data].extend(chunk)
                            write_log(key.data, chunk)
                    process.wait()
                if self.guard:
                    self.guard()
            finally:
                self._stop_group(process)
                # Persist anything emitted immediately before cancellation.
                for name, pipe in (("stdout", process.stdout), ("stderr", process.stderr)):
                    assert pipe is not None
                    try:
                        tail = pipe.read() or b""
                        output[name].extend(tail)
                        write_log(name, tail, final=True)
                    finally:
                        pipe.close()
                if log:
                    log.close()
        result = ProcessResult(
            process.returncode,
            _redact(output["stdout"].decode("utf-8", errors="replace"), secrets),
            _redact(output["stderr"].decode("utf-8", errors="replace"), secrets),
            bytes(output["stdout"]),
        )
        if check and result.returncode:
            raise Blocked(f"Command failed ({result.returncode}): {Path(argv[0]).name}")
        return result
