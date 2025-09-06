import os
import re
import uuid
import logging
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Optional, Tuple

from html2text import html2text

logger = logging.getLogger(__name__)


def _ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def _slugify(value: str) -> str:
    value = re.sub(r"[\n\r\t]+", " ", value).strip()
    value = re.sub(r"\s+", "-", value)
    value = re.sub(r"[^a-zA-Z0-9\-_.]", "", value)
    return value[:80] or str(uuid.uuid4())


def _sanitize_component(value: str, max_len: int = 80) -> str:
    # Сохраняем Unicode (в т.ч. кириллицу), удаляем только запрещённые для файлов символы
    value = value.replace("\n", " ").replace("\r", " ").replace("\t", " ")
    value = re.sub(r"\s+", " ", value).strip()
    # Удаляем недопустимые в именах файлов символы для Windows/Linux
    value = re.sub(r"[\\/:*?\"<>|]", "", value)
    # Ограничим длину компонента
    if len(value) > max_len:
        value = value[:max_len].rstrip()
    return value or "untitled"


def _clean_preview(text: str) -> str:
    # Удаляем HTML-теги
    text = re.sub(r"(?is)<[^>]+>", " ", text)
    # Удаляем системные HTML-слова, встречающиеся как отдельные токены
    html_tokens = r"div|span|p|br|hr|script|style|table|tr|td|thead|tbody|tfoot|ul|ol|li|html|body|head|meta|link|img|a|strong|em|b|i|u|h[1-6]"
    text = re.sub(rf"(?i)(?<![A-Za-z])(?:{html_tokens})(?![A-Za-z])", " ", text)
    # Сжимаем пробелы
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _strip_forward_headers(text: str) -> str:
    """Удаляет шапку пересылаемого письма (Yandex/Gmail) без извлечения дополнительных полей."""
    lines = text.splitlines()
    n = len(lines)
    import re as _re
    marker_re = _re.compile(r"^[-\s]{6,}(?:Пересылаемое сообщение|Forwarded message)[-\s]{2,}$", _re.IGNORECASE)
    start_idx = None
    for idx, line in enumerate(lines):
        if marker_re.match(line.strip()):
            start_idx = idx
            break
    if start_idx is None:
        return text
    i = start_idx + 1
    while i < n and lines[i].strip() != "":
        i += 1
    while i < n and lines[i].strip() == "":
        i += 1
    return "\n".join(lines[i:]).lstrip("\n")


class Storage:
    def __init__(self, base_dir: str, attachments_subdir: str = "attachments", timezone: str = "Europe/Moscow") -> None:
        self.base_dir = base_dir
        self.attachments_dir = os.path.join(base_dir, attachments_subdir)
        self.timezone = timezone
        _ensure_dir(self.base_dir)
        _ensure_dir(self.attachments_dir)

    def _unique_filename(self, prefix: str, suffix: str = ".md") -> str:
        timestamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
        unique = uuid.uuid4().hex[:6]
        base = f"{timestamp}-{_slugify(prefix)}-{unique}{suffix}"
        return os.path.join(self.base_dir, base)

    def save_markdown_message(
        self,
        source: str,
        sender: str,
        subject: Optional[str],
        text_body: Optional[str] = None,
        html_body: Optional[str] = None,
        attachments: Optional[list[Tuple[str, bytes]]] = None,
        extra_meta: Optional[dict] = None,
        pre_saved_attachment_names: Optional[list[str]] = None,
        pre_saved_attachments: Optional[list[Tuple[str, str]]] = None,
    ) -> str:
        _ensure_dir(self.base_dir)
        _ensure_dir(self.attachments_dir)

        if not text_body and html_body:
            try:
                text_body = html2text(html_body)
            except Exception:
                text_body = html_body

        text_body = text_body or ""
        # Удаляем шапку пересылаемых сообщений без парсинга отправителя
        text_body = _strip_forward_headers(text_body)
        # Определяем safe_subject и превью текста
        safe_subject = _sanitize_component(subject or "Письмо", max_len=100)
        text_preview_src = text_body or html_body or ""
        text_preview_clean = _clean_preview(text_preview_src)
        text_preview = _sanitize_component(text_preview_clean[:30], max_len=40)
        dt = datetime.now(ZoneInfo(self.timezone))
        timestamp = dt.strftime("%Y-%m-%d %H-%M-%S") + f".{dt.microsecond // 1000:03d}"
        # Для Telegram формируем имя файла только на основе текста/вложений, без subject
        if source == "telegram":
            # Явно заданный заголовок (из хендлера Telegram) имеет высший приоритет
            preview_for_name = None
            if extra_meta and isinstance(extra_meta, dict):
                explicit = extra_meta.get("tg_explicit_title")  # type: ignore
                if explicit:
                    preview_for_name = _sanitize_component(str(explicit)[:30], max_len=40)
            # Если явного названия нет — пробуем текст (но игнорируем псевдо-"untitled")
            if not preview_for_name:
                if text_body and text_body.strip() and text_preview and text_preview.lower() != "untitled":
                    preview_for_name = text_preview
            if not preview_for_name:
                # Приоритет 1: Явный лейбл типа сообщения (например, "Gif", "Стикер")
                label = None
                if extra_meta and isinstance(extra_meta, dict):
                    label = extra_meta.get("tg_title_label")  # type: ignore
                if label:
                    preview_for_name = _sanitize_component(str(label)[:30], max_len=40)
            if not preview_for_name:
                # Приоритет 2: Имя первого вложения (если есть). Для Telegram
                # предпочитаем оригинальное имя, если передано вместе с сохранённым.
                first_attach_display: Optional[str] = None
                if pre_saved_attachments and len(pre_saved_attachments) > 0:
                    display_name, saved_name = pre_saved_attachments[0]
                    first_attach_display = display_name or saved_name  # (display_name, saved_name)
                elif pre_saved_attachment_names and len(pre_saved_attachment_names) > 0:
                    first_attach_display = pre_saved_attachment_names[0]
                elif attachments and len(attachments) > 0:
                    first_attach_display = attachments[0][0]
                if first_attach_display:
                    preview_for_name = _sanitize_component(first_attach_display[:30], max_len=40)
            # Приоритет 3: Дефолт
            if not preview_for_name:
                preview_for_name = "Сообщение"
            file_base = preview_for_name or "untitled"
            filename = os.path.join(self.base_dir, f"{file_base} - {timestamp}.md")
        else:
            # Для писем, если и тема пустая, и текста нет — подставляем "Письмо"
            base_subject = safe_subject or "Письмо"
            base_preview = text_preview or "Письмо"
            filename = os.path.join(self.base_dir, f"{base_subject} - {base_preview} - {timestamp}.md")

        # Заголовок (front matter)
        date_str = datetime.now(ZoneInfo(self.timezone)).strftime("%Y-%m-%d")

        md_lines: list[str] = []
        md_lines.append("---")
        md_lines.append("tags:")
        if source == "telegram":
            md_lines.append("  - \"#input/telegram\"")
        else:
            md_lines.append("  - \"#input/mail\"")
        md_lines.append("Зачем_изучать?:")
        md_lines.append(f"date: \"[[{date_str}]]\"")
        md_lines.append("---")
        md_lines.append("## Сообщение")
        md_lines.append(text_body)
        md_lines.append("")

        has_any_attachments = (
            (attachments and len(attachments) > 0)
            or (pre_saved_attachment_names and len(pre_saved_attachment_names) > 0)
            or (pre_saved_attachments and len(pre_saved_attachments) > 0)
        )
        if has_any_attachments:
            # Для Telegram — заголовок из ТЗ, для остального — как раньше
            md_lines.append("## Вложение")
            # Предварительно сохранённые файлы (например, из Telegram)
            if pre_saved_attachments:
                for display_name, saved_name in pre_saved_attachments:
                    link_name = saved_name or uuid.uuid4().hex
                    md_lines.append(f"![[{link_name}]]")
            elif pre_saved_attachment_names:
                for saved_name in pre_saved_attachment_names:
                    link_name = saved_name or uuid.uuid4().hex
                    md_lines.append(f"![[{link_name}]]")
            # Вложения, переданные как байты (например, из email)
            if attachments:
                for original_name, blob in attachments:
                    safe_name = _slugify(original_name) if original_name else uuid.uuid4().hex
                    attach_path = os.path.join(self.attachments_dir, safe_name)
                    with open(attach_path, "wb") as f:
                        f.write(blob)
                    logger.info(f"Сохранено вложение: {attach_path}")
                    md_lines.append(f"![[{safe_name}]]")
            md_lines.append("")

        # Заключительный блок
        md_lines.append("---")
        md_lines.append("## Инпуты")
        md_lines.append(f"- [ ] Просмотреть 🔽⏳[[{date_str}]]")

        with open(filename, "w", encoding="utf-8") as f:
            f.write("\n".join(md_lines))
        logger.info(f"Сохранено письмо (Markdown): {filename}")

        return filename


