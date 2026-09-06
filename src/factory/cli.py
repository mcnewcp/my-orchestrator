"""`factory` console script (design §6).

Global flags: --harness claude|codex, --auth subscription|api, --model, --force. poll accepts none of them (it uses
the config). --force is accepted only for spec, plan and build; on any other command it is a FactoryError.
Exit codes: 0 done/continue; 1 FactoryError; 2 NeedsHuman. Unexpected exceptions -> traceback + 1.

Commands: init doctor spec accept plan build review fix finalize run poll status dismiss abandon version

Dispatch: discover Repo from cwd; init needs no config (it writes it); everything else load_config + validate_overrides.
Issue commands build one Context, call stages.prepare(ctx, ...) once with the per-command policy (spec/run
need_state=False; abandon commit_operator_edits=False; status does not prepare), dispatch, and clear the RunLock in a
finally. `run` uses stages.run_issue semantics inline; `poll` passes a closure over stages.run_issue.
NeedsHuman prints "needs human: <gate>\n<what clears it>" to stderr and returns 2.
FactoryError prints "error: <message>" (+ hint, + "transcript: <path>" for HarnessError) and returns 1.
Progress lines go to stderr; only `status` and `version` write to stdout.
"""

from __future__ import annotations

import argparse
import sys


def build_parser() -> argparse.ArgumentParser:
    raise NotImplementedError


def main(argv: list[str] | None = None) -> int:
    raise NotImplementedError


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
