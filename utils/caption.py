import html
import re
from datetime import timezone

from utils.tgsend import clip, esc

CAPTION_TZ = timezone.utc
CAPTION_LIMIT = 1024

KIND_PLURAL = {"post": "Posts", "reel": "Reels", "story": "Stories", "highlight": "Highlight"}


def short_num(value) -> str | None:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    if n < 0:
        return None
    if n < 1000:
        return str(n)
    if round(n / 1000, 1) < 1000:
        return f"{n / 1000:.1f}".rstrip("0").rstrip(".") + "K"
    return f"{n / 1_000_000:.1f}".rstrip("0").rstrip(".") + "M"


def fmt_size(num_bytes: int) -> str:
    if num_bytes < 1024 * 1024:
        return f"{max(1, round(num_bytes / 1024))}KB"
    mb = round(num_bytes / (1024 * 1024), 1)
    return f"{mb:g}MB"


def fmt_date(taken) -> str | None:
    try:
        return taken.astimezone(CAPTION_TZ).strftime("%Y/%m/%d %H:%M:%S")
    except (AttributeError, ValueError, OSError):
        return None


def build_caption(kind: str, url: str, size_bytes: int, meta: dict | None) -> str:
    meta = meta or {}
    stats = []
    likes, comments = short_num(meta.get("likes")), short_num(meta.get("comments"))
    if likes is not None:
        stats.append(f"❤️ {likes}")
    if comments is not None:
        stats.append(f"💬 {comments}")
    if meta.get("author"):
        stats.append(f"👤 {esc(meta['author'])}")
    when = fmt_date(meta.get("date")) if meta.get("date") is not None else None
    if when:
        stats.append(f"📅 {when}")
    head = f"📥 <b>Instagram {esc(kind.title())}</b>"
    tail = f'<a href="{html.escape(url, quote=True)}">Open on Instagram</a>\nSize: {fmt_size(size_bytes)}'
    text = (meta.get("caption") or "").strip()
    limit = 800
    while True:
        parts = [head]
        if text:
            parts.append(esc(clip(text, limit)))
        if stats:
            parts.append("\n".join(stats))
        parts.append(tail)
        caption = "\n\n".join(parts)
        if len(caption) <= CAPTION_LIMIT or limit <= 50:
            return caption
        limit -= 100


def build_zip_caption(kind: str, username: str, url: str, files: int, size_bytes: int, part: int, parts: int, items: int) -> str:
    head = f"📦 <b>Instagram {esc(KIND_PLURAL.get(kind, kind.title()))} (ZIP)</b>"
    lines = [f"👤 {esc(username)}"]
    lines.append(f"🗂 {files} file{'s' if files != 1 else ''}" + (f" · {items} {'item' if items == 1 else 'items'} in total" if parts > 1 else ""))
    if parts > 1:
        lines.append(f"📎 Part {part} of {parts}")
    tail = f'<a href="{html.escape(url, quote=True)}">Open on Instagram</a>\nSize: {fmt_size(size_bytes)}'
    return "\n\n".join([head, "\n".join(lines), tail])


def label_of(m, kind: str) -> str:
    if kind in ("story", "highlight", "reel"):
        return kind
    media_type = int(getattr(m, "media_type", 0) or 0)
    product = str(getattr(m, "product_type", "") or "").lower()
    if media_type == 8:
        return "album"
    if media_type == 2:
        return "reel" if product == "clips" else "video"
    return "photo"


def link_of(m, label: str, username: str, highlight_url: str = "") -> str:
    if label == "highlight" and highlight_url:
        return highlight_url
    if label == "story":
        pk = getattr(m, "pk", None)
        return f"https://www.instagram.com/stories/{username}/{pk}/" if pk else f"https://www.instagram.com/{username}/"
    code = getattr(m, "code", None)
    if not code:
        return f"https://www.instagram.com/{username}/"
    return f"https://www.instagram.com/{'reel' if label == 'reel' else 'p'}/{code}/"


def meta_of(m, username: str, text: str | None = None, date=None) -> dict:
    user = getattr(m, "user", None)
    return {
        "caption": text if text is not None else (getattr(m, "caption_text", "") or ""),
        "likes": getattr(m, "like_count", None),
        "comments": getattr(m, "comment_count", None),
        "author": getattr(user, "username", None) or username,
        "date": date if date is not None else getattr(m, "taken_at", None),
    }


def slug(text: str, default: str = "item", limit: int = 30) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "_", (text or "").strip()).strip("_")
    return cleaned[:limit] or default


def info_entry(index: int, label: str, link: str, meta: dict) -> str:
    bits = [f"[{index}] {label.title()} - {link}"]
    when = fmt_date(meta.get("date")) if meta.get("date") is not None else None
    if when:
        bits.append(f"Date: {when} UTC")
    likes, comments = short_num(meta.get("likes")), short_num(meta.get("comments"))
    if likes is not None:
        bits.append(f"Likes: {likes}")
    if comments is not None:
        bits.append(f"Comments: {comments}")
    text = (meta.get("caption") or "").strip()
    return "\n".join(bits) + (f"\n{text}" if text else "")
