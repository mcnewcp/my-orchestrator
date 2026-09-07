from types import SimpleNamespace

from typer.testing import CliRunner

from factory import cli


def test_status_accepts_optional_run_id_as_a_positional_argument(monkeypatch):
    requested = []
    run = {
        "id": "example-run",
        "workspace": "/workspace/runs/example-run/worktree",
        "artifacts": "/workspace/runs/example-run",
        "issue": 42,
    }

    def get(run_id):
        requested.append(run_id)
        return run

    monkeypatch.setattr(cli, "load_config", lambda: SimpleNamespace(host_path=str))
    monkeypatch.setattr(
        cli, "_state", lambda: SimpleNamespace(get=get, list_runs=lambda: [], attempts=lambda _: [])
    )
    runner = CliRunner()
    result = runner.invoke(cli.app, ["status", "example-run", "--json"])
    assert result.exit_code == 0, result.output
    assert requested == ["example-run"]
    assert '"id": "example-run"' in result.output
    listed = runner.invoke(cli.app, ["status", "--json"])
    assert listed.exit_code == 0 and listed.output.strip() == "[]"
