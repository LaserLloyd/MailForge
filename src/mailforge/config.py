"""Configuration schema (build spec §8).

Layered precedence via pydantic-settings: defaults < TOML < env < CLI.
Secrets (passwords/tokens) are NEVER stored here — they live in the OS
keyring (see ``secrets.py``) under service names derived from the account
``name``. In-memory secrets are ``SecretStr`` and never serialised.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

import tomli_w
from pydantic import BaseModel, Field, field_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)

from .paths import config_file, default_db_path

_SITE_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{1,31}$")


class SiteConfig(BaseModel):
    """One business/brand ("site"). Every mailbox belongs to exactly one site;
    RAG retrieval, response templates, and the OpenClaw bridge are site-scoped.
    New sites need only a ``[sites.<id>]`` entry here — the DB migrates itself."""

    name: str
    # Site-specific hard rule injected into the worker drafting prompts
    # ({{SITE_RULE}}). Keep it short and factual — it is a guardrail, not lore.
    guidance: str = "Do not invent business facts; if context is missing, say so."
    # ``content_only`` is a deterministic pre-AI gate for public creator
    # inboxes: messages default to review unless related to expected content.
    # ``standard`` preserves the original classification workflow.
    screening_mode: Literal["standard", "content_only"] = "standard"
    triage_guidance: str = ""
    # content_only screening vocabulary: regex fragments for mail that IS about
    # this site (content_terms) and for the site's own brand names (brand_terms,
    # used to catch forged "from ourselves" mail). Both extend the packaged
    # generic vocabulary; empty lists mean "generic only".
    content_terms: list[str] = Field(default_factory=list)
    brand_terms: list[str] = Field(default_factory=list)

    # --- managed site knowledge (knowledge/handbooks.py) ---------------------
    # Local folder holding this site's canonical public copy: either an
    # ``llms.txt`` + ``llms-full.txt`` pair, or HTML/Markdown pages. Empty =>
    # the site has no managed handbook and is skipped by knowledge refreshes.
    knowledge_root: str | None = None
    # Markdown policy header prepended to both generated documents. It is the
    # bot's non-negotiable rule sheet; ``{source_digest}`` is substituted with
    # the digest of the canonical sources. Required to generate a handbook.
    policy_file: str | None = None
    # Explicit page allowlist (relative paths, in output order). Empty =>
    # auto-discover every .html/.htm/.md file under ``knowledge_root``.
    knowledge_pages: list[str] = Field(default_factory=list)
    # Fail closed: generation aborts when any of these pages is missing.
    knowledge_required_pages: list[str] = Field(default_factory=list)
    # Case-insensitive regexes; ANY match drops that block from the full text.
    # This is how a site keeps facts the bot must not resolve autonomously
    # (placeholders, exact digests, disputed deadlines) out of bot knowledge.
    knowledge_exclude_patterns: list[str] = Field(default_factory=list)
    # Optional override for the sentence introducing the full-text section.
    knowledge_intro: str = ""
    # Agent workspace folder receiving SITE-HANDBOOK.md (absolute or ~-relative).
    # Empty => the handbook is not copied anywhere.
    openclaw_workspace: str | None = None
    # Public base URL, used for "Public URL:" lines in page mode.
    site_url: str = ""


DEFAULT_SITES: dict[str, SiteConfig] = {
    "main": SiteConfig(
        name="Main",
        guidance=(
            "Do not invent prices, stock, availability, delivery dates, "
            "technical tolerances, licensing terms, or commitments. If the "
            "answer is not in the provided context, say that human review is "
            "needed."
        ),
    ),
}


class IMAPAccount(BaseModel):
    name: str
    site_id: str = "main"
    host: str
    port: int = 993
    username: str
    auth_method: Literal["password", "xoauth2"] = "xoauth2"
    folders: list[str] = Field(default_factory=lambda: ["INBOX"])
    poll_interval_s: int = 30
    # Optional per-account SMTP override for mailboxes on a different provider
    # than the global [smtp] relay. Empty host => use the global relay.
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_starttls: bool = True

    @field_validator("site_id")
    @classmethod
    def _site_shape(cls, v: str) -> str:
        v = (v or "").strip().lower()
        if not _SITE_ID_RE.match(v):
            raise ValueError("site_id must be a short lowercase slug (a-z, 0-9, -, _)")
        return v


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
    # --- inbound quarantine (prompt-injection containment) -------------------
    # Messages whose ingest-time injection score >= threshold are QUARANTINED:
    # no LLM (classify/draft/revise) ever sees their content, and the OpenClaw
    # bridge returns metadata only (never the body) until a human releases the
    # message in the local UI. 0 disables scoring-based quarantine.
    quarantine_enabled: bool = True
    quarantine_threshold: float = 0.85
    # --- attachment ingest caps ----------------------------------------------
    attachment_max_bytes: int = 15 * 1024 * 1024  # per file
    attachment_max_count: int = 25  # per message
    # --- deleting mail at the provider ---------------------------------------
    # A UI delete always moves the message to the LOCAL, reversible Trash. This
    # setting decides what additionally happens on the IMAP server:
    #   "trash"   — MOVE the message to the account's Trash/Deleted folder. The
    #               provider copy is recoverable from there. Falls back to
    #               "expunge" only when the server exposes no such folder.
    #   "expunge" — flag \\Deleted and EXPUNGE. NOT recoverable.
    #   "off"     — never touch the provider mailbox (pre-0.4 behaviour).
    server_delete_mode: Literal["off", "trash", "expunge"] = "trash"
    # Local Trash is a holding box, not a graveyard: spam/scam mail is deleted
    # on the next sweep with no holding period, everything else after this many
    # days — locally AND at the provider, per server_delete_mode above.
    trash_retention_days: int = 60


class StyleSettings(BaseModel):
    style_guide_paths: list[str] = Field(default_factory=list)
    context_doc_paths: list[str] = Field(default_factory=list)
    signature: str = ""
    tone: str = "professional"


class ComposeSettings(BaseModel):
    """Composer defaults. ``signature`` is the name that fills the
    ``{{signature}}`` placeholder in response templates and the composer;
    empty (the default) leaves the placeholder for the user to fill in."""

    signature: str = ""


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
    heavy_lift_key_env: str = "MAILFORGE_HEAVY_KEY"
    # --- agent-relayed send ("the user told the agent to send it") ----------
    # OFF by default. When enabled, the bridge gains prepare-send/send: the
    # agent must first call prepare-send (guardrails run, recipient stays
    # thread/allowlist-bound, a one-time short-lived token is issued and the
    # exact draft is returned for the human to confirm in chat), then send with
    # that token. This is NOT autosend — every send still traces to an explicit
    # human instruction, is rate-capped, and is fully audited.
    agent_send_enabled: bool = False
    agent_send_per_hour: int = 10
    agent_send_token_ttl_s: int = 900


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MAILFORGE_",
        env_file=".env",
        env_nested_delimiter="__",
        toml_file=str(config_file()),
        extra="ignore",
    )

    imap_accounts: list[IMAPAccount] = Field(default_factory=list)
    smtp: SMTPAccount | None = None
    sites: dict[str, SiteConfig] = Field(
        default_factory=lambda: dict(DEFAULT_SITES)
    )
    llm: LLMSettings = Field(default_factory=LLMSettings)
    security: SecuritySettings = Field(default_factory=SecuritySettings)
    style: StyleSettings = Field(default_factory=StyleSettings)
    compose: ComposeSettings = Field(default_factory=ComposeSettings)
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
        for account in self.imap_accounts:
            if account.site_id not in self.sites:
                raise ValueError(
                    f"Account '{account.name}' references unknown site "
                    f"'{account.site_id}'; add a [sites.{account.site_id}] entry."
                )

    def site_name(self, site_id: str) -> str:
        site = self.sites.get(str(site_id or "").lower())
        return site.name if site else str(site_id or "unassigned")

    def site_guidance(self, site_id: str) -> str:
        site = self.sites.get(str(site_id or "").lower())
        if site and site.guidance.strip():
            return site.guidance.strip()
        return "No additional site rule is available; do not invent business facts."

    def site_screening_mode(self, site_id: str) -> str:
        site = self.sites.get(str(site_id or "").lower())
        return site.screening_mode if site else "standard"

    def site_triage_guidance(self, site_id: str) -> str:
        site = self.sites.get(str(site_id or "").lower())
        return site.triage_guidance.strip() if site else ""

    def smtp_for_account(self, account: IMAPAccount) -> "SMTPAccount | None":
        """Effective SMTP settings for one mailbox: per-account override when
        set, else the global relay with the mailbox's own username."""
        if account.smtp_host.strip():
            return SMTPAccount(
                host=account.smtp_host.strip(),
                port=account.smtp_port,
                username=account.username,
                starttls=account.smtp_starttls,
            )
        if self.smtp is None:
            return None
        return SMTPAccount(
            host=self.smtp.host,
            port=self.smtp.port,
            username=account.username,
            starttls=self.smtp.starttls,
        )

    def resolved_db_path(self) -> Path:
        import os

        return Path(os.path.expanduser(os.path.expandvars(self.db_path))).resolve()

    def to_toml(self) -> str:
        return tomli_w.dumps(self.model_dump(mode="json", exclude_none=True))

    def save(self, path: Path | None = None) -> Path:
        import os
        import stat

        target = path or config_file()
        target.parent.mkdir(parents=True, exist_ok=True)
        mode = stat.S_IRUSR | stat.S_IWUSR
        temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(self.to_toml())
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
            # Config contains mailbox identities, local document paths and the
            # user's signature. It is not a password store, but it is private.
            os.chmod(target, mode)
        finally:
            temporary.unlink(missing_ok=True)
        return target


def load_settings() -> Settings:
    """Load and validate settings, enforcing invariants."""
    s = Settings()
    s.assert_invariants()
    # The store's site validation follows the configured registry (the
    # single default site applies when the config has no [sites] table).
    from .db.store import register_sites

    register_sites(s.sites.keys())
    return s
