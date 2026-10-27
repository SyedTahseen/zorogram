import asyncio
import hashlib
import random
import time
from collections import deque
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError

from core.auth import describe_error
from modules.base import BaseModule, Command
from modules.dump import parse_target
from utils import media as media_utils
from utils.logger import get_logger
from utils.prefs import DEFAULTS
from utils.tgsend import caption_of, clip, esc, items_of, send_items
from utils.ui import BLUE, CORE_COMMANDS, NOT_CONNECTED, bad, btn, kb, onoff, screen

log = get_logger("watcher")

# (seconds between quick checks, gap between accounts, seconds between slow checks)
# Quick checks = new posts/reels and stories (what you want to hear about fast): 2 Instagram calls.
# Slow checks = profile changes, highlights, edits/removals, milestones: they change rarely.
PACES = {
    "safe": (600, (10.0, 20.0), 1800),
    "normal": (300, (6.0, 14.0), 1200),
    "fast": (180, (4.0, 8.0), 900),
}
FAST_POSTS = 8  # latest posts read on a quick check (leaves room for pinned posts)
TICK = 60
MAX_NEW_PER_CHECK = 5
THROTTLE_PAUSE = 900
CHECK_NOW_GAP = 120
SEEN_CAP = 300
TRACK_POSTS = 12
STORE_KEY = "watchlist"
PREFIX = "watch"

LIKE_MS = (100, 500, 1_000, 5_000, 10_000, 50_000, 100_000, 500_000, 1_000_000)
COMMENT_MS = (10, 50, 100, 500, 1_000, 5_000)
FOLLOWER_MS = (1_000, 2_500, 5_000, 10_000, 25_000, 50_000, 100_000, 250_000, 500_000, 1_000_000, 5_000_000, 10_000_000)

FLAGS = {
    "p": ("posts", "Posts & reels", "posts"),
    "s": ("stories", "Stories", "stories"),
    "pr": ("profile", "Profile changes", "profile"),
    "hl": ("highlights", "Highlights", "highlights"),
    "ed": ("edits", "Edits & removals", "edits"),
    "en": ("engagement", "Milestones", "milestones"),
}
DEFAULT_FLAGS = set(FLAGS)
BASELINES = {"posts": "seen_posts", "stories": "seen_stories", "profile": "snap", "highlights": "hl", "edits": "tracked", "engagement": "tracked"}


def new_entry(pk: str, flags: set[str]) -> dict:
    entry = {
        "pk": str(pk), "paused": False, "interval": 0,
        "last": 0.0, "next": 0.0, "next_slow": 0.0, "added": datetime.now(timezone.utc).isoformat(),
        "seen_posts": None, "seen_stories": None, "snap": None, "hl": None, "tracked": None,
    }
    for code, (key, _name, _short) in FLAGS.items():
        entry[key] = code in flags
    return entry


def normalize(entry: dict) -> dict:
    for key, value in new_entry(entry.get("pk", ""), set()).items():
        entry.setdefault(key, value)
    return entry


def _remember(seen: list[str], new: list[str]) -> list[str]:
    return (seen + [x for x in new if x not in seen])[-SEEN_CAP:]


def _crossed(value, steps: tuple[int, ...]) -> int:
    try:
        value = int(value)
    except (TypeError, ValueError):
        return 0
    return sum(1 for s in steps if value >= s) if value >= 0 else 0


def _hash(text) -> str:
    return hashlib.md5((text or "").strip().encode("utf-8")).hexdigest()[:10]


def _pic_key(url) -> str:
    return urlparse(str(url or "")).path


def summary(entry: dict) -> str:
    names = [short for _code, (key, _name, short) in FLAGS.items() if entry.get(key)]
    return ", ".join(names) or "nothing"


def _ago(ts: float) -> str:
    if not ts:
        return "never"
    seconds = int(time.time() - ts)
    return f"{seconds // 60} min ago" if seconds < 3600 else f"{seconds // 3600} h ago"


class WatcherModule(BaseModule):
    name = "watcher"
    toggleable = True
    label = "Watchlist"
    description = "Get new posts, reels, stories and changes of public accounts"

    def __init__(self, ctx):
        super().__init__(ctx)
        self._watch: dict[str, dict] = {}
        self._lock = asyncio.Lock()
        self._check_lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._kick = asyncio.Event()
        self._force = False
        self._last_kick = float("-inf")
        self._last_check = 0.0
        self._paused_until = 0.0
        self._awaiting: dict[int, tuple[str, str]] = {}
        self._back: dict[int, str] = {}
        self._alerted: set[str] = set()
        self.errors: deque = deque(maxlen=8)

    def get_commands(self):
        return {
            "watchlist": Command(self.cmd_watchlist, CORE_COMMANDS["watchlist"][0], CORE_COMMANDS["watchlist"][1], takes_update=True),
        }

    def get_callbacks(self):
        return {PREFIX: self.on_button}

    async def on_start(self) -> None:
        saved = await self.ctx.store.get(STORE_KEY, {}) or {}
        self._watch = {u: normalize(e) for u, e in saved.items()}
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())
        log.info("Watcher started with %d account(s)", len(self._watch))

    async def on_stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    @property
    def max_watch(self) -> int:
        return int(getattr(self.ctx.cfg, "max_watch", 10) or 10)

    def _pref(self, key: str):
        prefs = getattr(self.ctx, "prefs", None)
        return prefs.get(key) if prefs else DEFAULTS[key]

    def _pace(self) -> tuple[int, tuple[float, float], int]:
        return PACES.get(self._pref("pace"), PACES["normal"])

    def _interval(self, entry: dict) -> int:
        return entry.get("interval") or self._pace()[0]

    def _slow_interval(self, entry: dict) -> int:
        return max(self._pace()[2], self._interval(entry) * 4)

    def _enabled(self) -> bool:
        manager = getattr(self.ctx, "modules", None)
        return manager.is_enabled(self) if manager else True

    async def _save(self) -> None:
        await self.ctx.store.set(STORE_KEY, self._watch)

    async def _ig(self, fn):
        if not self.ctx.ig.logged_in:
            raise RuntimeError(NOT_CONNECTED)
        return await self.ctx.ig._call(fn)

    async def _fetch(self, url: str) -> bytes | None:
        try:
            return await self.ctx.tg._fetch(url)
        except (media_utils.MediaError, requests.RequestException) as e:
            log.info("Could not download media (%s)", e)
            return None

    def _explain(self, e: Exception) -> str:
        if isinstance(e, RuntimeError):
            return f"❌ {esc(str(e))}"
        try:
            from instagrapi import exceptions as ig_exc

            if isinstance(e, (ig_exc.UserNotFound, ig_exc.ClientNotFoundError)):
                return "❌ That account was not found."
            return f"❌ {esc(describe_error(e, self.ctx.ig.cl).text)}"
        except Exception:
            return f"❌ {esc(type(e).__name__)}: {esc(str(e))}"

    def _chats(self) -> list[int]:
        chat = self.ctx.tg.chat_id
        return [chat] if chat else []

    async def _send_text(self, text: str) -> bool:
        bot = self.ctx.tg.bot
        if not bot:
            return False
        for chat in self._chats():
            try:
                await bot.send_message(chat, text, parse_mode="HTML", disable_web_page_preview=True)
                return True
            except TelegramError as e:
                log.warning("Could not send a watcher message to %s: %s", chat, e)
        return False

    async def _alert_once(self, key: str, text: str) -> None:
        if key not in self._alerted:
            self._alerted.add(key)
            await self._send_text(text)

    async def _wait(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._kick.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass
        self._kick.clear()

    async def _loop(self) -> None:
        await asyncio.sleep(random.uniform(20, 60))
        while True:
            try:
                if self._enabled() and self.ctx.ig.logged_in:
                    await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Watcher tick failed")
            await self._wait(TICK)

    async def _tick(self) -> None:
        if time.time() < self._paused_until:
            return
        force, self._force = self._force, False
        now = time.time()
        due = [u for u, e in self._watch.items() if not e["paused"] and (force or now >= e["next"])]
        if due:
            async with self._check_lock:
                if not await self._check_list(due, full=force):
                    self._paused_until = time.time() + THROTTLE_PAUSE

    async def _check_list(self, names: list[str], full: bool = False) -> bool:
        gap = self._pace()[1]
        for i, username in enumerate(names):
            entry = self._watch.get(username)
            if not entry:
                continue
            if i:
                await asyncio.sleep(random.uniform(*gap))
            slow = full or time.time() >= entry.get("next_slow", 0.0)
            try:
                await self._check_one(username, entry, slow)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if not await self._handle_error(username, e):
                    return False
            if username in self._watch:
                now = time.time()
                entry["last"] = now
                entry["next"] = now + self._interval(entry) * random.uniform(0.8, 1.3)
                if slow:
                    entry["next_slow"] = now + self._slow_interval(entry) * random.uniform(0.8, 1.3)
                async with self._lock:
                    await self._save()
        self._last_check = time.time()
        return True

    async def _handle_error(self, username: str, e: Exception) -> bool:
        try:
            from instagrapi import exceptions as ig_exc

            gone = isinstance(e, (ig_exc.PrivateAccount, ig_exc.UserNotFound, ig_exc.ClientNotFoundError))
        except Exception:
            gone = False
        if gone:
            async with self._lock:
                self._watch.pop(username, None)
                await self._save()
            await self._send_text(f"⚠️ @{esc(username)} is private or no longer exists, so I stopped watching it.")
            return True
        info = describe_error(e, self.ctx.ig.cl)
        self.errors.append((time.time(), username, info.text[:120]))
        if info.kind in ("throttle", "feedback"):
            await self._alert_once(
                f"throttle:{int(time.time() // THROTTLE_PAUSE)}",
                f"⚠️ Instagram is slowing us down. Watcher paused for {THROTTLE_PAUSE // 60} minutes.\n{esc(info.text)}",
            )
            return False
        if info.needs_you:
            log.warning("Watcher stopped this round: %s", info.text)
            if info.kind == "session":
                self.ctx.ig.logged_in = False
            await self._alert_once(f"needs:{info.kind}:{int(time.time() // 3600)}", f"⚠️ {esc(info.text)}")
            return False
        log.warning("Watcher check for @%s failed (%s: %s)", username, type(e).__name__, str(e)[:200])
        return True

    async def _check_one(self, username: str, entry: dict, slow: bool = True) -> None:
        pk = entry["pk"]
        track = slow and (entry["edits"] or entry["engagement"])
        medias = None
        if entry["posts"] or track:
            amount = TRACK_POSTS if track else FAST_POSTS
            medias = await self._ig(lambda: self.ctx.ig.cl.user_medias(pk, amount))
        if entry["posts"]:
            await self._new_posts(username, entry, medias)
        if track:
            await self._track_posts(username, entry, medias)
        if entry["stories"]:
            await self._new_stories(username, entry)
        if slow:
            if entry["profile"] or entry["engagement"]:
                await self._profile_changes(username, entry)
            if entry["highlights"]:
                await self._highlights(username, entry)

    async def _announce(self, username: str, m, label: str) -> bool:
        if label == "story":
            link = f"https://www.instagram.com/stories/{username}/{m.pk}/"
        else:
            link = f"https://www.instagram.com/p/{m.code}/" if getattr(m, "code", None) else f"https://www.instagram.com/{username}/"
        header = f"🔔 <b>@{esc(username)}</b> · new {label}\n"
        bot, chats = self.ctx.tg.bot, self._chats()
        if not bot or not chats:
            return False
        try:
            sent = await send_items(bot, chats[0], items_of(m), header + caption_of(m, label), self._fetch)
        except TelegramError as e:
            log.warning("Could not deliver a watcher alert: %s", e)
            return False
        if sent == 0:
            await self._send_text(f"{header}Could not download the media. {link}")
        await asyncio.sleep(random.uniform(0.8, 1.6))
        return True

    async def _deliver(self, username: str, entry: dict, key: str, new: list, kind: str) -> None:
        if not new:
            return
        to_send, extra = new[:MAX_NEW_PER_CHECK], new[MAX_NEW_PER_CHECK:]
        done: list[str] = []
        for m in to_send:
            label = "reel" if getattr(m, "product_type", "") == "clips" else kind
            if not await self._announce(username, m, label):
                break
            done.append(str(m.pk))
        else:
            if extra:
                done += [str(m.pk) for m in extra]
                tail = "posts" if kind == "post" else "stories"
                await self._send_text(f"🔔 <b>@{esc(username)}</b> · {len(extra)} more new {kind}(s) not shown. Use /{tail} {esc(username)}.")
        if done:
            entry[key] = _remember(entry[key], done)

    async def _new_posts(self, username: str, entry: dict, medias: list) -> None:
        if entry["seen_posts"] is None:
            entry["seen_posts"] = _remember([], [str(m.pk) for m in medias])
            return
        new = sorted((m for m in medias if str(m.pk) not in entry["seen_posts"]), key=lambda m: m.taken_at)
        await self._deliver(username, entry, "seen_posts", new, "post")

    async def _new_stories(self, username: str, entry: dict) -> None:
        stories = await self._ig(lambda: self.ctx.ig.cl.user_stories(entry["pk"]))
        if entry["seen_stories"] is None:
            entry["seen_stories"] = _remember([], [str(s.pk) for s in stories])
            return
        new = sorted((s for s in stories if str(s.pk) not in entry["seen_stories"]), key=lambda s: s.taken_at)
        await self._deliver(username, entry, "seen_stories", new, "story")

    async def _track_posts(self, username: str, entry: dict, medias: list) -> None:
        now_tracked = {
            str(m.pk): {
                "c": _hash(m.caption_text), "t": clip(m.caption_text or "", 120), "code": m.code, "ts": m.taken_at.isoformat(),
                "lm": _crossed(m.like_count, LIKE_MS), "cm": _crossed(m.comment_count, COMMENT_MS),
            }
            for m in medias
        }
        old = entry["tracked"]
        if old is None:
            entry["tracked"] = now_tracked
            return
        window_full = len(medias) >= TRACK_POSTS
        oldest = min((m.taken_at for m in medias), default=None)
        lines: list[str] = []
        by_pk = {str(m.pk): m for m in medias}
        for pk, st in list(old.items()):
            link = f"https://www.instagram.com/p/{st['code']}/"
            m = by_pk.get(pk)
            if m is None:
                gone = not window_full or (oldest is not None and datetime.fromisoformat(st["ts"]) > oldest)
                if gone and entry["edits"]:
                    lines.append(f"🗑 <b>@{esc(username)}</b> · a post is no longer there (deleted or archived)\n“{esc(st['t'])}”\n{link}")
                if gone:
                    old.pop(pk, None)
                continue
            if entry["edits"] and _hash(m.caption_text) != st["c"]:
                lines.append(
                    f"✏️ <b>@{esc(username)}</b> · caption edited\nBefore: “{esc(st['t'])}”\nNow: “{esc(clip(m.caption_text or '', 120))}”\n{link}"
                )
            if entry["engagement"]:
                likes, comments = _crossed(m.like_count, LIKE_MS), _crossed(m.comment_count, COMMENT_MS)
                if likes > st["lm"]:
                    lines.append(f"🔥 <b>@{esc(username)}</b> · a post passed {LIKE_MS[likes - 1]:,} likes\n{link}")
                if comments > st["cm"]:
                    lines.append(f"💬 <b>@{esc(username)}</b> · a post passed {COMMENT_MS[comments - 1]:,} comments\n{link}")
        merged = {**now_tracked}
        for pk, st in now_tracked.items():
            if pk in old:
                st["lm"], st["cm"] = max(st["lm"], old[pk]["lm"]), max(st["cm"], old[pk]["cm"])
        entry["tracked"] = dict(sorted(merged.items(), key=lambda kv: kv[1]["ts"], reverse=True)[:TRACK_POSTS])
        for line in lines:
            await self._send_text(line)

    async def _profile_changes(self, username: str, entry: dict) -> None:
        user = await self._ig(lambda: self.ctx.ig.cl.user_info_by_username(username, False))
        if user.is_private:
            from instagrapi import exceptions as ig_exc

            raise ig_exc.PrivateAccount()
        pic = user.profile_pic_url_hd or user.profile_pic_url
        snap = {
            "n": user.full_name or "", "b": user.biography or "", "u": str(user.external_url or ""),
            "c": user.category or user.category_name or "", "v": bool(user.is_verified), "p": _pic_key(pic),
            "f": int(user.follower_count or 0), "fm": _crossed(user.follower_count, FOLLOWER_MS),
        }
        old = entry["snap"]
        entry["snap"] = snap
        if old is None:
            return
        tag = f"<b>@{esc(username)}</b>"
        if entry["profile"]:
            changes = []
            if snap["n"] != old["n"]:
                changes.append(f"Name: “{esc(old['n'])}” → “{esc(snap['n'])}”")
            if snap["b"] != old["b"]:
                changes.append(f"Bio: “{esc(clip(old['b'], 150))}” → “{esc(clip(snap['b'], 150))}”")
            if snap["u"] != old["u"]:
                changes.append(f"Link: {esc(old['u'] or 'none')} → {esc(snap['u'] or 'none')}")
            if snap["c"] != old["c"]:
                changes.append(f"Category: {esc(old['c'] or 'none')} → {esc(snap['c'] or 'none')}")
            if snap["v"] != old["v"]:
                changes.append("Verified badge " + ("added ✅" if snap["v"] else "removed"))
            pic_changed = snap["p"] != old["p"]
            if pic_changed:
                changes.append("Profile picture changed")
            if changes:
                await self._send_text(f"👤 {tag} · profile updated\n" + "\n".join(f"• {c}" for c in changes))
                if pic_changed:
                    data = await self._fetch(str(pic)) if pic else None
                    bot, chats = self.ctx.tg.bot, self._chats()
                    if data and bot and chats:
                        try:
                            await bot.send_photo(chats[0], photo=data, caption=f"New profile picture of @{username}")
                        except TelegramError as e:
                            log.info("Could not send the new profile picture: %s", e)
        if entry["engagement"] and snap["fm"] > old.get("fm", 0):
            await self._send_text(f"🎉 {tag} · reached {FOLLOWER_MS[snap['fm'] - 1]:,} followers ({snap['f']:,} now)")

    async def _highlights(self, username: str, entry: dict) -> None:
        found = await self._ig(lambda: self.ctx.ig.cl.user_highlights(entry["pk"]))
        now_counts = {str(h.pk).split(":")[-1]: int(h.media_count or 0) for h in found}
        titles = {str(h.pk).split(":")[-1]: h.title or "Highlight" for h in found}
        old = entry["hl"]
        entry["hl"] = now_counts
        if old is None:
            return
        for pk, count in now_counts.items():
            if pk not in old:
                await self._send_text(f"⭐ <b>@{esc(username)}</b> · new highlight “{esc(titles[pk])}” ({count} items)")
            elif count > old[pk]:
                await self._send_text(f"⭐ <b>@{esc(username)}</b> · “{esc(titles[pk])}” got {count - old[pk]} new item(s)")

    async def add_account(self, username: str, flags: set[str]) -> tuple[bool, str]:
        async with self._lock:
            if username in self._watch:
                return False, bad(f"Already watching @{esc(username)}. Change it from the Watchlist.")
            if len(self._watch) >= self.max_watch:
                return False, bad(f"The limit is {self.max_watch} watched accounts (keeps the Instagram login safe). Remove one first.")
        try:
            user = await self._ig(lambda: self.ctx.ig.cl.user_info_by_username(username, False))
            if user.is_private:
                return False, bad("That account is private. I only watch public accounts.")
            entry = new_entry(str(user.pk), flags)
            await self._check_one(username, entry)
        except Exception as e:
            return False, self._explain(e)
        now = time.time()
        entry["last"], entry["next"] = now, now + self._interval(entry) * random.uniform(0.8, 1.3)
        entry["next_slow"] = now + self._slow_interval(entry) * random.uniform(0.8, 1.3)
        async with self._lock:
            if username in self._watch or len(self._watch) >= self.max_watch:
                return False, bad("Could not add it: already watched, or the list is full.")
            self._watch[username] = entry
            await self._save()
        return True, (
            f"✅ Added <b>@{esc(username)}</b>. What is already there is not sent; you get what appears from now on."
        )

    async def remove_account(self, username: str) -> bool:
        async with self._lock:
            removed = self._watch.pop(username, None)
            if removed:
                await self._save()
        return removed is not None

    async def toggle_flag(self, username: str, code: str) -> None:
        entry = self._watch.get(username)
        if not entry or code not in FLAGS:
            return
        key = FLAGS[code][0]
        entry[key] = not entry[key]
        if entry[key]:
            entry[BASELINES[key]] = None
        async with self._lock:
            await self._save()

    async def update_entry(self, username: str, **changes) -> None:
        entry = self._watch.get(username)
        if entry:
            entry.update(changes)
            async with self._lock:
                await self._save()

    def request_check_now(self) -> tuple[bool, str]:
        wait = CHECK_NOW_GAP - (time.monotonic() - self._last_kick)
        if wait > 0:
            return False, f"⏳ Checked a moment ago. Try again in {int(wait)}s."
        if time.time() < self._paused_until:
            return False, "⏸ Paused after an Instagram rate limit. It resumes on its own shortly."
        self._last_kick = time.monotonic()
        self._force = True
        self._kick.set()
        return True, "🔄 Checking now. Anything new arrives here."

    def health(self) -> dict:
        return {
            "accounts": len(self._watch), "paused": sum(1 for e in self._watch.values() if e["paused"]),
            "last_check": self._last_check, "paused_until": self._paused_until,
            "errors": list(self.errors),
        }

    async def cmd_watchlist(self, args, update):
        chat_id = update.effective_message.chat_id
        self._back[chat_id] = "nav:start"
        text, markup = self.list_view(back="nav:start")
        await update.effective_message.reply_text(text, parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True)

    def is_awaiting(self, chat_id: int) -> bool:
        return chat_id in self._awaiting

    def cancel_pending(self, chat_id: int) -> bool:
        return self._awaiting.pop(chat_id, None) is not None

    def _list(self, chat_id: int, notice: str = "") -> tuple[str, InlineKeyboardMarkup]:
        return self.list_view(notice, self._back.get(chat_id, "nav:start"))

    async def _edit(self, bot, chat_id: int, message_id: int, text: str, markup=None) -> None:
        try:
            await bot.edit_message_text(text, chat_id=chat_id, message_id=message_id, parse_mode="HTML",
                                        reply_markup=markup, disable_web_page_preview=True)
        except TelegramError:
            pass

    async def on_text(self, text: str, update) -> bool:
        message = update.effective_message
        chat_id = message.chat_id
        wanted = self._awaiting.pop(chat_id, None)
        if not wanted:
            return False
        _what, prompt_id = wanted
        bot = update.get_bot()
        prompt_id = int(prompt_id)
        cancel = kb([btn("Cancel", f"m:{PREFIX}:ls:-")])
        try:
            await message.delete()
        except TelegramError:
            pass
        if text.strip().lower() in ("cancel", "stop"):
            await self._edit(bot, chat_id, prompt_id, *self._list(chat_id))
            return True
        target = parse_target(text.split()[0] if text.split() else "")
        if not target:
            self._awaiting[chat_id] = wanted
            await self._edit(bot, chat_id, prompt_id,
                             screen("Add account", bad("That is not a username.") + "\nSend one like nasa or @nasa."), cancel)
            return True
        await self._edit(
            bot, chat_id, prompt_id,
            screen("Add account", f"⏳ Adding <b>@{esc(target)}</b>. Checking the account and recording what is already "
                                  "there. This can take up to a minute."),
        )
        ok, reply = await self.add_account(target, set(DEFAULT_FLAGS))
        if ok:
            await self._edit(bot, chat_id, prompt_id, *self._account_view(target, notice=reply))
        else:
            await self._edit(bot, chat_id, prompt_id, screen("Add account", reply),
                             kb([btn("Try again", f"m:{PREFIX}:add:-")], [btn("Back", f"m:{PREFIX}:ls:-", BLUE)]))
        return True

    @staticmethod
    def _row(items, size: int = 3) -> list[list[InlineKeyboardButton]]:
        return [items[i:i + size] for i in range(0, len(items), size)]

    def list_view(self, notice: str = "", back: str = "nav:start") -> tuple[str, InlineKeyboardMarkup]:
        lines = []
        if notice:
            lines += [notice, ""]
        if not self._watch:
            lines.append("No accounts yet. Tap Add account and send a username.")
        else:
            lines.append(f"{len(self._watch)} of {self.max_watch} accounts. Tap one to choose what to watch on it. ⏸ marks a paused account.")
            lines += ["", f"Last check: {_ago(self._last_check)}"]
        if time.time() < self._paused_until:
            lines.append("Paused after an Instagram rate limit; it resumes on its own.")
        rows = self._row([
            btn(("⏸ " if e["paused"] else "") + f"@{u}"[:24], f"m:{PREFIX}:ac:{u}") for u, e in self._watch.items()
        ])
        all_paused = bool(self._watch) and all(e["paused"] for e in self._watch.values())
        rows.append([
            btn("Add account", f"m:{PREFIX}:add:-"),
            btn("Check now", f"m:{PREFIX}:now:-"),
            btn("Resume all" if all_paused else "Pause all", f"m:{PREFIX}:pa:-"),
        ])
        rows.append([btn("Back", back, BLUE)])
        return screen("Watchlist", "\n".join(lines)), kb(*rows)

    def _account_view(self, username: str, notice: str = "") -> tuple[str, InlineKeyboardMarkup]:
        entry = self._watch.get(username)
        back = btn("Back", f"m:{PREFIX}:ls:-", BLUE)
        if not entry:
            return screen("Watchlist", bad("That account is not on the watchlist.")), kb([back])
        body = (
            f"Watching: {summary(entry)}\n"
            f"Last check: {_ago(entry['last'])}\n\n"
            "Tap to switch each one on or off. Turning one on starts from now."
        )
        if notice:
            body = f"{notice}\n\n{body}"
        title = f"@{username}" + (" (paused)" if entry["paused"] else "")
        toggles = [
            btn(f"{name}: {onoff(entry[key])}", f"m:{PREFIX}:t:{code}:{username}")
            for code, (key, name, _short) in FLAGS.items()
        ]
        rows = self._row(toggles, 2)
        rows.append([
            btn("Resume" if entry["paused"] else "Pause", f"m:{PREFIX}:pz:{username}"),
            btn("Remove", f"m:{PREFIX}:rm:{username}"),
        ])
        rows.append([back])
        return screen(title, body), kb(*rows)

    async def _show(self, query, text: str, markup) -> None:
        try:
            await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True)
        except TelegramError:
            pass

    async def on_button(self, rest: str, query, context) -> None:
        act, _, arg = rest.partition(":")
        message = query.message
        chat_id = message.chat_id
        if act == "ls":
            self._awaiting.pop(chat_id, None)
            if arg in ("h", "s"):
                self._back[chat_id] = "nav:start" if arg == "h" else "nav:settings"
            await self._show(query, *self._list(chat_id))
            return
        if act == "add":
            other = self.ctx.modules.get("dump") if self.ctx.modules else None
            if other is not None:
                other.cancel_pending(chat_id)
            self._awaiting[chat_id] = ("add", str(message.message_id))
            await self._show(
                query,
                screen("Add account", "Send the username to watch (like nasa or @nasa).\n"
                                      "It will be watched for everything; switch features off afterwards from the account's menu."),
                kb([btn("Cancel", f"m:{PREFIX}:ls:-")]),
            )
            return
        if act == "now":
            _ok, text = self.request_check_now()
            await self._show(query, *self._list(chat_id, text))
            return
        if act == "pa":
            pause = not all(e["paused"] for e in self._watch.values())
            for e in self._watch.values():
                e["paused"] = pause
            async with self._lock:
                await self._save()
            await self._show(query, *self._list(chat_id))
            return
        if act == "t":
            code, _, username = arg.partition(":")
            await self.toggle_flag(username, code)
            await self._show(query, *self._account_view(username))
            return
        username = arg
        entry = self._watch.get(username)
        if act == "ac":
            await self._show(query, *self._account_view(username))
        elif act == "pz" and entry:
            await self.update_entry(username, paused=not entry["paused"])
            await self._show(query, *self._account_view(username))
        elif act == "rm" and entry:
            await self._show(
                query,
                screen("Remove account", f"Stop watching <b>@{esc(username)}</b>?"),
                kb([btn("Yes, remove", f"m:{PREFIX}:rc:{username}"), btn("Keep", f"m:{PREFIX}:ac:{username}")]),
            )
        elif act == "rc":
            await self.remove_account(username)
            await self._show(query, *self._list(chat_id, f"Stopped watching @{esc(username)}."))
        else:
            await self._show(query, *self._list(chat_id))
