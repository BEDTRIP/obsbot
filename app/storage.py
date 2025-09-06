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
        safe_subject = _sanitize_component(subject or "no-subject", max_len=100)
        text_preview_src = text_body or html_body or ""
        text_preview_clean = _clean_preview(text_preview_src)
        text_preview = _sanitize_component(text_preview_clean[:30], max_len=40)
        dt = datetime.now(ZoneInfo(self.timezone))
        timestamp = dt.strftime("%Y-%m-%d %H-%M-%S") + f".{dt.microsecond // 1000:03d}"
        filename = os.path.join(self.base_dir, f"{safe_subject} - {text_preview} - {timestamp}.md")

        # Заголовок (front matter)
        date_str = datetime.now(ZoneInfo(self.timezone)).strftime("%Y-%m-%d")

        md_lines: list[str] = []
        md_lines.append("---")
        md_lines.append("tags:")
        md_lines.append("  - \"#input/mail\"")
        md_lines.append("Зачем_изучать?:")
        md_lines.append(f"date: \"[[{date_str}]]\"")
        md_lines.append("---")
        md_lines.append("## Сообщение")
        md_lines.append(text_body)
        md_lines.append("")

        if attachments:
            md_lines.append("## Вложения")
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


