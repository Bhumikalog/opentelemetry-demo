from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

PROJECT_ROOT = Path(__file__).resolve().parent.parent

VALID_ADAPTERS = ("simulated", "organizer")
VALID_PROVIDERS = ("", "anthropic", "openai")
TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
FALSE_VALUES = frozenset({"", "0", "false", "no", "off"})


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Limits:
    http_connect_timeout_s: float = 3.0
    http_read_timeout_s: float = 8.0
    http_max_body_bytes: int = 200_000
    tool_output_max_chars: int = 6_000
    max_tool_calls: int = 12
    investigation_deadline_s: float = 120.0
    llm_timeout_s: float = 60.0
    llm_retries: int = 1
    action_timeout_s: float = 15.0
    action_retries: int = 0
    approval_expiry_s: int = 600
    action_cooldown_s: int = 600
    default_verify_wait_s: int = 90
    default_verify_window_s: int = 120


@dataclass(frozen=True)
class Settings:
    llm_provider: str
    llm_model: str
    llm_api_key: str
    prometheus_url: str
    jaeger_url: str
    opensearch_url: str
    action_api_url: str
    action_api_token: str
    adapter: str
    kill_switch_env: bool
    allow_draft_runbooks: bool
    state_dir: Path
    reports_dir: Path
    limits: Limits = Limits()

    @property
    def kill_switch_file(self) -> Path:
        return self.state_dir / "KILL_SWITCH"

    @property
    def ledger_file(self) -> Path:
        return self.state_dir / "ledger.jsonl"

    def require_llm(self) -> None:
        missing = []
        if not self.llm_provider:
            missing.append("LLM_PROVIDER")
        if not self.llm_model:
            missing.append("LLM_MODEL")
        if not self.llm_api_key:
            missing.append("the API key variable for the chosen provider")
        if missing:
            raise ConfigError("A live LLM run needs: " + ", ".join(missing))

    def require_action_api(self) -> None:
        if not self.action_api_url:
            raise ConfigError("The organizer adapter needs ACTION_API_URL")


def _parse_bool(name: str, raw: str) -> bool:
    value = raw.strip().lower()
    if value in TRUE_VALUES:
        return True
    if value in FALSE_VALUES:
        return False
    raise ConfigError(f"{name} must be one of 1/0/true/false/yes/no/on/off, got {raw!r}")


def _api_key_for(provider: str, env: Mapping[str, str]) -> str:
    if provider == "anthropic":
        return env.get("ANTHROPIC_API_KEY", "").strip()
    if provider == "openai":
        return env.get("OPENAI_API_KEY", "").strip()
    return ""


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    if env is None:
        try:
            from dotenv import load_dotenv

            load_dotenv(PROJECT_ROOT / ".env", override=False)
        except ImportError:
            pass
        env = os.environ

    provider = env.get("LLM_PROVIDER", "").strip().lower()
    if provider not in VALID_PROVIDERS:
        raise ConfigError(f"LLM_PROVIDER must be one of anthropic/openai or empty, got {provider!r}")

    adapter = (env.get("ONCALL_ADAPTER", "simulated").strip().lower()) or "simulated"
    if adapter not in VALID_ADAPTERS:
        raise ConfigError(f"ONCALL_ADAPTER must be one of {VALID_ADAPTERS}, got {adapter!r}")

    action_api_url = env.get("ACTION_API_URL", "").strip()
    if adapter == "organizer" and not action_api_url:
        raise ConfigError("ONCALL_ADAPTER=organizer needs ACTION_API_URL to be set")

    return Settings(
        llm_provider=provider,
        llm_model=env.get("LLM_MODEL", "").strip(),
        llm_api_key=_api_key_for(provider, env),
        prometheus_url=env.get("PROMETHEUS_URL", "http://localhost:9090").strip().rstrip("/"),
        jaeger_url=env.get("JAEGER_URL", "http://localhost:8080/jaeger/ui").strip().rstrip("/"),
        opensearch_url=env.get("OPENSEARCH_URL", "").strip().rstrip("/"),
        action_api_url=action_api_url.rstrip("/"),
        action_api_token=env.get("ACTION_API_TOKEN", "").strip(),
        adapter=adapter,
        kill_switch_env=_parse_bool("ONCALL_KILL_SWITCH", env.get("ONCALL_KILL_SWITCH", "0")),
        allow_draft_runbooks=_parse_bool(
            "ONCALL_ALLOW_DRAFT_RUNBOOKS", env.get("ONCALL_ALLOW_DRAFT_RUNBOOKS", "0")
        ),
        state_dir=PROJECT_ROOT / "state",
        reports_dir=PROJECT_ROOT / "reports",
    )
