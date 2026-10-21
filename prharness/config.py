from __future__ import annotations

from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


def _split_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", validate_assignment=True)

    github_webhook_secret: str = ""
    handlers: str = "log"
    pr_actions: str = "opened,synchronize,reopened,ready_for_review"
    ignore_drafts: bool = False
    handler_timeout_seconds: float = 30.0

    forward_url: str = ""
    forward_secret: str = ""
    forward_timeout_seconds: float = 10.0

    github_token: SecretStr = SecretStr("")
    github_api_url: str = "https://api.github.com"
    github_reviewer_login: str = ""

    openai_api_key: SecretStr = SecretStr("")
    openai_model: str = "gpt-5.5"
    openai_base_url: str | None = None
    openai_timeout_seconds: float = 120.0

    review_max_diff_chars: int = 120_000
    review_max_file_chars: int = 40_000
    review_max_file_reads: int = 10
    review_max_turns: int = 8
    review_max_comments: int = 25

    security_model: str = "gpt-5.5"
    security_timeout_seconds: float = 300.0
    security_max_scan_files: int = 50
    semgrep_enabled: bool = True
    semgrep_bin: str = "semgrep"
    semgrep_config: str = "p/default"
    semgrep_timeout_seconds: float = 180.0
    infra_semgrep_config: str = "p/github-actions,p/dockerfile,p/terraform,p/kubernetes"

    dependency_max_packages: int = 100
    osv_api_url: str = "https://api.osv.dev"
    pypi_url: str = "https://pypi.org"
    npm_registry_url: str = "https://registry.npmjs.org"
    go_proxy_url: str = "https://proxy.golang.org"

    max_webhook_bytes: int = 5_000_000
    webhook_replay_ttl_seconds: int = 600
    webhook_replay_cache_size: int = 10_000
    log_level: str = "INFO"

    @property
    def handler_specs(self) -> list[str]:
        return _split_csv(self.handlers)

    @property
    def pr_action_set(self) -> frozenset[str] | None:
        """Actions to dispatch, or ``None`` when every action is accepted."""
        actions = _split_csv(self.pr_actions)
        if "*" in actions:
            return None
        return frozenset(actions)


@lru_cache
def get_settings() -> Settings:
    return Settings()
