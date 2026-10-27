import asyncio
import html
from datetime import timezone
from typing import Awaitable, Callable

from telegram import InputFile, InputMediaPhoto, InputMediaVideo
from telegram.error import BadRequest, RetryAfter

from utils.logger import get_logger

log = get_logger("tgsend")

Fetch = Callable[[str], Awaitable["bytes | None"]]


def esc(text) -> str:
    return html.escape(str(text or ""), quote=False)


def clip(text: str, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def num(value) -> str:
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return "?"


DONE_REACTION = "👍"
_REJECTED_REACTIONS: set[str] = set()


async def set_reaction(bot, message, *emojis: str) -> None:
    try:
        if not emojis:
            await bot.set_message_reaction(message.chat_id, message.message_id, reaction=[])
            return
        for emoji in emojis:
            if emoji in _REJECTED_REACTIONS:
                continue
            try:
                await bot.set_message_reaction(message.chat_id, message.message_id, reaction=[emoji])
                return
            except BadRequest as e:
                log.info("Telegram does not allow the %s reaction for bots (%s)", emoji, e)
                _REJECTED_REACTIONS.add(emoji)
    except Exception as e:
        log.debug("Could not set the reaction (%s)", type(e).__name__)


async def tg_call(call):
    for _ in range(2):
        try:
            return await call()
        except RetryAfter as e:
            wait = e.retry_after.total_seconds() if hasattr(e.retry_after, "total_seconds") else float(e.retry_after)
            await asyncio.sleep(wait + 1)
    return await call()


def items_of(m) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    resources = getattr(m, "resources", None)
    if m.media_type == 8 and resources:
        for r in resources:
            if r.media_type == 2 and r.video_url:
                out.append(("video", str(r.video_url)))
            elif r.thumbnail_url:
                out.append(("photo", str(r.thumbnail_url)))
    elif m.media_type == 2 and m.video_url:
        out.append(("video", str(m.video_url)))
    elif m.thumbnail_url:
        out.append(("photo", str(m.thumbnail_url)))
    return out


def caption_of(m, kind: str) -> str:
    when = m.taken_at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC") if m.taken_at else ""
    if kind == "story":
        return f"📖 Story · {when}"
    text = esc(clip(getattr(m, "caption_text", "") or "", 600))
    link = f'<a href="https://www.instagram.com/p/{m.code}/">View on Instagram</a>' if getattr(m, "code", None) else ""
    stats = []
    likes = getattr(m, "like_count", -1)
    if likes is not None and likes >= 0:
        stats.append(f"❤️ {num(likes)}")
    if getattr(m, "comment_count", None) is not None:
        stats.append(f"💬 {num(m.comment_count)}")
    head = " · ".join(x for x in (when, *stats) if x)
    return "\n".join(x for x in (text, head, link) if x)


async def send_one(bot, chat_id: int, kind: str, data: bytes, caption: str, reply_markup=None) -> None:
    cap = caption[:1024] or None
    try:
        if kind == "photo":
            await tg_call(lambda: bot.send_photo(chat_id, photo=data, caption=cap, parse_mode="HTML", reply_markup=reply_markup))
        else:
            await tg_call(
                lambda: bot.send_video(
                    chat_id, video=InputFile(data, filename="video.mp4"), caption=cap, parse_mode="HTML", supports_streaming=True,
                    reply_markup=reply_markup, read_timeout=120, write_timeout=120,
                )
            )
    except BadRequest as e:
        log.info("Telegram refused the %s (%s), sending it as a file", kind, e)
        name = "photo.jpg" if kind == "photo" else "video.mp4"
        await tg_call(lambda: bot.send_document(chat_id, document=InputFile(data, filename=name), caption=cap, parse_mode="HTML", reply_markup=reply_markup, read_timeout=120, write_timeout=120))


async def fetch_items(items: list[tuple[str, str]], fetch: Fetch) -> list[tuple[str, bytes]]:
    files: list[tuple[str, bytes]] = []
    for kind, url in items:
        data = await fetch(url)
        if data:
            files.append((kind, data))
    return files


async def send_files(bot, chat_id: int, files: list[tuple[str, bytes]], caption: str) -> int:
    if not files:
        return 0
    for start in range(0, len(files), 10):
        chunk = files[start : start + 10]
        cap = caption if start == 0 else ""
        if len(chunk) == 1:
            await send_one(bot, chat_id, chunk[0][0], chunk[0][1], cap)
            continue
        media = []
        for i, (kind, data) in enumerate(chunk):
            first = cap[:1024] if i == 0 and cap else None
            if kind == "photo":
                media.append(InputMediaPhoto(media=data, caption=first, parse_mode="HTML" if first else None))
            else:
                media.append(InputMediaVideo(media=data, caption=first, parse_mode="HTML" if first else None, supports_streaming=True))
        try:
            await tg_call(lambda: bot.send_media_group(chat_id, media=media, read_timeout=180, write_timeout=180))
        except BadRequest as e:
            log.info("Album refused (%s), sending the files one by one", e)
            for i, (kind, data) in enumerate(chunk):
                await send_one(bot, chat_id, kind, data, cap if i == 0 else "")
    return len(files)


async def send_items(bot, chat_id: int, items: list[tuple[str, str]], caption: str, fetch: Fetch) -> int:
    return await send_files(bot, chat_id, await fetch_items(items, fetch), caption)
