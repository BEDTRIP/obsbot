# obsbot

Программа для сохранения сообщений из IMAP-почты и Telegram-групп в Markdown-файлы с вложениями. Работает в Docker.

## Возможности
- Поддержка IMAP: чтение новых писем, фильтрация по белому списку отправителей, сохранение содержимого и вложений
- Поддержка Telegram-бота: работа в группах, фильтрация отправителей, сохранение сообщений и медиа, реакция на сохранённые сообщения
- Уведомление в Telegram при сохранении письма с почты
- Сохранение в общую папку на хосте: `/folder` (маппится через volume)
- HTTP `/health` для проверки статуса

## Запуск
1. Заполните переменные окружения в `docker-compose.yaml` (см. раздел Конфигурация)
2. Подготовьте директорию для данных на хосте: `mkdir -p data/folder`
3. Соберите и запустите:
   ```bash
   docker compose up -d --build
   ```

После старта:
- HTTP-эндпоинт доступен на `http://localhost:8080/health`
- Все сохранённые материалы появятся в `./data/folder` на вашей машине

### Деплой на VPS за Traefik (HTTPS)
1. На VPS уже должен работать Traefik в режиме Docker provider и внешняя сеть, например `proxy`.
2. Создайте файл `.env` рядом с `docker-compose.yaml` и заполните (пример ниже). Можно взять за основу `env.example`.
3. В `docker-compose.yaml` ничего дополнительно менять не нужно — домен и сеть подтянутся из `.env`.
4. Запустите `docker compose up -d --build`.

Пример `.env`:
```
DOMAIN=obsbot.example.ru
TRAEFIK_DOCKER_NETWORK=proxy
TRAEFIK_ENTRYPOINT_WEB=web
TRAEFIK_ENTRYPOINT_WEBSECURE=websecure
TRAEFIK_CERTRESOLVER=letsencrypt

# Приложение
HTTP_PORT=8080
STORAGE_DIR=/folder
ATTACHMENTS_SUBDIR=attachments
TIMEZONE=Europe/Moscow

# Whitelist (по желанию)
# WHITELIST_EMAILS=a@ex.ru,b@ex.ru
# WHITELIST_TG_USERNAMES=user1,user2
# WHITELIST_TG_IDS=111,222

# Telegram
TELEGRAM_BOT_TOKEN=123:abc
TELEGRAM_NOTIFY_CHAT_ID=-100123456789

# (Опционально) Локальный Bot API для файлов до 2 ГБ
# Укажите адреса, если запускаете сервис telegram-bot-api в этом compose:
# TELEGRAM_API_BASE_URL=https://your-domain-or-internal/telegram-bot-api/bot
# TELEGRAM_API_FILE_URL=https://your-domain-or-internal/telegram-bot-api/file/bot
# Таймауты (секунды) на длинные загрузки
TELEGRAM_CONNECT_TIMEOUT=30
TELEGRAM_READ_TIMEOUT=3600

# IMAP
IMAP_HOST=imap.example.ru
IMAP_PORT=993
IMAP_USER=user@example.ru
IMAP_PASSWORD=secret
IMAP_SSL=true
IMAP_POLL_INTERVAL=30
```

Traefik должен иметь entrypoints `web` и `websecure`, а также certresolver (например, `letsencrypt`). Роутинг происходит по значению `DOMAIN` и трафик проксируется на внутренний порт `8080` в контейнере.

#### Файлы до 2 ГБ через локальный Telegram Bot API
- Добавлен сервис `telegram-bot-api` (образ `aiogram/telegram-bot-api`). Для его работы нужны `TELEGRAM_API_ID` и `TELEGRAM_API_HASH` (получите на `https://my.telegram.org`).
- Запустите compose, получите внутренний адрес сервиса (например, `http://telegram-bot-api:8081`).
- Пропишите в `.env`:
  - `TELEGRAM_API_BASE_URL=http://telegram-bot-api:8081/bot`
  - `TELEGRAM_API_FILE_URL=http://telegram-bot-api:8081/file/bot`
- Бот начнёт скачивать/отправлять файлы до 2 ГБ, ограничения Telegram Bot API по умолчанию будут обойдены локальным сервером.

## Конфигурация (переменные окружения)
- `HTTP_PORT` — порт HTTP-сервера внутри контейнера (по умолчанию 8080)
- `STORAGE_DIR` — директория для сохранения сообщений (в контейнере), по умолчанию `/folder`
- `ATTACHMENTS_SUBDIR` — подпапка для вложений (по умолчанию `attachments`)
- `WHITELIST_EMAILS` — список email-адресов, через запятую
- `WHITELIST_TG_USERNAMES` — Telegram username без `@`, через запятую
- `WHITELIST_TG_IDS` — Telegram user ID, через запятую (числа)
- `TELEGRAM_BOT_TOKEN` — токен бота (получите у @BotFather)
- `TELEGRAM_NOTIFY_CHAT_ID` — чат, куда отправлять уведомления при сохранении почтового письма
- `IMAP_HOST`, `IMAP_PORT`, `IMAP_USER`, `IMAP_PASSWORD`, `IMAP_SSL` — настройки IMAP
- `IMAP_POLL_INTERVAL` — интервал опроса IMAP в секундах (по умолчанию 60)

## Важно для Telegram-групп
- Добавьте бота в группу
- Отключите у бота «privacy mode» в @BotFather, чтобы он видел все сообщения

## Формат сохранения
- Каждое сообщение сохраняется как Markdown-файл в `STORAGE_DIR`
- Вложения сохраняются в `STORAGE_DIR/ATTACHMENTS_SUBDIR`
- Внутри Markdown на вложения ставятся ссылки вида: `![[file.ext]]`

## Примечания
- Для HTML-писем выполняется конвертация в Markdown
- Если API реакций недоступен для вашего бота, бот ответит смайликом в потоке


