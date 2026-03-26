"""Configuration utilities for the Discord bot."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional, Tuple

from dotenv import load_dotenv

load_dotenv()


def _get_int(name: str) -> Optional[int]:
    value = os.getenv(name)
    if not value or not value.strip():
        return None
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"Environment variable {name} must be an integer") from exc


def _get_int_list(name: str) -> Tuple[int, ...]:
    value = os.getenv(name)
    if not value or not value.strip():
        return ()
    ids = []
    for part in value.split(","):
        stripped = part.strip()
        if not stripped:
            continue
        try:
            ids.append(int(stripped))
        except ValueError as exc:
            raise ValueError(
                f"Environment variable {name} must be a comma-separated list of integers"
            ) from exc
    return tuple(ids)


@dataclass(frozen=True)
class BotConfig:
    """Runtime configuration loaded from environment variables."""

    # Core
    token: str
    guild_id: Optional[int]

    # Role IDs
    captain_role_id: Optional[int]
    co_captain_role_id: Optional[int]
    team_member_role_id: Optional[int]
    admin_role_ids: Tuple[int, ...]
    staff_role_ids: Tuple[int, ...]
    ranked_role_id: Optional[int]
    management_role_id: Optional[int]
    mod_role_id: Optional[int]
    caster_role_id: Optional[int]
    ref_role_id: Optional[int]

    # Channel IDs
    transactions_channel_id: Optional[int]
    log_channel_id: Optional[int]
    appeal_channel_id: Optional[int]
    ticket_channel_id: Optional[int]
    transcript_channel_id: Optional[int]
    match_results_channel_id: Optional[int]
    match_assignments_channel_id: Optional[int]
    match_staff_alert_channel_id: Optional[int]
    schedule_channel_id: Optional[int]

    # Category IDs
    match_category_id: Optional[int]
    general_support_category_id: Optional[int]
    ranked_support_category_id: Optional[int]
    management_support_category_id: Optional[int]

    # Appeal
    appeal_server_id: Optional[int]
    appeal_server_invite: str

    # AI (Groq)
    groq_api_key: Optional[str]
    groq_model: str
    ai_admin_ids: Tuple[int, ...]  # ← ADDED

    # Challonge
    challonge_username: Optional[str]
    challonge_api_key: Optional[str]
    challonge_tournament: Optional[str]

    # Web server
    web_host: Optional[str]
    web_port: Optional[int]

    @property
    def ticket_category_ids(self) -> Tuple[int, ...]:
        return tuple(
            c for c in (
                self.general_support_category_id,
                self.ranked_support_category_id,
                self.management_support_category_id,
            )
            if c is not None
        )

    @classmethod
    def from_env(cls) -> "BotConfig":
        token = os.getenv("DISCORD_TOKEN") or os.getenv("BOT_TOKEN")
        if not token:
            raise RuntimeError("DISCORD_TOKEN (or BOT_TOKEN) must be set")

        return cls(
            token=token,
            guild_id=_get_int("GUILD_ID"),

            captain_role_id=_get_int("CAPTAIN_ROLE_ID"),
            co_captain_role_id=_get_int("CO_CAPTAIN_ROLE_ID"),
            team_member_role_id=_get_int("TEAM_MEMBER_ROLE_ID") or _get_int("TEAM_PLAYER_ROLE_ID"),
            admin_role_ids=_get_int_list("ADMIN_ROLE_IDS"),
            staff_role_ids=_get_int_list("STAFF_ROLE_IDS"),
            ranked_role_id=_get_int("RANKED_ROLE_ID"),
            management_role_id=_get_int("MANAGEMENT_ROLE_ID"),
            mod_role_id=_get_int("MOD_ROLE_ID"),
            caster_role_id=_get_int("CASTER_ROLE_ID"),
            ref_role_id=_get_int("REF_ROLE_ID"),

            transactions_channel_id=_get_int("TRANSACTIONS_CHANNEL_ID"),
            log_channel_id=_get_int("LOG_CHANNEL_ID"),
            appeal_channel_id=_get_int("APPEAL_CHANNEL_ID"),
            ticket_channel_id=_get_int("TICKET_CHANNEL_ID"),
            transcript_channel_id=_get_int("TRANSCRIPT_CHANNEL_ID"),
            match_results_channel_id=_get_int("MATCH_RESULTS_CHANNEL_ID"),
            match_assignments_channel_id=_get_int("MATCH_ASSIGNMENTS_CHANNEL_ID"),
            match_staff_alert_channel_id=_get_int("MATCH_STAFF_ALERT_CHANNEL_ID"),
            schedule_channel_id=_get_int("SCHEDULE_CHANNEL_ID"),

            match_category_id=_get_int("MATCH_CATEGORY_ID"),
            general_support_category_id=_get_int("GENERAL_SUPPORT_CATEGORY_ID"),
            ranked_support_category_id=_get_int("RANKED_SUPPORT_CATEGORY_ID"),
            management_support_category_id=_get_int("MANAGEMENT_SUPPORT_CATEGORY_ID"),

            appeal_server_id=_get_int("APPEAL_SERVER_ID"),
            appeal_server_invite=os.getenv("APPEAL_SERVER_INVITE", "https://discord.gg"),

            groq_api_key=os.getenv("GROQ_API_KEY"),
            groq_model=os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile"),
            ai_admin_ids=_get_int_list("AI_ADMIN_IDS"),  # ← ADDED

            challonge_username=os.getenv("CHALLONGE_USERNAME"),
            challonge_api_key=os.getenv("CHALLONGE_API_KEY"),
            challonge_tournament=os.getenv("CHALLONGE_TOURNAMENT"),

            web_host=os.getenv("WEB_HOST"),
            web_port=_get_int("WEB_PORT"),
        )