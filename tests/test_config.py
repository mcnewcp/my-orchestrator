"""factory.config — factory.toml parsing, defaults, enum validation and CLI overrides (design §14, §6)."""

from __future__ import annotations

import pytest

from factory.config import (
    AUTHS,
    DEFAULT_TOML,
    HARNESSES,
    Config,
    HarnessConfig,
    load_config,
    parse_config,
    validate_overrides,
)
from factory.errors import FactoryError

# The §14 file exactly as the design prints it: nothing but documented keys, no extensions.
SECTION_14_TOML = """\
[factory]
harness = "claude"
auth = "api"
base_branch = "main"
max_fix_rounds = 3
stage_timeout_min = 45
checks = [["make", "test"], ["make", "lint"]]
test_paths = ["tests/"]
protected_paths = []

[poll]
label = "factory"
max_consecutive_failures = 3

[harness.claude]
model = ""
pinned_version = ""

[harness.codex]
model = ""
pinned_version = ""
"""


def test_empty_file_is_all_defaults():
    config = parse_config("")
    assert config == Config()
    assert config.harness == "claude"
    assert config.auth == "api"
    assert config.checks == [["make", "test"], ["make", "lint"]]
    assert set(config.harnesses) == set(HARNESSES)


def test_section_14_file_is_valid_and_matches_the_defaults():
    """A §14-only file must parse; every extension key defaults."""
    config = parse_config(SECTION_14_TOML)
    assert config == Config()
    assert config.max_nits == 10
    assert config.harness_config("claude").max_turns_read == 30
    assert config.harness_config("codex").writable_dirs == []


def test_default_toml_template_parses_and_round_trips():
    config = parse_config(DEFAULT_TOML)
    assert config == Config()


def test_every_documented_key_is_read():
    config = parse_config(
        """
        [factory]
        harness = "codex"
        auth = "subscription"
        base_branch = "trunk"
        max_fix_rounds = 5
        stage_timeout_min = 10
        checks = [["python3", "checks.py"]]
        test_paths = ["t/", "spec/"]
        protected_paths = ["infra/"]
        max_nits = 2
        max_diff_bytes = 4096
        transient_paths = [".cache/"]
        env_passthrough = ["JAVA_HOME"]

        [poll]
        label = "robot"
        max_consecutive_failures = 7

        [harness.claude]
        model = "claude-x"
        pinned_version = "2.1.263"
        max_turns_read = 11
        max_turns_write = 22
        max_budget_usd = 1.5

        [harness.codex]
        model = "gpt-x"
        pinned_version = "0.153.4"
        writable_dirs = ["~/.cache/uv"]
        """
    )
    assert config.harness == "codex"
    assert config.auth == "subscription"
    assert config.base_branch == "trunk"
    assert config.max_fix_rounds == 5
    assert config.stage_timeout_min == 10
    assert config.stage_timeout_s == 600
    assert config.checks == [["python3", "checks.py"]]
    assert config.test_paths == ["t/", "spec/"]
    assert config.protected_paths == ["infra/"]
    assert config.max_nits == 2
    assert config.max_diff_bytes == 4096
    assert config.transient_paths == [".cache/"]
    assert config.env_passthrough == ["JAVA_HOME"]
    assert config.poll_label == "robot"
    assert config.poll_max_consecutive_failures == 7
    claude = config.harness_config("claude")
    assert (claude.model, claude.pinned_version) == ("claude-x", "2.1.263")
    assert (claude.max_turns_read, claude.max_turns_write, claude.max_budget_usd) == (11, 22, 1.5)
    codex = config.harness_config("codex")
    assert (codex.model, codex.writable_dirs) == ("gpt-x", ["~/.cache/uv"])
    # config.model is the SELECTED harness's model.
    assert config.model == "gpt-x"


def test_model_property_is_none_when_the_cli_default_is_wanted():
    assert parse_config('[harness.claude]\nmodel = ""').model is None
    assert Config().model is None


def test_harness_config_falls_back_for_an_unknown_name():
    assert parse_config("").harness_config("gemini") == HarnessConfig()


def test_unknown_keys_and_tables_are_ignored():
    config = parse_config(
        """
        [factory]
        harness = "codex"
        future_key = 12

        [poll]
        unknown = true

        [harness.gemini]
        model = "g"

        [somewhere_else]
        x = 1
        """
    )
    assert config.harness == "codex"
    assert set(config.harnesses) == set(HARNESSES)


# --- errors ------------------------------------------------------------------------------------


def test_missing_file_names_the_directory_and_points_at_init(tmp_path):
    with pytest.raises(FactoryError) as excinfo:
        load_config(tmp_path)
    assert str(tmp_path) in excinfo.value.message
    assert "factory init" in (excinfo.value.hint or "")


def test_load_config_reads_the_checkout_file(tmp_path):
    (tmp_path / "factory.toml").write_text('[factory]\nharness = "codex"\n', encoding="utf-8")
    assert load_config(tmp_path).harness == "codex"


def test_invalid_toml_names_the_source(tmp_path):
    (tmp_path / "factory.toml").write_text("[factory\n", encoding="utf-8")
    with pytest.raises(FactoryError) as excinfo:
        load_config(tmp_path)
    assert "invalid TOML" in excinfo.value.message
    assert str(tmp_path / "factory.toml") in excinfo.value.message


@pytest.mark.parametrize(
    ("text", "needle"),
    [
        ('[factory]\nharness = "gpt"', "[factory] harness: unknown value 'gpt'"),
        ('[factory]\nauth = "oauth"', "[factory] auth: unknown value 'oauth'"),
        ("[factory]\nharness = 3", "[factory] harness must be a string"),
        ('[factory]\nmax_fix_rounds = "3"', "[factory] max_fix_rounds must be an integer"),
        ("[factory]\nmax_fix_rounds = true", "[factory] max_fix_rounds must be an integer"),
        ("[factory]\nstage_timeout_min = 0", "[factory] stage_timeout_min must be >= 1"),
        ("[factory]\nmax_diff_bytes = 0", "[factory] max_diff_bytes must be >= 1"),
        ("[factory]\nmax_nits = -1", "[factory] max_nits must be >= 0"),
        ('[factory]\nchecks = "make test"', "[factory] checks must be an array of argv arrays"),
        ("[factory]\nchecks = [[]]", "[factory] checks[0] must be a non-empty argv array"),
        ('[factory]\nchecks = ["make"]', "[factory] checks[0] must be a non-empty argv array"),
        ('[factory]\nchecks = [["make", 1]]', "[factory] checks[0][1] must be a string"),
        ("[factory]\ntest_paths = [1]", "[factory] test_paths[0] must be a string"),
        (
            '[factory]\nprotected_paths = "x"',
            "[factory] protected_paths must be an array of strings",
        ),
        ("[poll]\nmax_consecutive_failures = 0", "[poll] max_consecutive_failures must be >= 1"),
        ("[poll]\nlabel = 1", "[poll] label must be a string"),
        (
            '[harness.claude]\nmax_turns_read = "x"',
            "[harness.claude] max_turns_read must be an integer",
        ),
        ("[harness.claude]\nmax_budget_usd = -1", "[harness.claude] max_budget_usd must be >= 0"),
        (
            '[harness.codex]\nwritable_dirs = "~"',
            "[harness.codex] writable_dirs must be an array of strings",
        ),
        ("factory = 1", "[factory] must be a table"),
        ('[harness]\nclaude = "x"', "[harness.claude] must be a table"),
    ],
)
def test_invalid_values_raise_with_a_message_naming_the_key(text, needle):
    with pytest.raises(FactoryError) as excinfo:
        parse_config(text)
    assert needle in excinfo.value.message


def test_max_budget_usd_accepts_a_plain_integer():
    assert (
        parse_config("[harness.claude]\nmax_budget_usd = 2").harness_config("claude").max_budget_usd
        == 2.0
    )


# --- validate_overrides ------------------------------------------------------------------------


def test_no_overrides_returns_an_equal_but_independent_copy():
    config = parse_config(SECTION_14_TOML)
    result = validate_overrides(config, harness=None, auth=None, model=None)
    assert result == config
    assert result is not config

    result.checks[0].append("--fast")
    result.test_paths.append("extra/")
    result.protected_paths.append("infra/")
    result.transient_paths.append(".cache/")
    result.env_passthrough.append("JAVA_HOME")
    result.harnesses["codex"].writable_dirs.append("~/.cache/uv")
    result.harnesses["claude"].model = "mutated"

    assert config.checks == [["make", "test"], ["make", "lint"]]
    assert config.test_paths == ["tests/"]
    assert config.protected_paths == []
    assert config.transient_paths == []
    assert config.env_passthrough == []
    assert config.harnesses["codex"].writable_dirs == []
    assert config.harnesses["claude"].model == ""


def test_model_applies_only_to_the_selected_harness():
    config = parse_config('[factory]\nharness = "claude"\n[harness.codex]\nmodel = "gpt-x"')
    result = validate_overrides(config, harness=None, auth=None, model="claude-opus-4")
    assert result.harnesses["claude"].model == "claude-opus-4"
    assert result.harnesses["codex"].model == "gpt-x"
    assert result.model == "claude-opus-4"


def test_model_follows_a_harness_override():
    config = parse_config('[factory]\nharness = "claude"\n[harness.claude]\nmodel = "claude-x"')
    result = validate_overrides(config, harness="codex", auth=None, model="gpt-5")
    assert result.harness == "codex"
    assert result.harnesses["codex"].model == "gpt-5"
    assert result.harnesses["claude"].model == "claude-x"
    assert result.model == "gpt-5"


def test_overrides_leave_other_harness_settings_alone():
    config = parse_config("[harness.claude]\nmax_turns_read = 7\npinned_version = '2.1.263'")
    result = validate_overrides(config, harness=None, auth=None, model="m")
    claude = result.harness_config("claude")
    assert (claude.max_turns_read, claude.pinned_version) == (7, "2.1.263")


@pytest.mark.parametrize("auth", AUTHS)
@pytest.mark.parametrize("harness", HARNESSES)
def test_every_harness_auth_pair_is_accepted(harness, auth):
    result = validate_overrides(Config(), harness=harness, auth=auth, model=None)
    assert (result.harness, result.auth) == (harness, auth)


def test_empty_model_override_means_the_cli_default():
    config = parse_config('[harness.claude]\nmodel = "claude-x"')
    result = validate_overrides(config, harness=None, auth=None, model="")
    assert result.harnesses["claude"].model == ""
    assert result.model is None


@pytest.mark.parametrize(
    ("kwargs", "needle"),
    [
        ({"harness": "gpt", "auth": None, "model": None}, "--harness: unknown value 'gpt'"),
        ({"harness": None, "auth": "oauth", "model": None}, "--auth: unknown value 'oauth'"),
    ],
)
def test_invalid_override_enums_raise(kwargs, needle):
    with pytest.raises(FactoryError) as excinfo:
        validate_overrides(Config(), **kwargs)
    assert needle in excinfo.value.message
