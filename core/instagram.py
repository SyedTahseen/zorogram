import asyncio
import time
from dataclasses import replace

from core.auth import AuthManager, CookieError, CooldownError, describe_error, parse_cookie_input
from utils.logger import get_logger

log = get_logger("instagram")

SAVE_EVERY = 300
COOLDOWN = 300


class InstagramClient:
    def __init__(self, cfg, store):
        self.cfg = cfg
        self.store = store
        self.auth = AuthManager(cfg, store)
        self.logged_in = False
        self.last_alert = ""
        self.on_alert = None
        self._lock = asyncio.Lock()
        self._last_save = time.monotonic()
        self._cooldown_until = 0.0

    @property
    def cl(self):
        return self.auth.cl

    @property
    def username(self) -> str:
        return self.cfg.ig_username or getattr(self.cl, "username", "") or ""

    async def _call(self, fn, *args, raw: bool = False):
        if not raw:
            left = self._cooldown_until - time.monotonic()
            if left > 0:
                raise CooldownError(
                    f"Instagram asked us to slow down. Resting this account for about {int(left // 60) + 1} min, try again then."
                )
        async with self._lock:
            try:
                result = await asyncio.to_thread(fn, *args)
            except Exception as e:
                if not raw and describe_error(e, self.cl).kind in ("throttle", "feedback"):
                    self._cooldown_until = time.monotonic() + COOLDOWN
                    log.warning("Cooling down for %ss after %s", COOLDOWN, type(e).__name__)
                raise
        await self._maybe_save()
        return result

    async def _maybe_save(self) -> None:
        if not self.logged_in or time.monotonic() - self._last_save < SAVE_EVERY:
            return
        self._last_save = time.monotonic()
        try:
            await self._save_session()
        except Exception:
            log.exception("Could not save the refreshed session")

    async def _say(self, text: str) -> None:
        if self.on_alert:
            await self.on_alert(text)

    def _set_cfg(self, **changes) -> None:
        self.cfg = replace(self.cfg, **changes)
        self.auth.cfg = self.cfg

    async def _save_session(self, force: bool = False) -> None:
        settings = await self._call(self.cl.get_settings, raw=True)
        await self.auth.save_session(settings, force=force)

    def _bind(self) -> None:
        self.auth.bind(asyncio.get_running_loop(), self._say)

    async def login_with_password(self, username: str, password: str, totp_secret: str | None = None) -> tuple[bool, str]:
        self._bind()
        await self.auth.forget_session()
        self._set_cfg(ig_username=username, ig_password=password, ig_totp_secret=totp_secret, ig_sessionid=None)
        try:
            await self._call(self.auth.login_sync, None, raw=True)
        except Exception as e:
            self.logged_in = False
            info = describe_error(e, self.cl)
            self.last_alert = info.text
            log.info("Password login failed (%s)", type(e).__name__)
            return False, info.text
        finally:
            self._set_cfg(ig_password=None)
        self.logged_in = True
        self.last_alert = ""
        self._cooldown_until = 0.0
        await self._save_session(force=True)
        return True, f"Connected as @{self.username} ({self.auth.login_method})."

    async def login_with_cookie(self, raw: str) -> tuple[bool, str]:
        self._bind()
        try:
            sessionid = parse_cookie_input(raw)
        except CookieError as e:
            return False, str(e)
        self._set_cfg(ig_username="", ig_password=None)
        try:
            await self._call(self.auth.cookie_login_sync, sessionid, False, None, raw=True)
        except CookieError as e:
            return False, str(e)
        except Exception as e:
            log.info("Cookie login failed (%s)", type(e).__name__)
            info = describe_error(e, self.cl)
            extra = "" if info.kind == "other" else f" {info.text}"
            return False, (
                f"Instagram rejected that cookie ({type(e).__name__}). It may be expired, or from a browser you "
                f"logged out of.{extra}"
            )
        self._set_cfg(ig_username=getattr(self.cl, "username", "") or "")
        self.logged_in = True
        self.last_alert = ""
        await self._save_session(force=True)
        return True, f"Connected as @{self.username} (cookie)."

    async def restore_saved_session(self) -> bool:
        self._bind()
        session = await self.auth.load_session()
        if not (session or {}).get("authorization_data") and not (session or {}).get("cookies", {}).get("sessionid"):
            self.logged_in = False
            return False
        meta = await self.store.get(self.auth.META) or {}
        if meta.get("username") and not self.cfg.ig_username:
            self._set_cfg(ig_username=meta["username"])

        def _validate():
            cl = self.auth._build_client(session)
            cl.account_info()
            self.auth.login_method = "session"

        try:
            await self._call(_validate, raw=True)
        except Exception as e:
            self.logged_in = False
            log.info("Saved session is no longer valid (%s)", type(e).__name__)
            return False
        self.logged_in = True
        self.last_alert = ""
        return True

    async def logout(self) -> None:
        self.logged_in = False
        self.auth.prompter.cancel()
        await self.auth.forget_session()

    async def stop(self) -> None:
        self.auth.prompter.cancel()
        if self.logged_in:
            try:
                await self._save_session(force=True)
            except Exception:
                log.exception("Could not save the session on shutdown")
