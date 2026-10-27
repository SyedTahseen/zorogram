import asyncio
import io
import json
import random
import re
import tempfile
import time
from types import SimpleNamespace
from dataclasses import dataclass, field
from pathlib import Path

import requests
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputFile
from telegram.error import BadRequest, TelegramError

from core.auth import describe_error
from modules.base import BaseModule, Command
from utils import media as media_utils
from utils.logger import get_logger
from utils.progress import Progress
from utils.caption import build_caption, build_zip_caption, info_entry, label_of, link_of, meta_of, slug
from utils.tgsend import clip as _clip, esc as _esc, fetch_items, items_of, num as _num, send_files, send_one, set_reaction, tg_call, DONE_REACTION
from utils.ui import BUSY, NOT_CONNECTED, STYLE_BY_LABEL, bad, button, screen
from utils.zipper import ZipParts

log = get_logger("dump")

USERNAME_RE = re.compile(r"^[A-Za-z0-9._]{1,30}$")
URL_RE = re.compile(r"^(?:https?://)?(?:www\.|m\.)?(?:instagram\.com|instagr\.am)/([A-Za-z0-9._]{1,30})/?(?:[?#].*)?$", re.I)
RESERVED = {"p", "reel", "reels", "stories", "explore", "tv", "accounts", "direct", "s"}

# No default count: without a number the bot reads everything, up to this safety cap.
POSTS_MAX = REELS_MAX = 500
LIST_DEFAULT, LIST_MAX = 200, 2000
HIGHLIGHT_BUTTONS = 20
ZIP_THRESHOLD = 10  # fewer than this many items are sent normally; this many or more ask "ZIP or one by one?"
PENDING_TTL = 30 * 60
PREFIX = "dump"
NOT_USERNAMES = {
    "hi", "hii", "hello", "hey", "yo", "thanks", "thank", "thx", "ty", "ok", "okay", "yes", "no", "yep", "nope",
    "test", "start", "help", "stop", "cancel", "bye", "lol", "wow", "nice", "good", "cool", "done", "sure",
}


@dataclass
class Batch:
    medias: list
    kind: str  # post | reel | story | highlight
    username: str
    title: str = ""  # highlight title
    url: str = ""  # highlight link
    date: object = None  # highlight date
    capped: bool = False  # the account has more than the safety cap
    made: float = field(default_factory=time.monotonic)


def parse_target(raw: str) -> str | None:
    raw = (raw or "").strip().lstrip("@")
    match = URL_RE.match(raw)
    if match:
        name = match.group(1)
        return None if name.lower() in RESERVED else name.lower()
    return raw.lower() if USERNAME_RE.match(raw) else None


def _btn(text: str, act: str, arg: str) -> InlineKeyboardButton:
    return button(text, STYLE_BY_LABEL.get(text), callback_data=f"m:{PREFIX}:{act}:{arg}")


class DumpModule(BaseModule):
    name = "dump"
    toggleable = True
    label = "Profile tools"
    description = "Send a username to open a public profile, or use /profile, /posts and the other profile commands"

    def __init__(self, ctx):
        super().__init__(ctx)
        self._busy: set[int] = set()
        self._private: dict[str, bool] = {}
        self._awaiting: dict[int, tuple[str, str]] = {}
        self._fmt: dict[int, str] = {}
        self._pending: dict[int, tuple[int, Batch]] = {}
        self._seq = 0
        self._search: dict[int, tuple[str, list[tuple[str, str, bool, bool]]]] = {}

    def get_commands(self):
        return {
            "search": Command(self.cmd_search, "Find accounts by name", "/search <name>", takes_update=True),
            "profile": Command(self.cmd_profile, "Public profile info", "/profile <username or link>", takes_update=True),
            "pfp": Command(self.cmd_pfp, "Profile picture (full size)", "/pfp <username>", takes_update=True),
            "posts": Command(self.cmd_posts, f"All posts, or the latest N (up to {POSTS_MAX})", "/posts <username> [count]", takes_update=True),
            "reels": Command(self.cmd_reels, f"All reels, or the latest N (up to {REELS_MAX})", "/reels <username> [count]", takes_update=True),
            "stories": Command(self.cmd_stories, "Current stories", "/stories <username>", takes_update=True),
            "highlights": Command(self.cmd_highlights, "Highlights list with buttons", "/highlights <username>", takes_update=True),
            "followers": Command(self.cmd_followers, f"Followers list as a file (up to {LIST_MAX})", "/followers <username> [count] [json]", takes_update=True),
            "following": Command(self.cmd_following, f"Following list as a file (up to {LIST_MAX})", "/following <username> [count] [json]", takes_update=True),
        }

    def get_callbacks(self):
        return {PREFIX: self.on_button}

    def _need_login(self) -> None:
        if not self.ctx.ig.logged_in:
            raise RuntimeError(NOT_CONNECTED)

    async def _ig(self, fn):
        self._need_login()
        return await self.ctx.ig._call(fn)

    async def _profile(self, username: str):
        user = await self._ig(lambda: self.ctx.ig.cl.user_info_by_username(username))
        self._private[username] = bool(user.is_private)
        return user

    async def _public_profile(self, username: str):
        user = await self._profile(username)
        if user.is_private:
            raise PermissionError("That account is private. I only work with public accounts.")
        return user

    def _explain(self, e: Exception) -> str:
        if isinstance(e, PermissionError):
            return str(e)
        if isinstance(e, RuntimeError):
            return f"{e}"
        try:
            from instagrapi import exceptions as ig_exc

            if isinstance(e, (ig_exc.UserNotFound, ig_exc.ClientNotFoundError)):
                return "That account was not found."
            if isinstance(e, ig_exc.PrivateAccount):
                return "That account is private. I only work with public accounts."
            info = describe_error(e, self.ctx.ig.cl)
            return f"{info.text}"
        except Exception:
            return f"{type(e).__name__}: {e}"

    @staticmethod
    def _read_about(cl, pk: str) -> dict:
        """About this account. instagrapi's own parser raises IndexError or a ValidationError on many accounts, so fall back to reading its raw reply."""
        try:
            about = cl.user_about_v1(pk)
            return {"country": about.country, "date_joined": about.date, "former_usernames": about.former_usernames,
                    "is_verified": about.is_verified}
        except Exception:  # IndexError / pydantic ValidationError from the library's parser
            pass
        reply = cl.last_json or {}
        country, joined, former = None, None, None
        for item in (((reply.get("layout") or {}).get("bloks_payload") or {}).get("data")) or []:
            data = item.get("data") or {}
            if str(data.get("key", "")).endswith("about_this_account_country") and isinstance(data.get("initial"), str):
                country = data["initial"]
        parts = json.dumps(reply).split('")":')
        date_next = False
        for i, part in enumerate(parts):
            if date_next:
                joined = part.strip().split(",")[0][1:-1]
            if "Former usernames" in part and i + 2 < len(parts):
                former = parts[i + 2].strip().split(",")[0][1:-1]
            date_next = '"Date joined"' in part
        return {"country": country, "date_joined": joined, "former_usernames": former,
                "is_verified": '"Verified"' in json.dumps(reply)}

    async def _about(self, user) -> dict | None:
        # "About this account" (country, join date, former usernames); not every account exposes it.
        try:
            about = await self._ig(lambda: self._read_about(self.ctx.ig.cl, str(user.pk)))
        except Exception as e:
            log.warning("About-this-account info unavailable for %s: %s: %s", user.username, type(e).__name__, str(e)[:200])
            return None
        return {k: (v or None) if k != "is_verified" else v for k, v in about.items()}

    @staticmethod
    def _extras(u) -> dict:
        # Optional User fields: only present for some accounts (mostly business profiles).
        phone = getattr(u, "contact_phone_number", None) or (
            f"+{u.public_phone_country_code} {u.public_phone_number}"
            if getattr(u, "public_phone_number", None) and getattr(u, "public_phone_country_code", None)
            else getattr(u, "public_phone_number", None)
        )
        place = ", ".join(x for x in (getattr(u, "address_street", None), getattr(u, "city_name", None), getattr(u, "zip", None)) if x)
        links = [str(getattr(b, "url", "") or "") for b in (getattr(u, "bio_links", None) or [])]
        return {
            "email": getattr(u, "public_email", None) or None,
            "phone": phone or None,
            "address": place or None,
            "bio_links": [x for x in links if x and x != str(u.external_url or "")],
        }

    @staticmethod
    def _card(u, detailed: bool = False, about: dict | None = None, extras: dict | None = None) -> str:
        badges = ("✅" if u.is_verified else "") + ("🔒" if u.is_private else "")
        lines = [f"<b>{_esc(u.full_name or u.username)}</b> {badges}".strip(), f"@{_esc(u.username)}"]
        lines.append(
            f"📸 {_num(u.media_count)} posts · 👥 {_num(u.follower_count)} followers · ➡️ {_num(u.following_count)} following"
        )
        if u.biography:
            lines += ["", _esc(_clip(u.biography, 300))]
        if u.external_url:
            lines.append(f"🔗 {_esc(u.external_url)}")
        category = u.category or u.category_name
        if category:
            lines.append(f"🏷 {_esc(category)}")
        if detailed:
            lines += ["", f"🆔 <code>{u.pk}</code>"]
            if extras:
                if extras["email"]:
                    lines.append(f"✉️ {_esc(extras['email'])}")
                if extras["phone"]:
                    lines.append(f"📞 {_esc(extras['phone'])}")
                if extras["address"]:
                    lines.append(f"📍 {_esc(extras['address'])}")
                for link in extras["bio_links"][:5]:
                    lines.append(f"🔗 {_esc(link)}")
            if about is None:
                lines.append("ℹ️ Account details (country, join date) are not available for this account.")
            else:
                if about["country"]:
                    lines.append(f"🌍 {_esc(str(about['country']))}")
                if about["date_joined"]:
                    lines.append(f"📅 Joined {_esc(str(about['date_joined']))}")
                if about["former_usernames"]:
                    former = about["former_usernames"]
                    former = ", ".join(map(str, former)) if isinstance(former, (list, tuple)) else str(former)
                    lines.append(f"🕘 Former usernames: {_esc(former)}")
        kind = "💼 Business" if u.is_business else "👤 Personal"
        lines.append(f"{'🔒 Private' if u.is_private else '🌐 Public'} account · {kind}" if detailed else
                     f"{'🔒 Private' if u.is_private else '🌐 Public'} account")
        if u.is_private:
            lines += ["", "🔒 Private account: only this public info is shown."]
        return "\n".join(lines)

    @staticmethod
    def _menu(username: str, private: bool) -> InlineKeyboardMarkup | None:
        if private:
            return None
        # Same behaviour as /posts, /reels, /stories and /highlights: one tap, no count menu.
        return InlineKeyboardMarkup([
            [_btn("Posts", "p0", username), _btn("Reels", "r0", username)],
            [_btn("Stories", "st", username), _btn("Highlights", "hl", username)],
            [_btn("Followers / Following", "lm", username)],
        ])

    def _list_menu(self, chat_id: int, username: str) -> InlineKeyboardMarkup:
        fmt = self._fmt.get(chat_id, "txt")
        rows = []
        for kind, label in (("f", "Followers"), ("g", "Following")):
            rows.append([_btn(f"{label} {n}", f"l{kind}{n}", username) for n in (100, 500, 1000)])
            rows.append([_btn(f"{label}: type a number", "cu", f"l{kind}:{username}")])
        rows.append([_btn(f"File format: {fmt.upper()}", "ft", username)])
        rows.append([_btn("Back", "bk", username)])
        return InlineKeyboardMarkup(rows)

    @staticmethod
    async def _edit(message, text: str, markup=None) -> None:
        try:
            await message.edit_text(text, parse_mode="HTML", reply_markup=markup)
        except TelegramError:
            pass

    async def _fetch(self, url: str) -> bytes | None:
        try:
            return await self.ctx.tg._fetch(url)
        except (media_utils.MediaError, requests.RequestException) as e:
            log.info("Could not download media (%s)", e)
            return None

    @staticmethod
    def _post_meta(m) -> dict:
        return {
            "code": m.code,
            "url": f"https://www.instagram.com/p/{m.code}/" if m.code else None,
            "type": m.product_type or {1: "photo", 2: "video", 8: "album"}.get(m.media_type, str(m.media_type)),
            "taken_at": m.taken_at.isoformat() if m.taken_at else None,
            "caption": m.caption_text,
            "likes": m.like_count if (m.like_count or 0) >= 0 else None,
            "comments": m.comment_count,
            "views": m.view_count or m.play_count,
        }

    TITLES = {"p": "Downloading posts", "r": "Downloading reels", "st": "Downloading stories", "h": "Downloading highlight",
              "pp": "Fetching profile picture", "js": "Exporting profile", "lf": "Collecting followers", "lg": "Collecting following"}

    async def _run(self, bot, chat_id: int, act: str, username: str, n: int = 0, hl_pk: str = "") -> None:
        if act == "hl":
            await self._show_highlights(bot, chat_id, username)
            return
        if chat_id in self._busy:
            await bot.send_message(chat_id, bad(BUSY))
            return
        self._busy.add(chat_id)
        status = await Progress.begin(bot, chat_id, self.TITLES.get(act, "Working"), f"@{username}", "Reading profile")
        try:
            outcome = await self._job(bot, chat_id, status, act, username, n, hl_pk)
            if isinstance(outcome, Batch):
                if len(outcome.medias) >= ZIP_THRESHOLD:
                    await self._offer_zip(chat_id, status, outcome)
                    return
                outcome = await self._send_many(bot, chat_id, status, outcome)
            await self._finish(status, username, *outcome)
        except Exception as e:
            if not isinstance(e, (PermissionError, RuntimeError)):
                log.warning("Dump %s for @%s failed: %s", act, username, e)
            await status.fail(self._explain(e))
        finally:
            self._busy.discard(chat_id)

    @staticmethod
    async def _finish(status, username: str, sent: int, total: int, how: str = "") -> None:
        if total:
            await status.done(f"Sent {sent} of {total} from @{username}{how}" + ("" if sent == total else " (some files could not be downloaded)"))
        else:
            await status.done(f"@{username}")

    async def _job(self, bot, chat_id, status, act, username, n, hl_pk):
        if act == "pp":
            user = await self._profile(username)
            url = str(user.profile_pic_url_hd or user.profile_pic_url or "")
            data = await self._fetch(url) if url else None
            if not data:
                raise RuntimeError("No profile picture could be downloaded.")
            await send_one(bot, chat_id, "photo", data, f"🖼 @{_esc(username)}")
            return 0, 0

        if act == "js":
            user = await self._profile(username)
            doc = {
                "pk": str(user.pk),
                "username": user.username,
                "full_name": user.full_name,
                "biography": user.biography,
                "external_url": str(user.external_url) if user.external_url else None,
                "is_verified": user.is_verified,
                "is_private": user.is_private,
                "is_business": user.is_business,
                "category": user.category or user.category_name,
                "media_count": user.media_count,
                "follower_count": user.follower_count,
                "following_count": user.following_count,
                "profile_pic_url_hd": str(user.profile_pic_url_hd or user.profile_pic_url or ""),
            }
            doc.update(self._extras(user))
            doc["about"] = await self._about(user)
            if not user.is_private:
                medias = await self._ig(lambda: self.ctx.ig.cl.user_medias(str(user.pk), 12))
                doc["recent_posts"] = [self._post_meta(m) for m in medias]
            raw = json.dumps(doc, indent=2, ensure_ascii=False, default=str).encode("utf-8")
            await bot.send_document(chat_id, document=InputFile(io.BytesIO(raw), filename=f"{username}_profile.json"))
            return 0, 0

        user = await self._public_profile(username)
        pk = str(user.pk)

        if act in ("lf", "lg"):
            return await self._send_list(bot, chat_id, status, user, act == "lf", n)

        if act in ("p", "r"):
            cap = POSTS_MAX if act == "p" else REELS_MAX
            everything = not n
            n = max(1, min(n, cap)) if n else cap
            await status.set("Reading the list of posts" if act == "p" else "Reading the list of reels", None)
            if act == "p":
                medias = await self._ig(lambda: self.ctx.ig.cl.user_medias(pk, n))
            else:
                medias = await self._ig(lambda: self.ctx.ig.cl.user_clips(pk, n))
            if not medias:
                raise RuntimeError("No posts found." if act == "p" else "No reels found.")
            return Batch(medias[:n], "post" if act == "p" else "reel", username, capped=everything and len(medias) >= cap)

        if act == "st":
            stories = await self._ig(lambda: self.ctx.ig.cl.user_stories(pk))
            if not stories:
                raise RuntimeError("No active stories right now.")
            return Batch(list(stories), "story", username)

        if act == "h":
            hl = await self._ig(lambda: self.ctx.ig.cl.highlight_info(hl_pk))
            if not hl.items:
                raise RuntimeError("That highlight is empty.")
            date = getattr(hl, "created_at", None) or getattr(hl.items[0], "taken_at", None)
            return Batch(list(hl.items), "highlight", username, title=hl.title or "", date=date,
                         url=f"https://www.instagram.com/stories/highlights/{hl_pk}/")

        raise RuntimeError("Unknown action.")

    async def _send_list(self, bot, chat_id, status, user, followers: bool, n: int) -> tuple[int, int]:
        n = max(1, min(n or LIST_DEFAULT, LIST_MAX))
        word = "followers" if followers else "following"
        total = user.follower_count if followers else user.following_count
        await status.set(f"Collecting up to {n}. Large lists take a few minutes.", None)
        pk = str(user.pk)
        cl = self.ctx.ig.cl
        found = await self._ig(lambda: (cl.user_followers(pk, amount=n) if followers else cl.user_following(pk, amount=n)))
        people = [{"id": str(u.pk), "username": u.username, "full_name": u.full_name or ""} for u in list(found.values())[:n]]
        if not people:
            raise RuntimeError(f"No {word} could be read (the list may be hidden).")
        fmt = self._fmt.get(chat_id, "txt")
        if fmt == "json":
            raw = json.dumps({"account": user.username, "list": word, "listed": len(people), "total": total, "users": people},
                             indent=2, ensure_ascii=False).encode("utf-8")
        else:
            head = f"# {word} of @{user.username}: {len(people)} of {total}\n# username | full name | id\n"
            raw = (head + "\n".join(f"{p['username']} | {p['full_name']} | {p['id']}" for p in people)).encode("utf-8")
        await bot.send_document(
            chat_id, document=InputFile(io.BytesIO(raw), filename=f"{user.username}_{word}.{fmt}"),
            caption=f"{len(people)} {word} of @{user.username} (of {_num(total)} total)",
        )
        return 0, 0

    def _caption(self, batch: Batch, m, size: int, text: str | None = None, date=None) -> str:
        label = label_of(m, batch.kind)
        meta = meta_of(m, batch.username, text, date)
        return build_caption(label, link_of(m, label, batch.username, batch.url), size, meta)

    async def _files(self, m) -> list[tuple[str, bytes]]:
        return await fetch_items(items_of(m), self._fetch)

    async def _send_many(self, bot, chat_id, status, batch: Batch) -> tuple[int, int]:
        if batch.kind == "highlight":
            return await self._send_highlight(bot, chat_id, status, batch)
        sent, medias = 0, batch.medias
        for i, m in enumerate(medias, 1):
            await status.set(f"Sending {i} of {len(medias)}", (i - 1) / len(medias))
            files = await self._files(m)
            if files:
                await send_files(bot, chat_id, files, self._caption(batch, m, sum(len(d) for _, d in files)))
                sent += 1
            if i != len(medias):
                await asyncio.sleep(random.uniform(0.8, 1.8))
        return sent, len(medias)

    async def _send_highlight(self, bot, chat_id, status, batch: Batch) -> tuple[int, int]:
        """Same as a pasted highlight link: the stories go out together as albums, under one caption."""
        items = batch.medias
        sent = 0
        for start in range(0, len(items), 10):
            chunk = items[start : start + 10]
            await status.set(f"Sending {start + 1}-{start + len(chunk)} of {len(items)}", start / len(items))
            files: list[tuple[str, bytes]] = []
            for story in chunk:
                files += await self._files(story)
            if files:
                text = batch.title
                if len(items) > 10:
                    text = f"{text} ({start + 1}-{start + len(chunk)} of {len(items)})".strip()
                caption = self._caption(batch, chunk[0], sum(len(d) for _, d in files), text=text, date=batch.date)
                await send_files(bot, chat_id, files, caption)
                sent += len(files)
            if start + 10 < len(items):
                await asyncio.sleep(random.uniform(0.8, 1.8))
        return min(sent, len(items)), len(items)

    # ---- "10 or more" choice: ZIP or one by one -------------------------------------------------

    async def _offer_zip(self, chat_id: int, status, batch: Batch) -> None:
        self._seq += 1
        self._pending[chat_id] = (self._seq, batch)
        count = len(batch.medias)
        files = sum(len(items_of(m)) for m in batch.medias)
        word = {"post": "posts", "reel": "reels", "story": "stories", "highlight": "highlight items"}[batch.kind]
        mb = self.ctx.tg.max_bytes // 1_000_000
        body = (f"{count} {word}, {files} files in total. That is a lot to send one by one.\n"
                f"A ZIP is a single file (split into parts if it goes over {mb} MB).")
        if batch.capped:
            body += f"\nThis account has more; only the latest {count} were read."
        title = f"{count} {word} from @{batch.username}"
        markup = InlineKeyboardMarkup([
            [_btn("Send as ZIP", "pz", str(self._seq)), _btn("One by one", "ps", str(self._seq))],
            [_btn("Cancel", "pc", str(self._seq))],
        ])
        try:
            await status.message.edit_text(screen(title, _esc(body)), parse_mode="HTML", reply_markup=markup)
        except TelegramError:
            await status.message.reply_text(screen(title, _esc(body)), parse_mode="HTML", reply_markup=markup)

    async def _on_choice(self, bot, query, act: str, token: str) -> None:
        chat_id = query.message.chat_id
        found = self._pending.get(chat_id)
        if not found or str(found[0]) != token or time.monotonic() - found[1].made > PENDING_TTL:
            self._pending.pop(chat_id, None)
            await self._edit(query.message, bad("This choice expired. Run the command again."))
            return
        batch = found[1]
        if act == "pc":
            self._pending.pop(chat_id, None)
            await self._edit(query.message, "Cancelled.")
            return
        if chat_id in self._busy:
            await bot.send_message(chat_id, bad(BUSY))
            return
        self._busy.add(chat_id)
        self._pending.pop(chat_id, None)
        try:
            await query.message.delete()
        except TelegramError:
            pass
        as_zip = act == "pz"
        status = await Progress.begin(bot, chat_id, "Building ZIP" if as_zip else self.TITLES["h" if batch.kind == "highlight" else {"post": "p", "reel": "r", "story": "st"}[batch.kind]],
                                      f"@{batch.username}", "Starting")
        try:
            outcome = await (self._send_zip(bot, chat_id, status, batch) if as_zip else self._send_many(bot, chat_id, status, batch))
            await self._finish(status, batch.username, *outcome, how=" as ZIP" if as_zip else "")
        except Exception as e:
            if not isinstance(e, (PermissionError, RuntimeError)):
                log.warning("Dump delivery for @%s failed: %s", batch.username, e)
            await status.fail(self._explain(e))
        finally:
            self._busy.discard(chat_id)

    async def _send_zip(self, bot, chat_id, status, batch: Batch) -> tuple[int, int]:
        medias, user = batch.medias, batch.username
        limit = max(1_000_000, self.ctx.tg.max_bytes - 1_000_000)
        stem = f"{user}_{batch.kind}s" if batch.kind != "highlight" else f"{user}_highlight_{slug(batch.title, 'highlight')}"
        used, info = 0, []
        with tempfile.TemporaryDirectory(prefix="hyperig-zip-") as temp:
            zipper = ZipParts(Path(temp), stem, limit)
            for i, m in enumerate(medias, 1):
                await status.set(f"Packing {i} of {len(medias)}", (i - 1) / len(medias))
                items = items_of(m)
                label = label_of(m, batch.kind)
                got = 0
                for j, (kind, url) in enumerate(items, 1):
                    data = await self._fetch(url)
                    if not data:
                        continue
                    ext = "mp4" if kind == "video" else "jpg"
                    ident = slug(str(getattr(m, "code", "") or getattr(m, "pk", "")), "media")
                    base = f"{i:02d}_story_{ident}" if batch.kind == "story" else f"{i:02d}_{ident}"
                    name = f"{base}_{j}.{ext}" if len(items) > 1 else f"{base}.{ext}"
                    await asyncio.to_thread(zipper.add, name, data)
                    got += 1
                if got:
                    used += 1
                    text = batch.title if batch.kind == "highlight" and i == 1 else None
                    info.append(info_entry(i, label, link_of(m, label, user, batch.url), meta_of(m, user, text, batch.date if batch.kind == "highlight" else None)))
            if not zipper.parts:
                raise RuntimeError("None of the files could be downloaded.")
            note = f"@{user} - {batch.kind} export\n\n" + "\n\n".join(info) + "\n"
            await asyncio.to_thread(zipper.add, "info.txt", note.encode("utf-8"), True, False)
            parts = zipper.finish()
            profile = f"https://www.instagram.com/{user}/"
            for idx, path in enumerate(parts, 1):
                await status.set(f"Uploading part {idx} of {len(parts)}", None)
                data = await asyncio.to_thread(path.read_bytes)
                caption = build_zip_caption(batch.kind, user, batch.url or profile, zipper.counts[idx - 1], len(data), idx, len(parts), len(medias))
                await tg_call(lambda: bot.send_document(
                    chat_id, document=InputFile(data, filename=path.name), caption=caption, parse_mode="HTML",
                    read_timeout=300, write_timeout=300,
                ))
        return used, len(medias)

    async def _show_highlights(self, bot, chat_id: int, username: str) -> None:
        status = await Progress.begin(bot, chat_id, "Loading highlights", f"@{username}", "Reading profile")
        try:
            user = await self._public_profile(username)
            await status.set("Reading highlights", None)
            highlights = await self._ig(lambda: self.ctx.ig.cl.user_highlights(str(user.pk)))
        except Exception as e:
            if not isinstance(e, (PermissionError, RuntimeError)):
                log.warning("Highlights for @%s failed: %s", username, e)
            await status.fail(self._explain(e))
            return
        if not highlights:
            await status.fail(f"@{username} has no highlights.")
            return
        rows, row = [], []
        for h in highlights[:HIGHLIGHT_BUTTONS]:
            pk = str(h.pk).split(":")[-1]
            row.append(_btn(f"{_clip(h.title or 'Highlight', 18)} ({h.media_count or '?'})", "h", f"{pk}:{username}"))
            if len(row) == 2:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        text = screen(f"Highlights of @{username}", "Tap one to download it.")
        markup = InlineKeyboardMarkup(rows)
        try:  # turn the progress message into the list
            await status.message.edit_text(text, parse_mode="HTML", reply_markup=markup)
        except TelegramError:
            await bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=markup)

    async def _target(self, args, update, usage: str) -> str | None:
        username = parse_target(args[0]) if args else None
        if not username:
            await update.effective_message.reply_text(bad(f"Usage: {usage}\nUse a username, @username or instagram.com link."))
        return username

    async def _card_message(self, update, username: str, detailed: bool, with_menu: bool) -> bool:
        message = update.effective_message
        status = await Progress.begin(message.get_bot(), message.chat_id, "Loading profile", f"@{username}", "Reading profile")
        try:
            user = await self._profile(username)
            if detailed:
                await status.set("Reading account details")
            about = await self._about(user) if detailed else None
            await status.set("Fetching profile picture")
            pic = str(user.profile_pic_url_hd or user.profile_pic_url or "")
            data = await self._fetch(pic) if pic else None
        except Exception as e:
            if not isinstance(e, (PermissionError, RuntimeError)):
                log.warning("Profile @%s failed: %s", username, e)
            await status.fail(self._explain(e))
            return False
        text = self._card(user, detailed, about, self._extras(user) if detailed else None)
        markup = self._menu(username, bool(user.is_private)) if with_menu else None
        try:
            if data:
                try:
                    await message.reply_photo(photo=data, caption=text, parse_mode="HTML", reply_markup=markup)
                    return True
                except BadRequest as e:
                    log.info("Card photo refused (%s), sending text", e)
            await message.reply_text(text, parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True)
            return True
        finally:
            try:
                await status.message.delete()
            except TelegramError:
                pass

    SEARCH_PAGE = 8
    SEARCH_KEEP = 50
    SEARCH_CHATS = 200

    def _search_view(self, chat_id: int, page: int):
        entry = self._search.get(chat_id)
        if not entry:
            return None
        query, users = entry
        pages = max(1, -(-len(users) // self.SEARCH_PAGE))
        page = min(max(page, 1), pages)
        chunk = users[(page - 1) * self.SEARCH_PAGE : page * self.SEARCH_PAGE]
        start = (page - 1) * self.SEARCH_PAGE
        lines, rows = [], []
        for i, (username, full_name, verified, private) in enumerate(chunk, start + 1):
            badges = ("✅" if verified else "") + ("🔒" if private else "")
            name = f" · {_esc(_clip(full_name, 40))}" if full_name else ""
            lines.append(f"{i}. @{_esc(username)}{name} {badges}".rstrip())
            rows.append([_btn(f"@{username}", "op", username)])
        nav = []
        if page > 1:
            nav.append(_btn("◀ Prev", "sp", str(page - 1)))
        if page < pages:
            nav.append(_btn("More ▶", "sp", str(page + 1)))
        if nav:
            rows.append(nav)
        head = f"{len(users)} accounts found · page {page}/{pages}"
        text = screen(f"Results for “{_esc(query)}”", head + "\n\n" + "\n".join(lines) + "\n\nTap an account to open it.")
        return text, InlineKeyboardMarkup(rows)

    async def cmd_search(self, args, update):
        message = update.effective_message
        query = " ".join(args).strip()
        if len(query) < 2 or len(query) > 60:
            await message.reply_text(bad("Usage: /search <name>\nSend at least 2 characters, for example /search nasa official."))
            return
        status = await Progress.begin(message.get_bot(), message.chat_id, "Searching accounts", f"“{query}”", "Asking Instagram")
        try:
            found = await self._ig(lambda: self.ctx.ig.cl.search_users(query))
        except Exception as e:
            if not isinstance(e, (PermissionError, RuntimeError)):
                log.warning("Search %r failed: %s", query, e)
            await status.fail(self._explain(e))
            return
        users, seen = [], set()
        for u in found or []:
            name = getattr(u, "username", None)
            if not name or name.lower() in seen:
                continue
            seen.add(name.lower())
            users.append((name, getattr(u, "full_name", "") or "", bool(getattr(u, "is_verified", False)), bool(getattr(u, "is_private", False))))
        users = users[: self.SEARCH_KEEP]
        if not users:
            await status.fail(f"No accounts found for “{query}”. Try another spelling.")
            return
        chat_id = message.chat_id
        self._search.pop(chat_id, None)
        self._search[chat_id] = (query, users)
        while len(self._search) > self.SEARCH_CHATS:
            self._search.pop(next(iter(self._search)))
        text, markup = self._search_view(chat_id, 1)
        try:  # turn the progress message into the results
            await status.message.edit_text(text, parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True)
        except TelegramError:
            await message.reply_text(text, parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True)

    async def _on_search_page(self, query, arg: str) -> None:
        view = self._search_view(query.message.chat_id, int(arg)) if arg.isdigit() else None
        if not view:
            await query.message.reply_text(bad("These results expired. Run /search again."))
            return
        text, markup = view
        try:
            await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True)
        except Exception:
            pass  # same page tapped twice: nothing to change

    async def cmd_profile(self, args, update):
        username = await self._target(args, update, "/profile <username or link>")
        if username:
            await self._card_message(update, username, detailed=True, with_menu=False)

    async def _simple(self, args, update, usage: str, act: str, default: int = 0, cap: int = 0) -> None:
        username = await self._target(args, update, usage)
        if not username:
            return
        n = default
        if cap and len(args) > 1:
            try:
                n = max(1, min(int(args[1]), cap))
            except ValueError:
                pass
        await self._run(update.get_bot(), update.effective_chat.id, act, username, n)

    async def _list_cmd(self, args, update, act: str, usage: str) -> None:
        username = await self._target(args, update, usage)
        if not username:
            return
        chat_id, n = update.effective_chat.id, LIST_DEFAULT
        for a in args[1:]:
            if a.lower() in ("json", "txt"):
                self._fmt[chat_id] = a.lower()
            else:
                try:
                    n = max(1, min(int(a), LIST_MAX))
                except ValueError:
                    pass
        await self._run(update.get_bot(), chat_id, act, username, n)

    async def cmd_followers(self, args, update):
        await self._list_cmd(args, update, "lf", "/followers <username> [count] [json]")

    async def cmd_following(self, args, update):
        await self._list_cmd(args, update, "lg", "/following <username> [count] [json]")

    async def cmd_pfp(self, args, update):
        await self._simple(args, update, "/pfp <username>", "pp")

    async def cmd_posts(self, args, update):
        await self._simple(args, update, "/posts <username> [count]", "p", 0, POSTS_MAX)

    async def cmd_reels(self, args, update):
        await self._simple(args, update, "/reels <username> [count]", "r", 0, REELS_MAX)

    async def cmd_stories(self, args, update):
        await self._simple(args, update, "/stories <username>", "st")

    async def cmd_highlights(self, args, update):
        await self._simple(args, update, "/highlights <username>", "hl")

    async def on_button(self, rest: str, query, context) -> None:
        act, _, arg = rest.partition(":")
        bot, chat_id = context.bot, query.message.chat_id

        if act in ("pz", "ps", "pc"):
            await self._on_choice(bot, query, act, arg)
            return

        if act == "sp":
            await self._on_search_page(query, arg)
            return

        if act == "h":
            hl_pk, _, username = arg.partition(":")
            if hl_pk.isdigit() and USERNAME_RE.match(username):
                await self._run(bot, chat_id, "h", username, hl_pk=hl_pk)
            return

        if act == "cu":
            kind, _, who = arg.partition(":")
            if kind in ("p", "r", "lf", "lg") and USERNAME_RE.match(who):
                self._awaiting[chat_id] = (kind, who)
                cap = {"p": POSTS_MAX, "r": REELS_MAX}.get(kind, LIST_MAX)
                await bot.send_message(chat_id, f"Send a number from 1 to {cap} for @{who}, or send cancel.")
            return

        username = arg
        if not USERNAME_RE.match(username):
            return

        if act == "op":
            shim = SimpleNamespace(effective_message=query.message)
            await self._card_message(shim, username.lower(), detailed=True, with_menu=True)
            return

        if act == "ft":
            self._fmt[chat_id] = "json" if self._fmt.get(chat_id, "txt") == "txt" else "txt"
            act = "lm"
        if act == "lm":
            try:
                await query.edit_message_reply_markup(reply_markup=self._list_menu(chat_id, username))
            except TelegramError:
                pass
            return

        if act == "bk":
            private = self._private.get(username)
            if private is None:
                try:
                    private = bool((await self._profile(username)).is_private)
                except Exception as e:
                    await query.message.reply_text(bad(self._explain(e)))
                    return
            markup = self._menu(username, private)
            try:
                await query.edit_message_reply_markup(reply_markup=markup)
            except TelegramError:
                pass
            return

        counted = re.fullmatch(r"([pr])(\d+)", act)
        if counted:
            await self._run(bot, chat_id, counted.group(1), username, int(counted.group(2)))
            return

        listed = re.fullmatch(r"l([fg])(\d+)", act)
        if listed:
            await self._run(bot, chat_id, "l" + listed.group(1), username, int(listed.group(2)))
        elif act in ("pp", "st", "hl", "js"):
            await self._run(bot, chat_id, act, username)

    def is_awaiting(self, chat_id: int) -> bool:
        return chat_id in self._awaiting

    def cancel_pending(self, chat_id: int) -> bool:
        dropped = self._pending.pop(chat_id, None) is not None
        return (self._awaiting.pop(chat_id, None) is not None) or dropped

    async def on_text(self, text: str, update) -> bool:
        message = update.effective_message
        wanted = self._awaiting.pop(message.chat_id, None)
        if not wanted:
            name = await self._username_in(text)
            if not name:
                return False
            bot = update.get_bot()
            await set_reaction(bot, message, DONE_REACTION)
            if not await self._card_message(update, name, detailed=True, with_menu=True):
                await set_reaction(bot, message)
            return True
        kind, username = wanted
        word = text.strip().lower()
        if word in ("cancel", "stop"):
            await message.reply_text("Cancelled.")
            return True
        cap = {"p": POSTS_MAX, "r": REELS_MAX}.get(kind, LIST_MAX)
        if not word.isdigit() or not 1 <= int(word) <= cap:
            self._awaiting[message.chat_id] = wanted
            await message.reply_text(f"Send a number from 1 to {cap}, or send cancel.")
            return True
        await self._run(update.get_bot(), message.chat_id, kind, username, int(word))
        return True

    async def _username_in(self, text: str) -> str | None:
        tokens = (text or "").split()
        if len(tokens) == 1:
            token = tokens[0]
            if token.lower().lstrip("@") in NOT_USERNAMES:
                return None
            return parse_target(token)
        for token in tokens:
            if token.lower().startswith(("http://", "https://")):
                name = parse_target(token)
                if name:
                    return name
        return None
