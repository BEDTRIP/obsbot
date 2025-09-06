import asyncio
import os
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
from telegram import Bot, Update, ReactionTypeEmoji
from telegram.constants import ChatType
from telegram.ext import Application, ApplicationBuilder, ContextTypes, MessageHandler, filters

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


# ===================== Telegram =====================
def _is_tg_user_whitelisted(username: Optional[str], user_id: Optional[int], settings) -> bool:
    if user_id is not None and user_id in settings.whitelist_tg_ids:
        return True
    if username:
        return username.lower().lstrip("@") in settings.whitelist_tg_usernames
    return False


async def _download_and_save_telegram_attachments(update: Update, context: ContextTypes.DEFAULT_TYPE, storage: Storage) -> tuple[list[str], list[tuple[str, str]]]:
    saved_names: list[str] = []
    display_and_saved: list[tuple[str, str]] = []
    message = update.effective_message
    if not message:
        return saved_names

    async def _save_file(file_id: str, suggested_name: str, display_name: Optional[str] = None) -> None:
        try:
            tg_file = await context.bot.get_file(file_id)
            # Сохраняем напрямую в каталог вложений
            target_name = suggested_name
            # Гарантируем уникальность имени
            base_path = os.path.join(storage.attachments_dir, target_name)
            unique_path = base_path
            counter = 1
            while os.path.exists(unique_path):
                name, ext = os.path.splitext(target_name)
                unique_path = os.path.join(storage.attachments_dir, f"{name}_{counter}{ext}")
                counter += 1
            await tg_file.download_to_drive(custom_path=unique_path)
            saved_names.append(os.path.basename(unique_path))
            display_and_saved.append((display_name or suggested_name, os.path.basename(unique_path)))
            logger.info(f"Сохранён файл Telegram: {unique_path}")
        except Exception as e:
            logger.warning(f"Не удалось сохранить вложение Telegram {suggested_name}: {e}")

    # Документы, аудио, видео, фото, голос, стикер, гиф (animation), видеосообщение
    if message.document:
        file_name = message.document.file_name or f"document_{message.document.file_unique_id}"
        await _save_file(message.document.file_id, file_name, display_name=message.document.file_name)
    if message.audio:
        file_name = message.audio.file_name or f"audio_{message.audio.file_unique_id}.mp3"
        await _save_file(message.audio.file_id, file_name, display_name=message.audio.file_name)
    if message.voice:
        file_name = f"voice_{message.voice.file_unique_id}.ogg"
        await _save_file(message.voice.file_id, file_name)
    if message.video:
        file_name = message.video.file_name or f"video_{message.video.file_unique_id}.mp4"
        await _save_file(message.video.file_id, file_name)
    if message.video_note:
        file_name = f"video_note_{message.video_note.file_unique_id}.mp4"
        await _save_file(message.video_note.file_id, file_name)
    if message.animation:
        file_name = message.animation.file_name or f"animation_{message.animation.file_unique_id}.mp4"
        await _save_file(message.animation.file_id, file_name)
    if message.sticker:
        # Стикеры могут быть .webp/.tgs; Telegram отдаст реальный файл
        ext = ".webp"
        if message.sticker.is_animated:
            ext = ".tgs"
        file_name = f"sticker_{message.sticker.file_unique_id}{ext}"
        await _save_file(message.sticker.file_id, file_name)
    if message.photo:
        # Берём самое большое фото
        photo = message.photo[-1]
        file_name = f"photo_{photo.file_unique_id}.jpg"
        await _save_file(photo.file_id, file_name)

    return saved_names, display_and_saved


async def telegram_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = context.application.bot_data.get("settings")
    storage: Storage = context.application.bot_data.get("storage")
    message = update.effective_message
    if not message or not settings or not storage:
        return

    # Отбрасываем ботов
    if message.from_user and message.from_user.is_bot:
        return

    chat = message.chat
    user = message.from_user
    username = user.username if user else None
    user_id = user.id if user else None

    in_target_group = settings.telegram_notify_chat_id is not None and chat.id == settings.telegram_notify_chat_id
    is_private_and_whitelisted = (chat.type == ChatType.PRIVATE) and _is_tg_user_whitelisted(username, user_id, settings)

    if not (in_target_group or is_private_and_whitelisted):
        return

    # Текст сообщения или подпись к медиа
    text_body = message.text or message.caption or ""

    # Сохраняем медиа в attachments
    saved_attachment_names, display_and_saved = await _download_and_save_telegram_attachments(update, context, storage)

    # Лейбл для имени файла, если нет текста
    fallback_label: Optional[str] = None
    has_text = bool(text_body.strip())
    if not has_text:
        # Только эти типы жёстко задают лейбл. Для остальных возьмём имя файла.
        if message.animation:
            fallback_label = "Gif"
        elif message.voice:
            fallback_label = "Голосовое сообщение"
        elif message.sticker:
            fallback_label = "Стикер"
        elif not saved_attachment_names:
            # Нет текста и нет вложений — общее название
            fallback_label = "Сообщение"

    # Если это часть альбома (media group) — агрегируем и отложенно сохраняем одним файлом
    if message.media_group_id is not None:
        group_key = f"{chat.id}:{message.media_group_id}"
        media_groups = context.application.bot_data.setdefault("media_groups", {})
        state = media_groups.get(group_key)
        if not state:
            state = {
                "chat_id": chat.id,
                "message_ids": [],
                "saved_names": [],
                "display_and_saved": [],
                "text_body": "",
                "username": username,
                "user_id": user_id,
                "subject": chat.title or (username or ""),
                "has_animation": False,
                "has_voice": False,
                "has_sticker": False,
                "task": None,
            }
            media_groups[group_key] = state

        state["message_ids"].append(message.message_id)
        state["saved_names"].extend(saved_attachment_names)
        state["display_and_saved"].extend(display_and_saved)
        if text_body and not state["text_body"]:
            state["text_body"] = text_body
        # Флаги типов для лейблов
        if message.animation:
            state["has_animation"] = True
        if message.voice:
            state["has_voice"] = True
        if message.sticker:
            state["has_sticker"] = True

        # Перепланируем финализацию группы: 1.2 секунды после последнего элемента (через asyncio)
        try:
            if state.get("task"):
                try:
                    state["task"].cancel()
                except Exception:
                    pass
            state["task"] = asyncio.create_task(_finalize_media_group_after_delay(context.application, group_key, 1.2))
            logger.info(f"Запланирована финализация media_group {group_key} через 1.2с, элементов уже: {len(state['saved_names'])}")
        except Exception as e:
            logger.error(f"Не удалось запланировать финализацию media_group {group_key}: {e}")

        # Поки что ничего не сохраняем (ждём финализации группы)
        return

    # Заголовочную часть (subject) можно проставить чатом/юзером — не влияет на имя файла для telegram
    subject = chat.title or (username or "")
    sender = username or str(user_id or "unknown")

    try:
        path = storage.save_markdown_message(
            source="telegram",
            sender=sender,
            subject=subject,
            text_body=text_body,
            attachments=None,
            pre_saved_attachment_names=saved_attachment_names,
            pre_saved_attachments=display_and_saved,
            extra_meta={"tg_title_label": fallback_label} if fallback_label else None,
        )
        # Реакция на сообщение вместо ответа
        try:
            await context.bot.set_message_reaction(
                chat_id=message.chat_id,
                message_id=message.message_id,
                reaction=[ReactionTypeEmoji(emoji="👍")],
                is_big=False,
            )
        except Exception:
            pass
        logger.info(f"Сообщение Telegram сохранено: {path}")
    except Exception as e:
        logger.error(f"Ошибка сохранения сообщения из Telegram: {e}")
        try:
            await context.bot.set_message_reaction(
                chat_id=message.chat_id,
                message_id=message.message_id,
                reaction=[ReactionTypeEmoji(emoji="⚠️")],
                is_big=False,
            )
        except Exception:
            pass


async def telegram_worker(settings, storage: Storage):
    if not settings.telegram_bot_token:
        return
    application: Application = ApplicationBuilder().token(settings.telegram_bot_token).build()
    application.bot_data["settings"] = settings
    application.bot_data["storage"] = storage

    # Обрабатываем все сообщения (текст/медиа), а в хендлере фильтруем сами
    application.add_handler(MessageHandler(filters.ALL & ~filters.StatusUpdate.ALL, telegram_message_handler))

    await application.initialize()
    await application.start()
    try:
        await application.updater.start_polling(allowed_updates=Update.ALL_TYPES)
        # Держим задачу живой
        while True:
            await asyncio.sleep(3600)
    finally:
        await application.updater.stop()
        await application.stop()
        await application.shutdown()


async def _finalize_media_group_after_delay(application: Application, key: str, delay: float) -> None:
    try:
        await asyncio.sleep(delay)
        app_data = application.bot_data
        media_groups = app_data.get("media_groups", {})
        state = media_groups.pop(key, None)
        if not state:
            return
        settings = app_data.get("settings")
        storage: Storage = app_data.get("storage")
        if not settings or not storage:
            return

        chat_id = state["chat_id"]
        message_ids = state["message_ids"]
        saved_names = state["saved_names"]
        display_and_saved = state["display_and_saved"]
        text_body = state["text_body"]
        username = state["username"]
        user_id = state["user_id"]
        subject = state["subject"]

        # Лейбл только для особых типов, иначе используем имя первого файла
        fallback_label = None
        if state.get("has_animation"):
            fallback_label = "Gif"
        elif state.get("has_voice"):
            fallback_label = "Голосовое сообщение"
        elif state.get("has_sticker"):
            fallback_label = "Стикер"
        elif not saved_names and not (text_body and text_body.strip()):
            fallback_label = "Сообщение"

        sender = username or str(user_id or "unknown")
        path = storage.save_markdown_message(
            source="telegram",
            sender=sender,
            subject=subject,
            text_body=text_body,
            attachments=None,
            pre_saved_attachment_names=saved_names,
            pre_saved_attachments=display_and_saved,
            extra_meta={"tg_title_label": fallback_label} if fallback_label else None,
        )
        logger.info(f"Media group сохранён: {path}")
        # Реакции на все сообщения альбома
        try:
            for mid in message_ids:
                await application.bot.set_message_reaction(
                    chat_id=chat_id,
                    message_id=mid,
                    reaction=[ReactionTypeEmoji(emoji="👍")],
                    is_big=False,
                )
        except Exception:
            pass
    except asyncio.CancelledError:
        return
    except Exception as e:
        logger.error(f"Ошибка финализации media_group {key}: {e}")


async def finalize_media_group(context: ContextTypes.DEFAULT_TYPE) -> None:
    data = context.job.data or {}
    key = data.get("key")
    if not key:
        return
    app_data = context.application.bot_data
    media_groups = app_data.get("media_groups", {})
    state = media_groups.pop(key, None)
    if not state:
        return
    settings = app_data.get("settings")
    storage: Storage = app_data.get("storage")
    if not settings or not storage:
        return


    chat_id = state["chat_id"]
    message_ids = state["message_ids"]
    saved_names = state["saved_names"]
    display_and_saved = state["display_and_saved"]
    text_body = state["text_body"]
    username = state["username"]
    user_id = state["user_id"]
    subject = state["subject"]

    # Лейбл только для особых типов, иначе используем имя первого файла
    fallback_label = None
    if state.get("has_animation"):
        fallback_label = "Gif"
    elif state.get("has_voice"):
        fallback_label = "Голосовое сообщение"
    elif state.get("has_sticker"):
        fallback_label = "Стикер"
    elif not saved_names and not (text_body and text_body.strip()):
        fallback_label = "Сообщение"

    sender = username or str(user_id or "unknown")
    path = storage.save_markdown_message(
        source="telegram",
        sender=sender,
        subject=subject,
        text_body=text_body,
        attachments=None,
        pre_saved_attachment_names=saved_names,
        pre_saved_attachments=display_and_saved,
        extra_meta={"tg_title_label": fallback_label} if fallback_label else None,
    )
    logger.info(f"Media group сохранён: {path}")
    # Ставим реакции на все сообщения этой группы
    try:
        for mid in message_ids:
            await context.bot.set_message_reaction(
                chat_id=chat_id,
                message_id=mid,
                reaction=[ReactionTypeEmoji(emoji="👍")],
                is_big=False,
            )
    except Exception:
        pass

async def main_async():
    settings = load_settings()
    storage = Storage(settings.storage_dir, settings.attachments_subdir, settings.timezone)

    imap_task = asyncio.create_task(imap_worker(settings, storage))
    http_task = asyncio.create_task(run_http_server(settings.http_port))
    tg_task = asyncio.create_task(telegram_worker(settings, storage))

    await asyncio.gather(imap_task, http_task, tg_task)


def main():
    asyncio.run(main_async())


if __name__ == "__main__":
    main()


