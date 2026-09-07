"""The explicit v0 workflow. Only this worker-side module changes target Git/GitHub."""

import fnmatch
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from factory.agents import AgentRunner, agent_environment, check_environment, prompt_text
from factory.config import Config, canonical_json, digest, file_hash, image_identity
from factory.errors import Blocked, FactoryError
from factory.git_ops import GitOps
from factory.github import GitHub
from factory.process import ProcessRunner

POLICY_PATHS = (
    "AGENTS.md",
    "**/AGENTS.md",
    "CLAUDE.md",
    "**/CLAUDE.md",
    "REVIEW.md",
    "**/REVIEW.md",
    "factory.toml",
    ".github",
    ".github/**",
    ".claude",
    ".claude/**",
    ".codex",
    ".codex/**",
    ".gitmodules",
    ".gitattributes",
    ".gitignore",
)


def assert_decisions_resolved(spec: str) -> None:
    match = re.search(r"(?im)^## Unresolved decisions\s*\n(.*?)(?=^## |\Z)", spec, re.S | re.M)
    if not match or match.group(1).strip().lower() not in {"none", "none."}:
        raise Blocked("unresolved_decisions: spec must have '## Unresolved decisions' with 'None.'")


def approved_hashes(workspace: Path, issue: int) -> dict[str, str]:
    docs = workspace / "work" / str(issue)
    assert_decisions_resolved((docs / "spec.md").read_text())
    return {name: file_hash(docs / name) for name in ("spec.md", "plan.md")}


def is_protected(path: str, patterns: list[str] | tuple[str, ...]) -> bool:
    return any(
        path == pattern.rstrip("/")
        or path.startswith(pattern.rstrip("/") + "/")
        or fnmatch.fnmatchcase(path, pattern)
        for pattern in patterns
    )


class Workflow:
    def __init__(self, state, config: Config, runner=None, agents=None, git=None, github=None):
        self.state = state
        self.config = config
        self.runner = runner or ProcessRunner()
        self.agents = agents or AgentRunner(self.runner)
        self.git = git or GitOps(config.checkout, runner=self.runner)
        self.github = github or GitHub(config.repo, runner=self.runner)

    def _update(self, run: dict, event: str, **fields) -> dict:
        return self.state.update(run["id"], event, **fields)

    def _metadata(self, run: dict, **values) -> dict:
        return {**(run.get("metadata") or {}), **values}

    def _evidence(self, run: dict, name: str, value) -> Path:
        path = Path(run["artifacts"]) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        # The database owns workflow state; these files are evidence, never a second queue.
        path.write_text(json.dumps(value, indent=2, default=str) + "\n")
        return path

    def _config(self, run: dict) -> Config:
        return Config.model_validate(run["frozen"]["config"]) if run.get("frozen") else self.config

    def _inputs_hash(self, run: dict) -> str:
        return digest(canonical_json(run["frozen"]))

    def _check_inputs(self, run: dict) -> None:
        if run["image"] != image_identity():
            raise Blocked("image_changed: resume requires the recorded image identity")
        if run.get("frozen"):
            path = Path(run["artifacts"]) / "inputs.json"
            if not path.is_file() or json.loads(path.read_text()) != run["frozen"]:
                raise Blocked("frozen_inputs_changed: restore the recorded immutable inputs")
        if run["repo"] != self.config.repo:
            raise Blocked("repository_changed: worker config does not match this run")

    def process(self, run: dict) -> None:
        self._check_inputs(run)
        if run["next_stage"] == "supersede":
            old = self.state.get(run["supersedes"])
            pr = self.github.find_pr(old["branch"])
            if pr and pr["state"].upper() == "OPEN":
                raise Blocked(f"open_pr: close {pr['url']} before superseding")
            self._update(old, "superseded", state="superseded", superseded_by=run["id"])
            run = self._update(run, "supersession_completed", next_stage="prepare")
        if run["next_stage"] == "prepare":
            self._prepare(run)
            return
        if run["next_stage"] == "approve":
            run = self._approve(run)
        else:
            run = self._recover(run)
        self._verify_approval(run)
        while True:
            stage = run["next_stage"]
            if stage == "implement":
                run = self._implement(run)
            elif stage == "check":
                run = self._check(run)
            elif stage == "review":
                run = self._review(run)
            elif stage == "publish":
                self._publish(run)
                return
            else:
                raise Blocked(f"invalid_stage: {stage}")

    def _prepare(self, run: dict) -> None:
        cfg = self._config(run)
        agent_environment(run["engine"], run["auth"])
        self.git.validate(cfg.base_branch, cfg.repo)
        if not run.get("frozen"):
            for name in ("AGENTS.md", "CLAUDE.md", "REVIEW.md"):
                if not (cfg.checkout / name).is_file():
                    raise Blocked(
                        f"missing_guidance: owner must review and commit {name}; see examples/"
                    )
            base_sha = self.git.fetch_base(cfg.base_branch)
            issue = self.github.issue(run["issue"])
            frozen = {
                "config": cfg.model_dump(mode="json"),
                "prompts": {role: prompt_text(role) for role in ("prepare", "implement", "review")},
                "policies": {
                    name: self.git.show(cfg.checkout, base_sha, name).decode()
                    for name in ("AGENTS.md", "CLAUDE.md", "REVIEW.md")
                },
                "issue": issue,
            }
            frozen["hashes"] = {
                "config": digest(canonical_json(frozen["config"])),
                **{f"prompt:{k}": digest(v.encode()) for k, v in frozen["prompts"].items()},
                **{f"policy:{k}": digest(v.encode()) for k, v in frozen["policies"].items()},
            }
            self._evidence(run, "inputs.json", frozen)
            run = self._update(run, "inputs_frozen", frozen=frozen, base_sha=base_sha)
        work = Path(run["workspace"])
        self.git.create_worktree(work, run["branch"], run["base_sha"])
        # A completed controller preparation commit can be adopted after an interrupted DB write.
        adopted = self.git.checkpoint(work, run["id"], "prepare")
        if adopted:
            self._verify_preparation_checkpoint(run, adopted)
            self._update(
                run,
                "preparation_adopted",
                state="awaiting_approval",
                next_stage="approve",
                checkpoint_sha=adopted,
                prepared_sha=adopted,
            )
            return
        if self.git.head(work) != run["base_sha"]:
            raise Blocked("unexpected_commit: preparation workspace no longer matches base")
        if self.git.status(work) or self.git.untracked_paths(work):
            archive = self.git.preserve_and_restore(
                work, run["base_sha"], Path(run["artifacts"]) / "recovery"
            )
            run = self._update(
                run,
                "partial_changes_preserved",
                metadata=self._metadata(run, recovery_archive=str(archive)),
            )
        baseline = self._run_checks(run, "baseline", run["base_sha"])
        self._evidence(run, "baseline.json", baseline)
        if not all(c["returncode"] == 0 for c in baseline):
            raise Blocked(
                "baseline_failed: repair the environment or supersede with a healthy base"
            )
        issue = run["frozen"]["issue"]
        prompt = self._prompt(run, "prepare", f"Frozen issue:\n{json.dumps(issue)}")
        output = self.agents.run(
            "prepare",
            run["engine"],
            run["auth"],
            work,
            self._session_path(run, "prepare"),
            prompt,
            cfg.stage_timeout,
        )
        self._unchanged(run, run["base_sha"])
        docs = work / "work" / str(run["issue"])
        if docs.exists():
            raise Blocked(
                "existing_artifacts: work/<issue> already exists at base; choose fresh scope"
            )
        docs.mkdir(parents=True)
        (docs / "intent.md").write_text(
            f"# {issue['title']}\n\n{issue['url']}\n\nRetrieved: {issue['retrieved_at']}\n\n"
            + (issue.get("body") or "")
            + "\n"
        )
        spec = re.split(r"(?im)^## Unresolved decisions\s*$", output.spec)[0].rstrip()
        questions = "\n".join(f"- {q}" for q in output.unresolved_decisions) or "None."
        (docs / "spec.md").write_text(f"{spec}\n\n## Unresolved decisions\n{questions}\n")
        (docs / "plan.md").write_text(output.plan.rstrip() + "\n")
        run = self._update(
            run,
            "preparation_commit_requested",
            metadata=self._metadata(
                run,
                preparation_hashes={
                    f"work/{run['issue']}/{name}": file_hash(docs / name)
                    for name in ("intent.md", "spec.md", "plan.md")
                },
            ),
        )
        sha = self.git.commit(
            work, f"Prepare issue #{run['issue']}", run_id=run["id"], stage="prepare"
        )
        self._verify_preparation_checkpoint(run, sha)
        self._update(
            run,
            "prepared",
            state="awaiting_approval",
            next_stage="approve",
            checkpoint_sha=sha,
            prepared_sha=sha,
        )

    def _verify_preparation_checkpoint(self, run: dict, sha: str) -> None:
        work = Path(run["workspace"])
        expected = (run.get("metadata") or {}).get("preparation_hashes") or {}
        documents = {f"work/{run['issue']}/{name}" for name in ("intent.md", "spec.md", "plan.md")}
        if set(expected) != documents or self.git.parent(work, sha) != run["base_sha"]:
            raise Blocked("unexpected_commit: preparation checkpoint has no matching provenance")
        self._unchanged(run, sha)
        committed = self.git.changed_paths(work, run["base_sha"]) - self.git.untracked_paths(work)
        if committed != documents or any(
            digest(self.git.show(work, sha, name)) != expected[name] for name in documents
        ):
            raise Blocked("unexpected_commit: preparation checkpoint contents have changed")

    def _approve(self, run: dict) -> dict:
        work = Path(run["workspace"])
        if approved_hashes(work, run["issue"]) != run["approval"]["hashes"]:
            raise Blocked("approval_changed: queued document contents no longer match approval")
        allowed = {f"work/{run['issue']}/{name}" for name in ("spec.md", "plan.md")}
        if self.git.changed_paths(work, run["prepared_sha"]) - allowed:
            raise Blocked("unexpected_edits: only spec.md and plan.md can change before approval")
        adopted = self.git.checkpoint(work, run["id"], "approve")
        if adopted and self.git.parent(work, adopted) != run["prepared_sha"]:
            raise Blocked("unexpected_commit: approval checkpoint parent has changed")
        if not adopted and self.git.head(work) != run["prepared_sha"]:
            raise Blocked("unexpected_commit: approval requires the prepared checkpoint")
        sha = adopted or self.git.commit(
            work, f"Approve issue #{run['issue']}", run_id=run["id"], stage="approve"
        )
        for name, expected in run["approval"]["hashes"].items():
            if digest(self.git.show(work, sha, f"work/{run['issue']}/{name}")) != expected:
                raise Blocked(
                    "approval_changed: committed approval does not match submitted hashes"
                )
        return self._update(
            run,
            "approved",
            approval_sha=sha,
            checkpoint_sha=sha,
            next_stage="implement",
            metadata=self._metadata(run, active_stage=None),
        )

    def _verify_approval(self, run: dict) -> None:
        if not run.get("approval_sha") or not run.get("approval"):
            raise Blocked("missing_approval")
        work = Path(run["workspace"])
        for name, expected in run["approval"]["hashes"].items():
            if (
                digest(self.git.show(work, run["approval_sha"], f"work/{run['issue']}/{name}"))
                != expected
            ):
                raise Blocked("approval_changed: recorded approval commit does not match hashes")
        if (
            file_hash(work / "work" / str(run["issue"]) / "spec.md")
            != run["approval"]["hashes"]["spec.md"]
        ):
            raise Blocked("requirements_changed: approved specification was modified")

    def _recover(self, run: dict) -> dict:
        work = Path(run["workspace"])
        checkpoint = run.get("checkpoint_sha")
        if not checkpoint or not work.is_dir():
            raise Blocked("missing_checkpoint: workspace must be restored before resume")
        head = self.git.head(work)
        active = (run.get("metadata") or {}).get("active_stage")
        if head != checkpoint:
            adopted = self.git.checkpoint(work, run["id"], f"candidate:{run['attempt_count']}")
            if adopted and active == "implement":
                if self.git.parent(work, adopted) != checkpoint:
                    raise Blocked("unexpected_commit: candidate parent is not the checkpoint")
                self._protect(run, checkpoint, allow_plan=True)
                plan = work / "work" / str(run["issue"]) / "plan.md"
                if file_hash(plan) != run["metadata"].get("controller_plan_hash"):
                    raise Blocked(
                        "unexpected_edits: candidate plan does not match controller checkpoint"
                    )
                self.state.finish_attempt(
                    run["id"],
                    run["attempt_count"],
                    candidate_sha=adopted,
                    stage="check",
                    status="running",
                )
                run = self._update(
                    run,
                    "candidate_adopted",
                    candidate_sha=adopted,
                    checkpoint_sha=adopted,
                    next_stage="check",
                )
                checkpoint = adopted
            else:
                raise Blocked("unexpected_commit: cannot safely recover unrecorded history")
        if self.git.status(work) or self._unexpected_untracked(run):
            if active not in {"implement", "check", "review"}:
                raise Blocked("unexpected_edits: workspace changed outside an interrupted stage")
            archive = self.git.preserve_and_restore(
                work, checkpoint, Path(run["artifacts"]) / "recovery"
            )
            run = self._update(
                run,
                "partial_changes_preserved",
                metadata=self._metadata(run, recovery_archive=str(archive)),
            )
        self._unchanged(run, checkpoint)
        return run

    def _prompt(self, run: dict, role: str, context: str) -> str:
        return (
            run["frozen"]["prompts"][role]
            + "\n\n"
            + f"Run: {run['id']}\nIssue: {run['issue']}\n"
            + "Frozen review policy:\n"
            + run["frozen"]["policies"]["REVIEW.md"]
            + "\n\n"
            + context
        )

    def _documents(self, run: dict) -> str:
        docs = Path(run["workspace"]) / "work" / str(run["issue"])
        return "\n\n".join(
            f"{name}:\n{(docs / name).read_text()}" for name in ("intent.md", "spec.md", "plan.md")
        )

    def _session_path(self, run: dict, stage: str) -> Path:
        # New evidence directory on each invocation, including resumed read-only sessions.
        return Path(run["artifacts"]) / stage / str(uuid4())

    def _unexpected_untracked(self, run: dict) -> set[str]:
        allowed = self._config(run).allowed_untracked
        protected = self._protected_paths(run)
        return {
            p
            for p in self.git.untracked_paths(Path(run["workspace"]))
            if is_protected(p, protected) or not is_protected(p, allowed)
        }

    def _protected_paths(self, run: dict, *, allow_plan: bool = False) -> list[str]:
        protected = [
            *POLICY_PATHS,
            *self._config(run).protected_paths,
            f"work/{run['issue']}/intent.md",
            f"work/{run['issue']}/spec.md",
        ]
        if not allow_plan:
            protected.append(f"work/{run['issue']}/plan.md")
        return protected

    def _protect(self, run: dict, since: str, *, allow_plan: bool = False) -> None:
        protected = self._protected_paths(run, allow_plan=allow_plan)
        work = Path(run["workspace"])
        changed = self.git.changed_paths(work, since) | self.git.untracked_paths(work)
        forbidden = sorted(p for p in changed if is_protected(p, protected))
        if forbidden:
            raise Blocked("protected_paths_changed: " + ", ".join(forbidden))
        ignored = self._unexpected_untracked(run) - set(self.git.status(work))
        if ignored:
            raise Blocked("unexpected_ignored_files: " + ", ".join(sorted(ignored)))

    def _implement(self, run: dict) -> dict:
        cfg = self._config(run)
        work = Path(run["workspace"])
        self._unchanged(run, run["checkpoint_sha"])
        previous = self.state.attempts(run["id"])
        attempt = self.state.reserve_attempt(run["id"])
        run = self.state.get(run["id"])
        run = self._update(
            run, "implementation_started", metadata=self._metadata(run, active_stage="implement")
        )
        prompt = self._prompt(
            run,
            "implement",
            self._documents(run)
            + "\nPrior failure evidence:\n"
            + json.dumps(previous, default=str),
        )
        output = self.agents.run(
            "implement",
            run["engine"],
            run["auth"],
            work,
            self._session_path(run, f"attempt-{attempt['number']}/implement"),
            prompt,
            cfg.stage_timeout,
        )
        if self.git.head(work) != run["checkpoint_sha"]:
            raise Blocked("agent_commit: agents must not change Git history")
        self._protect(run, run["checkpoint_sha"])
        if output.unresolved_decisions:
            raise Blocked("unresolved_decisions: " + "; ".join(output.unresolved_decisions))
        if output.plan_deviations:
            plan = work / "work" / str(run["issue"]) / "plan.md"
            with plan.open("a") as out:
                out.write(f"\n\n## Implementation deviations — attempt {attempt['number']}\n")
                out.write("\n".join(f"- {item}" for item in output.plan_deviations) + "\n")
        run = self._update(
            run,
            "candidate_commit_requested",
            metadata=self._metadata(
                run, controller_plan_hash=file_hash(work / "work" / str(run["issue"]) / "plan.md")
            ),
        )
        sha = self.git.commit(
            work,
            f"Implement issue #{run['issue']} (attempt {attempt['number']})",
            run_id=run["id"],
            stage=f"candidate:{attempt['number']}",
        )
        self.state.finish_attempt(
            run["id"], attempt["number"], candidate_sha=sha, stage="check", status="running"
        )
        return self._update(
            run,
            "candidate_committed",
            candidate_sha=sha,
            checkpoint_sha=sha,
            next_stage="check",
            metadata=self._metadata(run, active_stage=None),
        )

    def _unchanged(self, run: dict, sha: str) -> None:
        work = Path(run["workspace"])
        untracked = self.git.untracked_paths(work)
        tracked_dirty = set(self.git.status(work)) - untracked
        if self.git.head(work) != sha or tracked_dirty or self._unexpected_untracked(run):
            raise Blocked(
                "candidate_changed: tracked or unexpected untracked changes invalidate evidence"
            )

    def _run_checks(self, run: dict, stage: str, sha: str) -> list[dict]:
        results = []
        evidence = self._session_path(run, stage)
        for check in self._config(run).checks:
            self._unchanged(run, sha)
            path = evidence / f"{check.name}.log"
            result = self.runner.run(
                check.command,
                cwd=Path(run["workspace"]),
                env=check_environment(),
                timeout=check.timeout,
                log_path=path,
            )
            results.append(
                {
                    "name": check.name,
                    "returncode": result.returncode,
                    "candidate_sha": sha,
                    "inputs_hash": self._inputs_hash(run),
                    "log": str(path),
                }
            )
            self._unchanged(run, sha)
        return results

    def _check(self, run: dict) -> dict:
        run = self._update(
            run, "checks_started", metadata=self._metadata(run, active_stage="check")
        )
        self._protect(run, run["approval_sha"], allow_plan=True)
        results = self._run_checks(
            run, f"attempt-{run['attempt_count']}/checks", run["candidate_sha"]
        )
        self._evidence(run, f"attempt-{run['attempt_count']}/checks.json", results)
        self.state.finish_attempt(
            run["id"], run["attempt_count"], checks=results, stage="review", status="running"
        )
        return self._update(
            run,
            "checks_completed",
            next_stage="review",
            metadata=self._metadata(run, active_stage=None),
        )

    def _review(self, run: dict) -> dict:
        cfg = self._config(run)
        sha = run["candidate_sha"]
        self._unchanged(run, sha)
        attempt = self.state.attempts(run["id"])[-1]
        checks = attempt["checks"]
        logs = "\n".join(f"{c['name']}:\n{Path(c['log']).read_text()[-40000:]}" for c in checks)
        context = (
            self._documents(run)
            + f"\nCandidate SHA: {sha}\n"
            + "Diff from approved baseline:\n"
            + self.git.diff(Path(run["workspace"]), run["approval_sha"], sha)
            + "\nCheck evidence:\n"
            + json.dumps(checks)
            + "\n"
            + logs
        )
        run = self._update(
            run, "review_started", metadata=self._metadata(run, active_stage="review")
        )
        output = self.agents.run(
            "review",
            run["engine"],
            run["auth"],
            Path(run["workspace"]),
            self._session_path(run, f"attempt-{run['attempt_count']}/review"),
            self._prompt(run, "review", context),
            cfg.stage_timeout,
        )
        self._unchanged(run, sha)
        self._verify_approval(run)
        review = {
            **output.model_dump(mode="json"),
            "candidate_sha": sha,
            "inputs_hash": self._inputs_hash(run),
        }
        self._evidence(run, f"attempt-{run['attempt_count']}/review.json", review)
        passed = all(check["returncode"] == 0 for check in checks)
        accepted = passed and output.decision == "accept"
        self.state.finish_attempt(
            run["id"],
            run["attempt_count"],
            review=review,
            findings=review["findings"],
            stage="complete",
            status="accepted" if accepted else "rejected",
            finished_at=datetime.now(UTC),
        )
        run = self._update(
            run,
            "review_completed",
            next_stage=(
                "review" if output.decision == "blocked" else "publish" if accepted else "implement"
            ),
            metadata=self._metadata(run, active_stage=None),
        )
        if output.decision == "blocked":
            raise Blocked("review_blocked: " + output.summary)
        if not accepted and run["attempt_count"] >= 3:
            raise Blocked("attempts_exhausted: three implementation attempts used")
        return run

    def _accepted(self, run: dict) -> dict:
        self._check_inputs(run)
        self._verify_approval(run)
        self._unchanged(run, run["candidate_sha"])
        self._protect(run, run["approval_sha"], allow_plan=True)
        attempts = self.state.attempts(run["id"])
        if not attempts:
            raise Blocked("missing_evidence")
        attempt = attempts[-1]
        review, checks = attempt.get("review") or {}, attempt.get("checks") or []
        sha, inputs_hash = run["candidate_sha"], self._inputs_hash(run)
        expected = {c.name for c in self._config(run).checks}
        if (
            attempt.get("candidate_sha") != sha
            or review.get("candidate_sha") != sha
            or review.get("inputs_hash") != inputs_hash
            or review.get("decision") != "accept"
            or any(f["severity"] == "important" for f in review.get("findings", []))
            or len(checks) != len(expected)
            or {c["name"] for c in checks} != expected
            or any(
                c["returncode"] != 0 or c["candidate_sha"] != sha or c["inputs_hash"] != inputs_hash
                for c in checks
            )
        ):
            raise Blocked(
                "stale_or_failed_evidence: acceptance must describe this exact candidate and inputs"
            )
        return attempt

    def _publish(self, run: dict) -> None:
        cfg = self._config(run)
        attempt = self._accepted(run)
        sha = run["candidate_sha"]
        self.git.push(Path(run["workspace"]), run["branch"])
        body = (
            f"<!-- factory-run:{run['id']} -->\nCloses #{run['issue']}\n\n"
            + f"Run: `{run['id']}`\nCandidate: `{sha}`\n"
            + f"Engine/auth: {run['engine']}/{run['auth']}\n"
            + f"Implementation attempts: {run['attempt_count']}/3\n\n"
            + "Acceptance coverage and scope:\n"
            + self.git.show(
                Path(run["workspace"]), run["approval_sha"], f"work/{run['issue']}/spec.md"
            ).decode()
            + "\n\nLocal checks:\n"
            + "\n".join(f"- {c['name']}: exit {c['returncode']}" for c in attempt["checks"])
            + "\n\nReview: "
            + attempt["review"]["summary"]
            + f"\n\nEvidence remains on the factory host: {cfg.host_path(run['artifacts'])}\n"
        )
        pr = self.github.create_draft(
            run["branch"], cfg.base_branch, run["frozen"]["issue"]["title"], body, run_id=run["id"]
        )
        run = self._update(run, "draft_published", pr=pr)
        self._assert_pr(run, self.github.pr(pr["number"]))
        ci = self.github.wait_ci(
            sha, cfg.required_ci, cfg.ci_timeout, poll_seconds=cfg.poll_seconds
        )
        self._evidence(run, "ci.json", ci)
        self._accepted(run)
        current = self.github.pr(pr["number"])
        self._assert_pr(run, current)
        if current["isDraft"]:
            self.github.ready(pr["number"], expected_sha=sha)
        current = self.github.pr(pr["number"])
        self._assert_pr(run, current)
        if current["isDraft"]:
            raise Blocked("pr_still_draft")
        # CI is re-read after conversion too; a changed head never gets recorded ready.
        self.github.wait_ci(sha, cfg.required_ci, cfg.ci_timeout, poll_seconds=cfg.poll_seconds)
        self._accepted(run)
        self._assert_pr(run, self.github.pr(pr["number"]))
        self._update(
            run,
            "ready",
            state="ready",
            next_stage="done",
            pr=current,
            metadata=self._metadata(run, active_stage=None),
        )

    def _assert_pr(self, run: dict, pr: dict) -> None:
        if (
            pr["headRefOid"] != run["candidate_sha"]
            or pr["state"].upper() != "OPEN"
            or pr["headRefName"] != run["branch"]
            or pr["baseRefName"] != self._config(run).base_branch
            or f"<!-- factory-run:{run['id']} -->" not in pr.get("body", "")
        ):
            raise Blocked(
                "pr_changed: PR must be open at the accepted head, base, and run identity"
            )

    def reconcile_ready(self) -> None:
        for run in self.state.list_runs():
            if run["repo"] != self.config.repo or run["state"] != "ready" or not run.get("pr"):
                continue
            try:
                pr = self.github.pr(run["pr"]["number"])
                if pr["state"].upper() in {"CLOSED", "MERGED"}:
                    self._update(run, "pr_closed", state="closed", pr=pr)
            except FactoryError:
                # A transient read failure must not release a reservation.
                continue
