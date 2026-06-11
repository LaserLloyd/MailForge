"""Configuration schema (build spec §8).

Layered precedence via pydantic-settings: defaults < TOML < env < CLI.
Secrets (passwords/tokens) are NEVER stored here — they live in the OS
keyring (see ``secrets.py``) under service names derived from the account
``name``. In-memory secrets are ``SecretStr`` and never serialised.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import tomli_w
from pydantic import BaseModel, Field
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)

from .paths import config_file, default_db_path


class IMAPAccount(BaseModel):
    name: str
    host: str
    port: int = 993
    username: str
    auth_method: Literal["password", "xoauth2"] = "xoauth2"
    folders: list[str] = Field(default_factory=lambda: ["INBOX"])
    poll_interval_s: int = 30


class SMTPAccount(BaseModel):
    host: str
    port: int = 587
    username: str
    starttls: bool = True


class LLMSettings(BaseModel):
    # Shares the local LM Studio endpoint that OpenClaw's "vllm" provider uses
    # (127.0.0.1:1234). Defaults below are model IDs actually served there so a
    # fresh install talks to a real model out of the box; override via the
    # Models page or config.toml.
    lm_studio_host: str = "localhost:1234"
    chat_model: str = "qwen/qwen3.6-35b-a3b"
    embedding_model: str = "text-embedding-nomic-embed-text-v1.5"
    ttl_seconds: int = 1800
    context_length: int = 8192


class SecuritySettings(BaseModel):
    recipient_allowlist_domains: set[str] = Field(default_factory=set)
    recipient_block_domains: set[str] = Field(default_factory=set)
    link_allowlist_domains: set[str] = Field(default_factory=set)
    require_human_approval: bool = True  # MUST be True (invariant)
    autosend_allowed: bool = False  # MUST be False (invariant)
    prompt_guard_threshold: float = 0.85
    pii_block_categories: list[str] = Field(
        default_factory=lambda: ["US_SSN", "CREDIT_CARD", "IBAN_CODE"]
    )
    crossthread_ngram_n: int = 6
    crossthread_embedding_threshold: float = 0.85
    approvals_per_hour: int = 60  # UI rate-limit (§9)


class StyleSettings(BaseModel):
    style_guide_paths: list[str] = Field(default_factory=list)
    context_doc_paths: list[str] = Field(default_factory=list)
    signature: str = ""
    tone: str = "professional"


class OpenClawSettings(BaseModel):
    """Optional 'OpenClaw link'. The app is a free-standing LM Studio client;
    everything here is additive and OFF/empty by default.

    OpenClaw (which runs the overall system) plugs in two ways:

    * **Prompt updates** — drop updated ``<name>.txt`` templates into
      ``prompt_override_dir`` (see :func:`paths.prompt_override_dir`); they win
      over the packaged defaults with no restart. ``enabled=False`` ignores the
      override dir entirely.
    * **Heavy lifting when online** — point ``heavy_lift_base_url`` at any
      OpenAI-compatible endpoint OpenClaw exposes (a cloud model it has keys
      for). When set AND reachable, the *draft* step uses it; on any error it
      falls back to the local LM Studio model, so a missing/offline link never
      breaks the app. The API key is read from the env var named by
      ``heavy_lift_key_env`` (kept out of config/keyring so OpenClaw owns it).

    Empty ``heavy_lift_base_url`` => pure standalone on local LM Studio.
    """

    enabled: bool = True
    prompt_override_dir: str = ""  # "" => paths.prompt_override_dir() default
    heavy_lift_base_url: str = ""  # e.g. "http://127.0.0.1:8080/v1"; "" => local only
    heavy_lift_model: str = ""
    heavy_lift_key_env: str = "OPENCLAW_EMAIL_HEAVY_KEY"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="OPENCLAW_EMAIL_",
        env_file=".env",
        env_nested_delimiter="__",
        toml_file=str(config_file()),
        extra="ignore",
    )

    imap_accounts: list[IMAPAccount] = Field(default_factory=list)
    smtp: SMTPAccount | None = None
    llm: LLMSettings = Field(default_factory=LLMSettings)
    security: SecuritySettings = Field(default_factory=SecuritySettings)
    style: StyleSettings = Field(default_factory=StyleSettings)
    openclaw: OpenClawSettings = Field(default_factory=OpenClawSettings)
    db_path: str = Field(default_factory=lambda: str(default_db_path()))

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # Precedence (first wins): init/CLI > env > dotenv > TOML > defaults.
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            TomlConfigSettingsSource(settings_cls),
        )

    # ----- enforcement of non-negotiable invariants (spec §0) -----
    def assert_invariants(self) -> None:
        if self.security.autosend_allowed:
            raise ValueError("INVARIANT VIOLATION: security.autosend_allowed must be False")
        if not self.security.require_human_approval:
            raise ValueError(
                "INVARIANT VIOLATION: security.require_human_approval must be True"
            )

    def resolved_db_path(self) -> Path:
        import os

        return Path(os.path.expanduser(os.path.expandvars(self.db_path))).resolve()

    def to_toml(self) -> str:
        return tomli_w.dumps(self.model_dump(mode="json", exclude_none=True))

    def save(self, path: Path | None = None) -> Path:
        target = path or config_file()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(self.to_toml(), encoding="utf-8")
        return target


def load_settings() -> Settings:
    """Load and validate settings, enforcing invariants."""
    s = Settings()
    s.assert_invariants()
    return s
