import asyncio
import email
import email.policy
from email.message import EmailMessage
from typing import Optional
from datetime import datetime
from zoneinfo import ZoneInfo

import logging
import uvicorn
from fastapi import FastAPI
from imapclient import IMAPClient
from telegram import Bot

from .config import load_settings
from .storage import Storage


app = FastAPI()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)


async def run_http_server(port: int):
    config = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()


@app.get("/health")
def health():
    return {"ok": True}


def is_email_whitelisted(sender: Optional[str], whitelist: set[str]) -> bool:
    if not sender:
        return False
    return sender.lower() in whitelist


def parse_email_message(msg_bytes: bytes) -> tuple[str, Optional[str], Optional[str], list[tuple[str, bytes]]]:
    msg: EmailMessage = email.message_from_bytes(msg_bytes, policy=email.policy.default)  # type: ignore
    sender = msg.get("From")
    subject = msg.get("Subject")
    text_body: Optional[str] = None
    html_body: Optional[str] = None
    attachments: list[tuple[str, bytes]] = []

    if msg.is_multipart():
        for part in msg.walk():
            content_disposition = part.get_content_disposition()
            content_type = part.get_content_type()
            if content_disposition == "attachment":
                filename = part.get_filename() or "attachment"
                payload = part.get_payload(decode=True) or b""
                attachments.append((filename, payload))
            elif content_type == "text/plain" and text_body is None:
                payload = part.get_payload(decode=True) or b""
                text_body = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
            elif content_type == "text/html" and html_body is None:
                payload = part.get_payload(decode=True) or b""
                html_body = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
    else:
        content_type = msg.get_content_type()
        payload = msg.get_payload(decode=True) or b""
        if content_type == "text/plain":
            text_body = payload.decode(msg.get_content_charset() or "utf-8", errors="replace")
        elif content_type == "text/html":
            html_body = payload.decode(msg.get_content_charset() or "utf-8", errors="replace")

    return sender or "", subject, text_body or html_body or "", attachments


async def imap_worker(settings, storage: Storage):
    if not settings.imap_host or not settings.imap_user or not settings.imap_password:
        return
    bot: Optional[Bot] = None
    if settings.telegram_bot_token and settings.telegram_notify_chat_id:
        try:
            bot = Bot(token=settings.telegram_bot_token)
        except Exception:
            bot = None
    while True:
        try:
            ssl = settings.imap_ssl
            with IMAPClient(settings.imap_host, port=settings.imap_port, ssl=ssl) as client:
                client.login(settings.imap_user, settings.imap_password)
                client.select_folder("INBOX")
                messages = client.search(["UNSEEN"])  # простая стратегия: только новые
                for uid in messages:
                    raw = client.fetch([uid], ["RFC822"])  # type: ignore
                    msg_bytes: bytes = raw[uid][b"RFC822"]  # type: ignore
                    sender, subject, body, attachments = parse_email_message(msg_bytes)
                    # Извлечём email-адрес из поля From
                    sender_email = sender
                    if "<" in sender and ">" in sender:
                        sender_email = sender.split("<")[-1].split(">")[0].strip()
                    time_str = datetime.now(ZoneInfo(settings.timezone)).strftime("%H:%M")
                    logger.info(f"Получено письмо UID={uid} от {sender_email} с темой '{subject}'")
                    if is_email_whitelisted(sender_email, settings.whitelist_emails):
                        logger.info(f"Письмо {uid} от {sender_email} прошло проверку белого списка")
                        try:
                            path = storage.save_markdown_message(
                                source="email",
                                sender=sender_email,
                                subject=subject,
                                text_body=body,
                                attachments=attachments,
                            )
                            # Реакция: отметить прочитанным
                            client.add_flags([uid], [b"\\Seen"])  # type: ignore
                            logger.info(f"Письмо сохранено: {path}")
                            # Уведомление в Telegram (только сообщение "принято ...")
                            if bot and settings.telegram_notify_chat_id:
                                try:
                                    await bot.send_message(
                                        chat_id=settings.telegram_notify_chat_id,
                                        text=f"Сообщение от {sender_email} в {time_str} записано"
                                    )
                                except Exception as e:
                                    logger.warning(f"Не удалось отправить уведомление в Telegram о письме {uid} от {sender_email}: {e}")
                        except Exception as e:
                            logger.error(f"Ошибка сохранения письма {uid} от {sender_email}: {e}")
                            if bot and settings.telegram_notify_chat_id:
                                try:
                                    await bot.send_message(
                                        chat_id=settings.telegram_notify_chat_id,
                                        text=f"Получено письмо от {sender_email} в {time_str}, ошибка сохранения (прикрепленный файл или все письмо не сохранено)"
                                    )
                                except Exception as e2:
                                    logger.warning(f"Не удалось отправить уведомление об ошибке в Telegram о письме {uid} от {sender_email}: {e2}")
                    else:
                        logger.info(f"Письмо от {sender_email} в {time_str} не прошло проверку белого списка")
                        if bot and settings.telegram_notify_chat_id:
                            try:
                                await bot.send_message(
                                    chat_id=settings.telegram_notify_chat_id,
                                    text=f"Сообщение от {sender_email} в {time_str} проигнорировано"
                                )
                            except Exception as e:
                                logger.warning(f"Не удалось отправить уведомление о не-белом письме в Telegram о письме {uid} от {sender_email}: {e}")
                await asyncio.sleep(settings.imap_poll_interval)
        except Exception as e:
            logger.error(f"Ошибка IMAP-цикла: {e}")
            await asyncio.sleep(max(10, settings.imap_poll_interval))

async def main_async():
    settings = load_settings()
    storage = Storage(settings.storage_dir, settings.attachments_subdir, settings.timezone)

    imap_task = asyncio.create_task(imap_worker(settings, storage))
    http_task = asyncio.create_task(run_http_server(settings.http_port))

    await asyncio.gather(imap_task, http_task)


def main():
    asyncio.run(main_async())


if __name__ == "__main__":
    main()


