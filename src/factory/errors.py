"""Exit-code semantics (design §6).

0  done / continue            -> normal return
1  failed                     -> FactoryError (harness error, check failed, invariant violated;
                                 fix and re-run the same command)
2  needs human (a gate)       -> NeedsHuman
"""

from __future__ import annotations


class FactoryError(Exception):
    """Exit 1. `message` is printed to stderr; `hint` (optional) follows on its own line."""

    exit_code = 1

    def __init__(self, message: str, hint: str | None = None):
        super().__init__(message)
        self.message = message
        self.hint = hint


class HarnessError(FactoryError):
    """Exit 1. A harness subprocess failed: non-zero exit, timeout, unparseable or schema-invalid output.

    `transcript_path` is always set when the process ran at all, so the operator can inspect it.
    """

    def __init__(self, message: str, transcript_path=None, hint: str | None = None):
        super().__init__(message, hint)
        self.transcript_path = transcript_path


class GateViolation(FactoryError):
    """Exit 1. A deterministic gate or allowed-edit rule failed (design §9). `paths` names offenders."""

    def __init__(self, message: str, paths: list[str] | None = None, hint: str | None = None):
        super().__init__(message, hint)
        self.paths = paths or []


class NeedsHuman(Exception):
    """Exit 2. `gate` is one of the design §11 gate names:
    open_questions | baseline_failing | no_progress | rounds_exhausted
    `what_clears_it` is the operator-facing sentence used in the PR comment and on stderr.
    """

    exit_code = 2

    def __init__(self, gate: str, what_clears_it: str):
        super().__init__(f"needs_human:{gate}")
        self.gate = gate
        self.what_clears_it = what_clears_it

    @property
    def outcome(self) -> str:
        return f"needs_human:{self.gate}"
