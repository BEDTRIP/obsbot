import os
from dataclasses import dataclass
from typing import List, Optional, Set


def _split_list(value: Optional[str]) -> List[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def _to_bool(value: Optional[str], default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    http_port: int

    storage_dir: str
    attachments_subdir: str
    timezone: str

    whitelist_emails: Set[str]
    whitelist_tg_usernames: Set[str]
    whitelist_tg_ids: Set[int]

    telegram_bot_token: Optional[str]
    telegram_notify_chat_id: Optional[int]

    imap_host: Optional[str]
    imap_port: int
    imap_user: Optional[str]
    imap_password: Optional[str]
    imap_ssl: bool
    imap_poll_interval: int


def load_settings() -> Settings:
    http_port = int(os.getenv("HTTP_PORT", "8080"))

    storage_dir = os.getenv("STORAGE_DIR", "/folder").rstrip("/\\")
    attachments_subdir = os.getenv("ATTACHMENTS_SUBDIR", "attachments").strip()
    timezone = os.getenv("TIMEZONE", "Europe/Moscow").strip() or "Europe/Moscow"

    whitelist_emails = {e.lower() for e in _split_list(os.getenv("WHITELIST_EMAILS"))}
    whitelist_tg_usernames = {u.lower().lstrip("@") for u in _split_list(os.getenv("WHITELIST_TG_USERNAMES"))}
    whitelist_tg_ids_raw = _split_list(os.getenv("WHITELIST_TG_IDS"))
    whitelist_tg_ids: Set[int] = set()
    for item in whitelist_tg_ids_raw:
        try:
            whitelist_tg_ids.add(int(item))
        except ValueError:
            continue

    telegram_bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    notify_chat = os.getenv("TELEGRAM_NOTIFY_CHAT_ID")
    telegram_notify_chat_id = int(notify_chat) if notify_chat and notify_chat.strip() else None

    imap_host = os.getenv("IMAP_HOST")
    imap_port = int(os.getenv("IMAP_PORT", "993"))
    imap_user = os.getenv("IMAP_USER")
    imap_password = os.getenv("IMAP_PASSWORD")
    imap_ssl = _to_bool(os.getenv("IMAP_SSL", "true"), default=True)
    imap_poll_interval = int(os.getenv("IMAP_POLL_INTERVAL", "60"))

    return Settings(
        http_port=http_port,
        storage_dir=storage_dir,
        attachments_subdir=attachments_subdir,
        timezone=timezone,
        whitelist_emails=whitelist_emails,
        whitelist_tg_usernames=whitelist_tg_usernames,
        whitelist_tg_ids=whitelist_tg_ids,
        telegram_bot_token=telegram_bot_token,
        telegram_notify_chat_id=telegram_notify_chat_id,
        imap_host=imap_host,
        imap_port=imap_port,
        imap_user=imap_user,
        imap_password=imap_password,
        imap_ssl=imap_ssl,
        imap_poll_interval=imap_poll_interval,
    )


