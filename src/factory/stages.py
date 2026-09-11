"""The stage machine. Only this process assigns verdicts or publishes changes."""

import copy
import hashlib
import json
import re
from pathlib import Path

from .checks import PROTECTED_PATHS, changed_paths, filtered_env, run_checks, validate_edits
from .config import ROLES, resolve_role
from .harness import make_harness
from .locks import record_safe_head
from .state import atomic_json, load_json, merge_ledger, open_important, utcnow


GATES = {
    "open_questions": "Resolve .factory/issues/{issue}/spec.md, then run factory accept {issue}.",
    "baseline_failing": "Fix the baseline checks by hand and commit on factory/{issue}.",
    "no_progress": "Fix and commit by hand, or dismiss a finding with a reason.",
    "rounds_exhausted": "Fix and commit by hand, or dismiss the remaining Important findings.",
}


class NeedsHuman(RuntimeError):
    pass


class Engine:
    def __init__(self, repo, github, config, issue, *, harness=None, auth=None, model=None, effort=None):
        self.repo, self.github, self.config, self.issue = repo, github, config, issue
        self.options = config["factory"]
        self.auth = auth or self.options["auth"]
        self.role_settings = {role: resolve_role(config, role, harness=harness, model=model, effort=effort)
                              for role in ROLES}
        self.timeout = self.options["stage_timeout_min"] * 60
        self.cwd = None
        self.state = {}
        self.ledger = {"findings": []}
        self.force_push = False

    @property
    def work(self):
        return self.repo.issue_dir(self.issue)

    def prepare(self, *, create=True):
        self.repo.fetch()
        self.cwd = self.repo.worktree(self.issue, create=create)
        if self.cwd is None:
            raise RuntimeError(f"no run for issue {self.issue}; start with factory spec {self.issue}")
        self.repo.validate_clean(self.cwd, self.issue)
        self.repo.sync(self.cwd, self.issue)
        self.reload()
        for name, stage in self.state.get("stages", {}).items():
            try:
                self.repo.resolve(self.cwd, stage["commit"])
            except (RuntimeError, KeyError) as exc:
                raise RuntimeError(f"recorded {name} commit is not reachable from HEAD") from exc
        record_safe_head(self.repo, self.issue, self.head(), state=self.state, ledger=self.ledger)
        return self

    def reload(self):
        self.state = load_json(self.work / "state.json", {})
        self.ledger = load_json(self.work / "findings.json", {"findings": []})
        if self.state and self.state.get("issue", {}).get("number") != self.issue:
            raise RuntimeError("state issue does not match branch")

    def head(self):
        return self.repo.head(self.cwd)

    def is_head(self, ref):
        return bool(ref) and self.head() == self.repo.resolve(self.cwd, ref)

    def save(self):
        atomic_json(self.work / "findings.json", self.ledger)
        atomic_json(self.work / "state.json", self.state)
        if not self.force_push:
            record_safe_head(self.repo, self.issue, self.head(), state=self.state, ledger=self.ledger)

    def render_documents(self):
        return "\n\n".join(
            f"<details>\n<summary>{title}</summary>\n\n{self.text(name).rstrip()}\n\n</details>"
            for title, name in (("Specification", "spec.md"), ("Plan", "plan.md"))
        )

    def publish(self):
        if self.force_push and self.state.get("pr"):
            self.github.draft(self.state["pr"]["number"])
        self.repo.push(self.issue, force=self.force_push)
        self.force_push = False
        self.save()
        body = f"Closes #{self.issue}\n\n" + self.render_documents()
        body_digest = hashlib.sha256(body.encode()).hexdigest()
        # GitHub cannot open a PR until the branch contains a code diff.
        if "build" in self.state.get("stages", {}) and not self.state.get("pr"):
            pr = self.github.ensure_pr(
                self.issue, self.state["branch"], self.state["base"]["branch"],
                f"Factory #{self.issue}: {self.state['issue'].get('title', 'implementation')}",
                body,
            )
            self.state["pr"] = pr
            self.save()
        # Also refresh a PR recovered after an interrupted creation.
        if self.state.get("pr") and self.state.get("pr_body_sha256") != body_digest:
            self.github.update_pr(self.state["pr"]["number"], body)
            self.state["pr_body_sha256"] = body_digest
            self.save()

    def clear_outcome(self):
        self.state["outcome"] = None
        self.state["outcome_sha"] = None
        self.state.pop("gate_notice", None)

    def gate_comment(self, reason):
        self.publish()
        notice = self.state.get("gate_notice", {})
        if self.state.get("pr") and not notice.get("sent"):
            head = self.repo.resolve(self.cwd, notice.get("sha", self.state["outcome_sha"]))
            body = (f"Factory needs human input: **{reason}**.\n\n"
                    + GATES[reason].format(issue=self.issue) + "\n\n" + self.render_ledger())
            self.github.comment(self.state["pr"]["number"], body, f"factory:gate:{reason}:{head}")
            self.state["gate_notice"] = {"sha": head, "sent": True}
            self.save()

    def gate(self, reason):
        outcome = f"needs_human:{reason}"
        if self.state.get("outcome") != outcome or not self.is_head(self.state.get("outcome_sha")):
            self.state.update(outcome=outcome, outcome_sha=self.head())
            self.state["gate_notice"] = {"sha": self.head(), "sent": False}
            self.save()
        self.gate_comment(reason)
        raise NeedsHuman(f"{reason}: {GATES[reason].format(issue=self.issue)}")

    def parked(self):
        outcome = self.state.get("outcome") or ""
        if outcome.startswith("needs_human:") and self.is_head(self.state.get("outcome_sha")):
            self.gate(outcome.split(":", 1)[1])
        if outcome.startswith("needs_human:"):
            self.clear_outcome()

    def require(self, stage):
        if stage not in self.state.get("stages", {}):
            raise RuntimeError(f"{stage} must complete first")

    def text(self, name):
        path = self.work / name
        return path.read_text() if path.exists() else ""

    def prompt(self, stage, number):
        resources = Path(__file__).parent
        diff = self.repo.diff(self.cwd, self.state["base"]["sha"]) if stage == "review" else ""
        if diff:
            path = self.work / f"review-{number}.diff"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(diff)
        logs = sorted((self.work / "checks").glob("*.log"), key=lambda p: p.stat().st_mtime) if (self.work / "checks").exists() else []
        values = {
            "intent": self.text("intent.md"), "spec": self.text("spec.md"),
            "plan": self.text("plan.md"), "diff": diff,
            "checks": logs[-1].read_text() if logs else "No checks yet.",
            "review_policy": (self.repo.root / "REVIEW.md").read_text(),
            "ledger": json.dumps(self.ledger, indent=2),
            "findings": json.dumps(open_important(self.ledger), indent=2),
        }
        template = (resources / "roles" / f"{stage}.md").read_text()
        rendered = re.sub(r"\{(" + "|".join(values) + r")\}", lambda m: values[m[1]], template)
        policy = (
            f"\n\n---\nIssue: {self.issue}. Artifacts: {self.work}/.\n"
            f"Configured checks: {json.dumps(self.options['checks'])}\n"
            f"Protected paths: {json.dumps([*PROTECTED_PATHS, *self.options['protected_paths']])}\n"
            + ("Mode: read-only; create, modify, or delete nothing.\n"
               if stage in ("spec", "plan", "review")
               else "Mode: write; edit only files inside this worktree.\n")
        )
        if stage == "fix":
            policy += f"Test paths: {json.dumps(self.options['test_paths'])}\n"
        path = self.work / "prompts" / f"{stage}-{number}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered + policy)
        return path, resources / "schemas" / f"{stage}.json"

    def call(self, stage, number=1):
        settings = self.role_settings[stage]
        name = settings["harness"]
        filtered_env(harness=name, auth=self.auth)
        print(f"Issue #{self.issue}: {stage} using {name} ({self.auth})", flush=True)
        record_safe_head(self.repo, self.issue, stage=stage)
        prompt, schema = self.prompt(stage, number)
        before = self.snapshot_dirty()
        artifacts = self.snapshot_factory()
        head = self.head()
        adapter = make_harness(name, self.repo.local_dir / "transcripts")
        try:
            result = adapter.run(cwd=self.cwd, prompt_file=prompt, schema_file=schema,
                                 mode="write" if stage in ("build", "fix") else "read",
                                 model=settings["model"] or None, effort=settings["effort"] or None, auth=self.auth,
                                 env=filtered_env(harness=name, auth=self.auth), timeout_s=self.timeout)
        finally:
            changed_artifacts = self.snapshot_factory()
            if artifacts != changed_artifacts:
                self.restore_factory(artifacts, changed_artifacts)
            if self.head() != head:
                self.repo.reset(self.cwd, head)
                raise RuntimeError("harness changed Git HEAD; factory owns commits")
            if artifacts != changed_artifacts:
                raise ValueError(f"{stage} modified forbidden paths under .factory/")
        if stage in ("spec", "plan", "review") and before != self.snapshot_dirty():
            raise RuntimeError("read-mode harness modified the worktree")
        if stage in ("build", "fix"):
            dirty = self.snapshot_dirty()
            paths = [p for p in dirty if p not in before or dirty[p] != before[p]]
            paths.extend(p for p in before if p not in dirty)
            validate_edits(paths, stage, self.issue, self.config, self.text("plan.md"))
        return result

    def snapshot_dirty(self):
        result = {}
        for name in changed_paths(self.cwd):
            p = self.cwd / name
            if p.is_symlink():
                result[name] = ("symlink", str(p.readlink()))
            elif p.is_file():
                result[name] = (p.stat().st_mode, p.read_bytes())
            else:
                result[name] = None
        return result

    def snapshot_factory(self, *, exclude=()):
        # The harness owns its transcripts. Everything else in .factory is
        # factory-owned, including ignored files inside the issue worktree.
        roots = [path for path in self.repo.local_dir.iterdir()
                 if path.name not in ("worktrees", "transcripts")]
        roots.append(self.cwd / ".factory")
        result = {}
        for root in roots:
            paths = [root]
            if root.is_dir() and not root.is_symlink():
                paths.extend(root.rglob("*"))
            for path in paths:
                if path in exclude:
                    continue
                if path.is_symlink():
                    result[path] = ("symlink", str(path.readlink()))
                elif path.is_file():
                    result[path] = (path.stat().st_mode, path.read_bytes())
                elif path.is_dir():
                    result[path] = ("directory", None)
        return result

    @staticmethod
    def restore_factory(before, after):
        for path in sorted(after, key=lambda p: len(p.parts), reverse=True):
            if path not in before or before[path][0] != after[path][0]:
                if path.is_dir() and not path.is_symlink():
                    path.rmdir()
                else:
                    path.unlink(missing_ok=True)
        for path, (kind, value) in sorted(before.items(), key=lambda item: len(item[0].parts)):
            if after.get(path) == (kind, value):
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            if kind == "directory":
                path.mkdir(exist_ok=True)
            elif kind == "symlink":
                path.unlink(missing_ok=True)
                path.symlink_to(value)
            else:
                path.write_bytes(value)
                path.chmod(kind)

    def provenance(self, stage, result):
        settings = self.role_settings[stage]
        return {"harness": settings["harness"], "model": settings["model"] or "CLI default",
                "effort": settings["effort"] or "CLI default", "cli_version": result.cli_version,
                "auth": self.auth}

    def record_stage(self, stage, result):
        commit = self.repo.commit(self.cwd, f"factory({self.issue}): build") if stage == "build" else self.head()
        self.clear_outcome()
        self.state["stages"][stage] = {
            "commit": commit, "at": utcnow(), **self.provenance(stage, result),
        }
        self.save()
        self.publish()

    def check(self, stage, number):
        print(f"Issue #{self.issue}: {stage} checks", flush=True)
        record_safe_head(self.repo, self.issue, stage=f"{stage} checks")
        path = self.work / "checks" / f"{stage}-{number}.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        artifacts = self.snapshot_factory(exclude=(path,))
        head = self.head()
        try:
            passed = run_checks(self.cwd, self.options["checks"], path, self.timeout)
        finally:
            changed_artifacts = self.snapshot_factory(exclude=(path,))
            if artifacts != changed_artifacts:
                self.restore_factory(artifacts, changed_artifacts)
            if self.head() != head:
                self.repo.reset(self.cwd, head)
                raise RuntimeError("checks changed Git HEAD; factory owns commits")
            if artifacts != changed_artifacts:
                raise RuntimeError("checks modified factory files")
        if not passed:
            print(f"Checks failed; output: {path}")
        return passed

    def spec(self):
        if "spec" in self.state.get("stages", {}):
            self.publish()
            if self.state.get("spec_open_questions") and not self.state.get("spec_accepted"):
                self.gate("open_questions")
            return
        filtered_env(harness=self.role_settings["spec"]["harness"], auth=self.auth)
        issue = self.github.issue(self.issue)
        body = issue.get("body") or ""
        now = utcnow()
        digest = hashlib.sha256(body.encode()).hexdigest()
        self.state = {
            "issue": {"number": self.issue, "title": issue["title"], "url": issue["url"],
                      "snapshot_sha256": digest, "snapshot_at": now},
            "base": {"branch": self.options["base_branch"], "sha": self.head()},
            "branch": f"factory/{self.issue}", "stages": {}, "spec_open_questions": [],
            "spec_accepted": None, "reviews": [], "fixes": [], "fix_rounds": 0,
            "pr": getattr(self, "rewound_pr", None),
            "outcome": None, "outcome_sha": None,
        }
        self.work.mkdir(parents=True, exist_ok=True)
        metadata = {**self.state["issue"], "labels": issue.get("labels", [])}
        (self.work / "intent.md").write_text("<!-- Factory issue snapshot\n" + json.dumps(metadata, indent=2) + "\n-->\n\n" + body)
        result = self.call("spec")
        markdown = result.output["markdown"].strip()
        if not markdown:
            raise RuntimeError("spec markdown must not be empty")
        questions = result.output["open_questions"]
        (self.work / "spec.md").write_text(markdown + "\n\n## Open questions\n\n" + ("\n".join(f"- {q}" for q in questions) if questions else "None.") + "\n")
        self.state["spec_open_questions"] = questions
        if not questions:
            self.state["spec_accepted"] = {"by": "auto", "at": utcnow()}
        self.record_stage("spec", result)
        if questions:
            self.gate("open_questions")

    def accept(self):
        self.require("spec")
        if self.state.get("spec_accepted") and not self.state.get("outcome"):
            self.publish()
            return
        self.state["spec_accepted"] = {"by": "operator", "at": utcnow()}
        self.clear_outcome()
        self.save()
        self.publish()

    def plan(self):
        self.require("spec")
        if not self.state.get("spec_accepted"):
            self.gate("open_questions")
        if "plan" in self.state["stages"]:
            self.publish()
            return
        result = self.call("plan")
        markdown = result.output["markdown"]
        if not all(re.search(r"^" + re.escape(heading) + r"\s*$", markdown, re.M) for heading in ("## Files that change", "## Proof")):
            raise RuntimeError("plan requires ## Files that change and ## Proof")
        (self.work / "plan.md").write_text(markdown.rstrip() + "\n")
        self.record_stage("plan", result)

    def build(self):
        self.require("plan")
        if "build" in self.state["stages"]:
            self.publish()
            return
        filtered_env(harness=self.role_settings["build"]["harness"], auth=self.auth)
        before = self.snapshot_dirty()
        passed = self.check("baseline", 1)
        # Baseline checks may create caches, but must not change source or policy.
        # Retained operator artifacts from --force already existed before checks.
        after = self.snapshot_dirty()
        changed = [p for p in after if p not in before or after[p] != before[p]] + [p for p in before if p not in after]
        if changed:
            raise RuntimeError("baseline checks modified tracked or unignored files")
        if not passed:
            self.gate("baseline_failing")
        result = self.call("build")
        before = self.snapshot_dirty()
        if not self.check("build", 1):
            raise RuntimeError("build checks failed")
        after = self.snapshot_dirty()
        changed = [p for p in after if p not in before or after[p] != before[p]] + [p for p in before if p not in after]
        if changed:
            raise RuntimeError("checks modified candidate files; checks must not rewrite source")
        self.state["build_summary"] = result.output
        self.record_stage("build", result)

    def nit_cap(self):
        policy = (self.repo.root / "REVIEW.md").read_text()
        match = re.search(r"nit[_ -]cap\s*[:=]\s*(\d+)", policy, re.I)
        return int(match[1]) if match else 5

    def review(self):
        self.require("build")
        number = len(self.state["reviews"]) + 1
        reviewed = self.head()
        result = self.call("review", number)
        ledger, stats = merge_ledger(self.ledger, result.output, number, self.nit_cap())
        self.ledger = ledger
        atomic_json(self.work / f"review-{number}.json", result.output)
        previous_fixes = self.state["reviews"][-1].get("fix_rounds", 0) if self.state["reviews"] else 0
        after_fix = self.state["fix_rounds"] > previous_fixes
        self.state["reviews"].append({
            "round": number, "sha": reviewed, "reviewed_input_sha": reviewed,
            "fix_rounds": self.state["fix_rounds"], "at": utcnow(),
            **self.provenance("review", result), **stats,
        })
        self.clear_outcome()
        reason = None
        if stats["important_open"]:
            if after_fix and not stats["important_resolved"]:
                reason = "no_progress"
            elif self.state["fix_rounds"] >= self.options["max_fix_rounds"]:
                reason = "rounds_exhausted"
        if reason:
            self.state.update(outcome=f"needs_human:{reason}", outcome_sha=self.head())
            self.state["gate_notice"] = {"sha": self.head(), "sent": False}
        self.save()
        self.publish()
        self.review_comment(number)
        if reason:
            self.gate(reason)

    def review_comment(self, number):
        self.github.comment(self.state["pr"]["number"], f"Factory review {number}\n\n" + self.render_ledger(),
                            f"factory:review:{self.state['reviews'][number - 1]['sha']}:{number}")

    def fix(self):
        self.require("build")
        findings = open_important(self.ledger)
        if not findings:
            self.publish()
            return
        if not self.reviewed_head():
            raise RuntimeError("HEAD is not the reviewed commit; run factory review first")
        if self.state["fix_rounds"] >= self.options["max_fix_rounds"]:
            self.gate("rounds_exhausted")
        number = self.state["fix_rounds"] + 1
        result = self.call("fix", number)
        listed = [x["id"] for k in ("addressed", "not_addressed") for x in result.output[k]]
        if len(set(listed)) != len(listed) or set(listed) != {f["id"] for f in findings}:
            raise RuntimeError("fix must account for every open Important finding exactly once")
        before = self.snapshot_dirty()
        if not self.check("fix", number):
            raise RuntimeError("fix checks failed")
        after = self.snapshot_dirty()
        changed = [p for p in after if p not in before or after[p] != before[p]] + [p for p in before if p not in after]
        if changed:
            raise RuntimeError("checks modified candidate files")
        atomic_json(self.work / f"fix-{number}.json", result.output)
        self.state["fix_rounds"] = number
        self.clear_outcome()
        commit = self.repo.commit(self.cwd, f"factory({self.issue}): fix {number} (claims await review)")
        self.state.setdefault("fixes", []).append({
            "round": number, "commit": commit, "at": utcnow(), **self.provenance("fix", result),
        })
        self.save()
        self.publish()

    def render_ledger(self):
        if not self.ledger["findings"]:
            return "No findings."
        return "\n\n".join(
            f"- **{f['id']} [{f['severity']}, {f['status']}] {f['title']}** "
            f"(`{f['file']}:{f.get('line') or '?'}`)\n  Evidence: {f['evidence']}"
            + (f"\n  Adjudication: {f.get('dismissed_reason')}" if f['status'] == 'dismissed' else '')
            for f in self.ledger["findings"]
        )

    def finalize(self):
        self.require("build")
        if open_important(self.ledger):
            raise RuntimeError("cannot finalize with open Important findings")
        if self.state.get("outcome") == "done":
            if not self.is_head(self.state.get("outcome_sha")):
                raise RuntimeError("HEAD changed after completion; review it before finalizing again")
            self.publish()
            return
        if not self.reviewed_head():
            raise RuntimeError("HEAD is not the last reviewed commit")
        number = len(self.state["reviews"])
        if not self.check("finalize", number):
            raise RuntimeError("finalize checks failed")
        if changed_paths(self.cwd):
            raise RuntimeError("finalize checks modified the worktree")
        self.publish()
        pr = self.state["pr"]["number"]
        summary = (f"Factory completed #{self.issue}.\n\n"
                   + self.render_documents() + "\n\n"
                   + self.render_ledger() + "\n\nHarness provenance:\n```json\n"
                   + json.dumps({"stages": self.state["stages"], "reviews": self.state["reviews"],
                                 "fixes": self.state.get("fixes", [])}, indent=2) + "\n```")
        self.github.comment(pr, summary, f"factory:finalize:{self.head()}")
        self.github.ready(pr)
        self.state.update(outcome="done", outcome_sha=self.head())
        self.save()
        self.publish()

    def dismiss(self, finding_id, reason):
        if not reason.strip():
            raise ValueError("dismissal requires a nonempty reason")
        match = next((f for f in self.ledger["findings"] if f["id"] == finding_id), None)
        if match is None:
            raise ValueError(f"unknown finding {finding_id}")
        if match["status"] == "dismissed" and match.get("dismissed_reason") == reason:
            self.publish()
            return
        match.update(status="dismissed", status_round=len(self.state["reviews"]),
                     status_evidence=reason, dismissed_reason=reason)
        self.clear_outcome()
        self.save()
        self.publish()

    def rewind(self, stage):
        if stage not in ("spec", "plan", "build"):
            raise ValueError("--force is supported only for spec, plan, and build")
        if not self.state:
            return
        old = copy.deepcopy(self.state)
        previous = {"spec": None, "plan": "spec", "build": "plan"}[stage]
        if previous:
            self.require(previous)
        target = self.repo.resolve(self.cwd, old["stages"][previous]["commit"]) if previous else old["base"]["sha"]
        self.rewind_original_head = self.head()
        self.rewind_backup = (old, copy.deepcopy(self.ledger), {
            name: (self.work / name).read_bytes() if (self.work / name).exists() else None
            for name in ("spec.md", "plan.md")
        })
        self.repo.reset(self.cwd, target)
        self.rewound_pr = old.get("pr")
        stages = ("spec", "plan", "build")
        self.state["stages"] = {name: old["stages"][name] for name in stages[:stages.index(stage)]}
        self.state.update(reviews=[], fixes=[], fix_rounds=0)
        self.state.pop("build_summary", None)
        self.clear_outcome()
        self.force_push = True

    def rollback(self):
        if self.force_push and hasattr(self, "rewind_backup"):
            self.repo.reset(self.cwd, self.rewind_original_head)
            self.state, self.ledger, documents = self.rewind_backup
            for name, data in documents.items():
                path = self.work / name
                if data is None:
                    path.unlink(missing_ok=True)
                else:
                    path.write_bytes(data)
            self.force_push = False
            self.save()
        else:
            self.repo.reset(self.cwd)
        self.reload()

    def reviewed_head(self):
        return (bool(self.state["reviews"])
                and self.is_head(self.state["reviews"][-1]["sha"])
                and self.state["reviews"][-1]["fix_rounds"] == self.state["fix_rounds"])

    def run(self):
        if self.state.get("reviews") and self.state.get("pr"):
            # Review comments are idempotent writes. Replay the latest one when
            # resuming after a failure following its saved review artifact.
            self.review_comment(len(self.state["reviews"]))
        self.parked()
        if self.state.get("outcome") == "done":
            self.finalize()
            return
        self.spec()
        self.plan()
        self.build()
        while True:
            if not self.reviewed_head():
                self.review()
            if not open_important(self.ledger):
                self.finalize()
                return
            self.fix()
