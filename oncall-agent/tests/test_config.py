from pathlib import Path

import pytest

from oncall.config import ConfigError, load_settings


def test_defaults_work_with_an_empty_environment():
    settings = load_settings({})
    assert settings.adapter == "simulated"
    assert settings.kill_switch_env is False
    assert settings.allow_draft_runbooks is False
    assert settings.prometheus_url == "http://localhost:9090"
    assert settings.limits.max_tool_calls == 12
    assert settings.limits.action_retries == 0
    assert settings.limits.approval_expiry_s == 600
    assert settings.kill_switch_file.name == "KILL_SWITCH"
    assert settings.ledger_file.name == "ledger.jsonl"


def test_urls_are_normalised():
    settings = load_settings({"PROMETHEUS_URL": "http://prom:9090/", "JAEGER_URL": "http://j:8080/jaeger/ui/"})
    assert settings.prometheus_url == "http://prom:9090"
    assert settings.jaeger_url == "http://j:8080/jaeger/ui"


def test_settings_have_no_feature_flag_fields():
    names = {field for field in load_settings({}).__dataclass_fields__}
    assert not [name for name in names if "flag" in name.lower()]


@pytest.mark.parametrize("raw,expected", [("1", True), ("true", True), ("YES", True), ("0", False), ("", False), ("off", False)])
def test_kill_switch_parsing(raw, expected):
    assert load_settings({"ONCALL_KILL_SWITCH": raw}).kill_switch_env is expected


def test_invalid_boolean_is_a_clear_error():
    with pytest.raises(ConfigError, match="ONCALL_KILL_SWITCH"):
        load_settings({"ONCALL_KILL_SWITCH": "maybe"})


def test_invalid_adapter_and_provider_are_rejected():
    with pytest.raises(ConfigError, match="ONCALL_ADAPTER"):
        load_settings({"ONCALL_ADAPTER": "docker"})
    with pytest.raises(ConfigError, match="LLM_PROVIDER"):
        load_settings({"LLM_PROVIDER": "gemini"})


def test_organizer_adapter_requires_action_api_url():
    with pytest.raises(ConfigError, match="ACTION_API_URL"):
        load_settings({"ONCALL_ADAPTER": "organizer"})
    ok = load_settings({"ONCALL_ADAPTER": "organizer", "ACTION_API_URL": "http://x/"})
    assert ok.action_api_url == "http://x"


def test_llm_key_is_picked_per_provider_and_require_llm_reports_gaps():
    settings = load_settings({"LLM_PROVIDER": "anthropic", "LLM_MODEL": "m", "ANTHROPIC_API_KEY": "k", "OPENAI_API_KEY": "other"})
    assert settings.llm_api_key == "k"
    settings.require_llm()
    with pytest.raises(ConfigError, match="LLM_PROVIDER"):
        load_settings({}).require_llm()
    with pytest.raises(ConfigError, match="API key"):
        load_settings({"LLM_PROVIDER": "openai", "LLM_MODEL": "m"}).require_llm()


def test_env_example_parses_with_dotenv():
    from dotenv import dotenv_values

    values = dotenv_values(Path(__file__).resolve().parent.parent / ".env.example")
    settings = load_settings({k: v or "" for k, v in values.items()})
    assert settings.adapter == "simulated"
    assert settings.llm_provider == ""
    assert settings.opensearch_url == ""
