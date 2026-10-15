"""Configuration, loaded from the environment and/or a `.env` file.

Design notes:
  * The Discord-prefixed names (``DISCORD_WEBHOOK_URL`` and friends) are the
    ones people copy out of the Discord UI, so they keep their natural names.
  * Everything else is namespaced under ``RADAR_`` to keep the env tidy.
  * Validation happens once, at startup. A bad config should explode loudly
    before we start hammering an endpoint, not 3 hours into a scan.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal, get_args

from pydantic import AliasChoices, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .errors import ConfigError
from .models import TIER_ORDER, Tier
from .platforms import default_db_path

#: Valid values for ``Settings.source``. Exported so the bot's slash-command
#: choices are checked against the same set the config validates against.
SourceName = Literal["wordlist", "bruteforce", "stdin", "file"]

#: Runtime tuple of :data:`SourceName`, for iterating (e.g. building CLI choices).
SOURCE_NAMES: tuple[SourceName, ...] = get_args(SourceName)

#: Discord only accepts these in a username (plus `.`), length 2..32.
USERNAME_RE = r"^[a-z0-9_.]{2,32}$"


class Settings(BaseSettings):
    """Runtime configuration."""

    model_config = SettingsConfigDict(
        env_prefix="RADAR_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        # Lets the CLI pass overrides by field name (``Settings(source=...)``)
        # despite the aliases used for the Discord-prefixed variables.
        populate_by_name=True,
    )

    # ---------------------------------------------------------- Discord
    discord_webhook_url: str = Field(
        default="",
        validation_alias=AliasChoices("RADAR_DISCORD_WEBHOOK_URL", "DISCORD_WEBHOOK_URL"),
    )
    discord_bot_token: str | None = Field(
        default=None,
        validation_alias=AliasChoices("RADAR_DISCORD_BOT_TOKEN", "DISCORD_BOT_TOKEN"),
    )
    discord_command_channel_id: int | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "RADAR_DISCORD_COMMAND_CHANNEL_ID", "DISCORD_COMMAND_CHANNEL_ID"
        ),
    )

    # ---------------------------------------------------------- scanning
    source: SourceName = "wordlist"
    length: int = Field(default=4, ge=2, le=32)
    names_file: str = ""
    limit: int = Field(default=0, ge=0, description="0 = scan until stopped")

    # -------------------------------------------------------- networking
    rate_limit_rps: float = Field(
        default=0.5,
        gt=0,
        le=20,
        description=(
            "Upper bound on request rate. The adaptive limiter sits below this "
            "because Discord throttles the public endpoint aggressively."
        ),
    )
    concurrency: int = Field(default=1, ge=1, le=16)
    timeout_s: float = Field(default=25.0, gt=0)
    max_retries: int = Field(default=3, ge=0, le=10)
    user_agent: str = "fourchar-radar/0.1 (+https://github.com/your-org/fourchar-radar)"
    proxy: str | None = None

    # ------------------------------------------------------------ alerts
    alert_webhook: bool = True
    alert_min_tier: Tier = Tier.RARE
    alert_cooldown_s: float = Field(default=0.0, ge=0)
    alert_embeds: bool = True

    # ---------------------------------------------------------- storage
    # Absolute by default: a relative path breaks the moment the radar is
    # started from systemd, cron, or a different terminal tab.
    db_path: str = Field(default_factory=default_db_path)

    # --------------------------------------------------------- logging
    log_level: str = "INFO"
    log_json: bool = False

    # -------------------------------------------------------- validation
    @field_validator("discord_webhook_url")
    @classmethod
    def _check_webhook(cls, value: str) -> str:
        if not value:
            return value
        if not value.startswith(
            ("https://discord.com/api/webhooks/", "https://discordapp.com/api/webhooks/")
        ):
            raise ValueError(
                "webhook URL must look like https://discord.com/api/webhooks/<id>/<token>"
            )
        return value

    @field_validator("log_level")
    @classmethod
    def _check_level(cls, value: str) -> str:
        allowed = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}
        upper = value.upper()
        if upper not in allowed:
            raise ValueError(f"log_level must be one of {sorted(allowed)}")
        return upper

    @model_validator(mode="after")
    def _check_consistency(self) -> Settings:
        if self.source == "file" and not self.names_file:
            raise ValueError("RADAR_SOURCE=file requires RADAR_NAMES_FILE to point at a wordlist")
        return self

    # ------------------------------------------------------------ helpers
    def require_webhook(self) -> str:
        """Return the webhook URL or explain what is missing.

        Checked by the commands that actually alert (``run``), not at parse
        time — read-only commands like ``top`` must work with no webhook at all.
        """
        if not self.alert_webhook:
            raise ConfigError("alerts are disabled (RADAR_ALERT_WEBHOOK=false)")
        if not self.discord_webhook_url:
            raise ConfigError(
                "no webhook configured — paste a URL into DISCORD_WEBHOOK_URL "
                "(channel -> Edit Channel -> Integrations -> Webhooks), "
                "or pass --dry-run to scan without alerting"
            )
        return self.discord_webhook_url

    @property
    def webhook_enabled(self) -> bool:
        return self.alert_webhook and bool(self.discord_webhook_url)

    @property
    def bot_enabled(self) -> bool:
        return bool(self.discord_bot_token)

    @property
    def min_tier_rank(self) -> int:
        return TIER_ORDER[self.alert_min_tier]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings singleton."""
    return Settings()
