import asyncio
import concurrent.futures
import hashlib
import json
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import quote, unquote, urlparse

from instagrapi import Client
from instagrapi.config import DEFAULT_APP_VERSION
from instagrapi.exceptions import (
    AccountSuspended,
    BadCredentials,
    BadPassword,
    ChallengeRequired,
    ChallengeSelfieCaptcha,
    ChallengeUnknownStep,
    ClientConnectionError,
    ClientError,
    ClientIncompleteReadError,
    ClientLoginRequired,
    ClientNeedsUpgradeError,
    ClientRequestTimeout,
    ClientThrottledError,
    FeedbackRequired,
    LegacyForceSetNewPasswordForm,
    LoginRequired,
    PleaseWaitFewMinutes,
    ProxyAddressIsBlocked,
    RateLimitError,
    RecaptchaChallengeForm,
    ReloginAttemptExceeded,
    SelectContactPointRecoveryForm,
    SentryBlock,
    SubmitPhoneNumberForm,
    TwoFactorRequired,
)
from instagrapi.mixins.challenge import ChallengeChoice
from instagrapi.utils import json_value

from utils.logger import get_logger

log = get_logger("auth")

RELOGIN_COOLDOWN = 600


class CookieError(Exception):
    pass


class CooldownError(RuntimeError):
    pass


def parse_cookie_input(raw: str) -> str:
    text = (raw or "").strip().strip("`'\"")
    value = None
    if text[:1] in "[{":
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            raise CookieError("That looks like JSON but isn't valid JSON (did the chat app change the quotes?).") from None
        if isinstance(data, dict) and isinstance(data.get("cookies"), list):
            data = data["cookies"]
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict) and item.get("name") == "sessionid":
                    value = item.get("value")
        elif isinstance(data, dict):
            value = data.get("sessionid")
    else:
        match = re.search(r"sessionid\s*[=:]\s*([^;\s\"']+)", text)
        value = match.group(1) if match else text
    sid = str(value or "").strip().strip("\"'")
    decoded = unquote(sid)
    if len(decoded) <= 30 or not re.match(r"^\d+:\S+$", decoded):
        raise CookieError(
            "I couldn't find a valid sessionid in that. It looks like 123456789:AbCd...:27:AYx... "
            "(or 123456789%3AAbCd...). Copy the 'sessionid' cookie value from instagram.com."
        )
    return sid


def sessionid_candidates(sid: str) -> list[str]:
    other = unquote(sid) if "%" in sid else quote(sid, safe="")
    return [sid] if other == sid else [sid, other]


class CodePrompter:
    def __init__(self, timeout: int):
        self.timeout = timeout
        self.hint: str = ""
        self._loop = None
        self._notify = None
        self._pending: concurrent.futures.Future | None = None
        self._lock = threading.Lock()

    def bind(self, loop, notify) -> None:
        self._loop, self._notify = loop, notify

    @property
    def waiting(self) -> bool:
        with self._lock:
            return self._pending is not None and not self._pending.done()

    def ask(self, hint: str) -> str:
        fut: concurrent.futures.Future = concurrent.futures.Future()
        with self._lock:
            self._pending = fut
            self.hint = hint
        minutes = max(1, self.timeout // 60)
        text = f"{hint}\nSend the verification code here (waiting {minutes} min)."
        log.info(text)
        if self._loop is not None and self._notify is not None:
            asyncio.run_coroutine_threadsafe(self._notify(text), self._loop)
        try:
            return fut.result(timeout=self.timeout)
        except concurrent.futures.TimeoutError:
            log.warning("No code received within %ss", self.timeout)
            return ""
        finally:
            with self._lock:
                if self._pending is fut:
                    self._pending = None
                    self.hint = ""

    def _resolve(self, value: str) -> bool:
        with self._lock:
            fut = self._pending
        if fut is None or fut.done():
            return False
        try:
            fut.set_result(value)
        except concurrent.futures.InvalidStateError:
            return False
        return True

    def submit(self, code: str) -> bool:
        code = "".join((code or "").split())
        return bool(code) and self._resolve(code)

    def cancel(self) -> None:
        self._resolve("")


@dataclass
class ErrorInfo:
    kind: str
    text: str
    needs_you: bool


def describe_error(e: Exception, cl=None) -> ErrorInfo:
    detail = str(e)[:300]
    if isinstance(e, CooldownError):
        return ErrorInfo("throttle", str(e), False)
    if isinstance(e, ReloginAttemptExceeded):
        return ErrorInfo("password", "Too many login attempts. Open the Instagram app, make sure the account is fine, then reconnect with /connect.", True)
    if isinstance(e, ClientNeedsUpgradeError) or _needs_app_migration(e):
        return ErrorInfo(
            "password",
            "Instagram blocked the password login from this server. "
            "Connect with a browser cookie instead: /connect, then Cookie.",
            True,
        )
    if isinstance(e, (BadPassword, BadCredentials)):
        return ErrorInfo(
            "password",
            "Instagram rejected the login. If the password is right, this is usually a risk check: "
            "open the Instagram app, approve any 'was this you?' prompt, then try /connect again, "
            "or connect with a browser cookie. "
            f"I will not retry automatically. ({detail})",
            True,
        )
    if isinstance(e, TwoFactorRequired):
        return ErrorInfo("2fa", "Instagram needs a 2FA code and none arrived in time. Try /connect again and send the code when asked.", True)
    if isinstance(e, ChallengeSelfieCaptcha):
        return ErrorInfo("challenge", "Instagram wants a selfie/captcha. Complete it in the official Instagram app, then reconnect with /connect.", True)
    if isinstance(e, AccountSuspended):
        return ErrorInfo("account", "Instagram says this account is suspended. Check it in the Instagram app.", True)
    if _has_name(e, {"CheckpointRequired", "ConsentRequired", "ChallengeRedirection", "TermsUnblock", "TermsAccept", "AboutUsError", "GeoBlockRequired"}):
        return ErrorInfo("challenge", f"Instagram wants a security check or consent. Finish it in the Instagram app, then reconnect with /connect. ({type(e).__name__})", True)
    if isinstance(e, (RecaptchaChallengeForm, SelectContactPointRecoveryForm, ChallengeUnknownStep, SubmitPhoneNumberForm, LegacyForceSetNewPasswordForm)):
        return ErrorInfo("challenge", f"Instagram started a security check that can't be automated. Finish it in the Instagram app, then reconnect with /connect. ({type(e).__name__})", True)
    if isinstance(e, ChallengeRequired):
        return ErrorInfo("challenge", f"Instagram wants a security check. Approve it in the Instagram app, then reconnect with /connect. ({detail})", True)
    if isinstance(e, FeedbackRequired):
        message = ""
        if cl is not None:
            try:
                message = str((cl.last_json or {}).get("feedback_message", ""))[:200]
            except Exception:
                pass
        return ErrorInfo("feedback", f"Instagram restricted this action ({message or 'feedback_required'}). Cooling down.", False)
    if isinstance(e, (PleaseWaitFewMinutes, ClientThrottledError, RateLimitError)):
        return ErrorInfo("throttle", f"Instagram is rate-limiting us ({type(e).__name__}). Backing off.", False)
    if isinstance(e, (SentryBlock, ProxyAddressIsBlocked)):
        return ErrorInfo("throttle", f"Instagram blocked this server's IP or proxy ({type(e).__name__}). Try a different proxy (IG_PROXY).", False)
    if isinstance(e, (LoginRequired, ClientLoginRequired)):
        return ErrorInfo("session", "The Instagram session expired. Reconnect with /connect.", True)
    if _is_not_found(e):
        return ErrorInfo(
            "notfound",
            "Instagram couldn't find that. The link or username may be wrong, or the post was deleted or is private.",
            False,
        )
    if _has_name(e, {"ConnectProxyError", "AuthRequiredProxyError"}):
        return ErrorInfo("network", f"The proxy could not be used ({type(e).__name__}). Check IG_PROXY.", False)
    if isinstance(e, ClientIncompleteReadError):
        return ErrorInfo("network", "The download was interrupted. Try again.", False)
    if isinstance(e, (ClientConnectionError, ClientRequestTimeout)):
        return ErrorInfo("network", f"Network problem talking to Instagram ({type(e).__name__}).", False)
    return ErrorInfo("other", f"{type(e).__name__}: {detail}", False)


_NOT_FOUND_NAMES = {
    "ClientGraphqlError", "ClientNotFoundError", "MediaNotFound", "MediaUnavailable", "InvalidMediaId",
    "UserNotFound", "StoryNotFound", "HighlightNotFound", "NotFoundError",
}


def _has_name(e: Exception, names: set) -> bool:
    return any(cls.__name__ in names for cls in type(e).__mro__)


def _is_not_found(e: Exception) -> bool:
    return _has_name(e, _NOT_FOUND_NAMES)


def _needs_app_migration(e: Exception) -> bool:
    s = str(e).lower()
    error_type = str(getattr(e, "error_type", "") or "").strip().lower()
    return (
        error_type == "needs_upgrade"
        or type(e).__name__ == "ClientNeedsUpgradeError"
        or "your version of instagram is out of date" in s
        or "bloks_versioning_id" in s
        or "bloks hash" in s
    )


def _mask_proxy(proxy: str | None) -> str:
    if not proxy:
        return "none"
    p = urlparse(proxy if "://" in proxy else f"http://{proxy}")
    return f"{p.scheme}://{p.hostname}:{p.port}" if p.hostname else "set"


class AuthManager:
    SESSION = "session"
    META = "session_meta"

    def __init__(self, cfg, store, client_factory=Client):
        self.cfg = cfg
        self.store = store
        self.client_factory = client_factory
        self.prompter = CodePrompter(cfg.code_timeout)
        self.login_method = "none"
        self.upgrade_note = ""
        self._loop = None
        self._notify = None
        self._in_login = False
        self._last_relogin = float("-inf")
        self._saved_hash: str | None = None
        self._custom_profile = False
        self._app_overridden = False
        self.cl = self._build_client(None)

    def bind(self, loop, notify) -> None:
        self._loop, self._notify = loop, notify
        self.prompter.bind(loop, notify)

    def notify_threadsafe(self, text: str) -> None:
        if self._loop and self._notify:
            asyncio.run_coroutine_threadsafe(self._notify(text), self._loop)

    def _build_client(self, session: dict | None):
        cfg = self.cfg
        cl = self.client_factory(
            settings=session or None,
            proxy=cfg.ig_proxy,
            delay_range=cfg.ig_delay_range,
            tls_verify=cfg.ig_tls_verify,
            with_challenge_flow=False,
        )
        self._custom_profile = False
        self._app_overridden = False
        if cfg.ig_override_app_version:
            self._use_default_app_profile(cl)
        if cfg.ig_read_timeout:
            cl.read_timeout = cfg.ig_read_timeout
        if cfg.ig_app_version and not (session or {}).get("authorization_data"):
            self._apply_app_profile(cl)
        if not session:
            if cfg.ig_locale:
                cl.set_locale(cfg.ig_locale)
            if cfg.ig_country:
                cl.set_country(cfg.ig_country)
            if cfg.ig_country_code is not None:
                cl.set_country_code(cfg.ig_country_code)
            if cfg.ig_timezone_offset is not None:
                cl.set_timezone_offset(cfg.ig_timezone_offset)
        if cfg.ig_phone_number:
            cl.phone_number = cfg.ig_phone_number
        cl.challenge_code_handler = self._challenge_code_handler
        cl.change_password_handler = self._change_password_handler
        cl.handle_exception = self._handle_exception
        self.cl = cl
        return cl

    def _use_default_app_profile(self, cl) -> None:
        cl.set_app(DEFAULT_APP_VERSION)
        cl.set_user_agent()
        self._app_overridden = True

    def _apply_app_profile(self, cl) -> None:
        cfg = self.cfg
        try:
            cl.set_app(cfg.ig_app_version)
            return
        except ValueError:
            pass
        profile = {"app_version": cfg.ig_app_version}
        if cfg.ig_app_version_code:
            profile["version_code"] = cfg.ig_app_version_code
        if cfg.ig_bloks_versioning_id:
            profile["bloks_versioning_id"] = cfg.ig_bloks_versioning_id
        cl.set_app(profile)
        cl.set_user_agent()
        self._custom_profile = True
        if not cfg.ig_app_version_code:
            log.warning("IG_APP_VERSION=%s has no IG_APP_VERSION_CODE; the version code of the default profile is used", cfg.ig_app_version)

    def _challenge_code_handler(self, username, choice):
        via = {ChallengeChoice.SMS: "SMS", ChallengeChoice.EMAIL: "email"}.get(choice, str(choice))
        return self.prompter.ask(f"Instagram sent a verification code to @{username} by {via}.")

    def _change_password_handler(self, username):
        msg = (
            "Instagram requires a password change for this account. Change it in the Instagram app, "
            "then reconnect with /connect."
        )
        self.notify_threadsafe("🔒 " + msg)
        raise ChallengeRequired(msg)

    def _handle_exception(self, client, e):
        if isinstance(e, BadPassword):
            log.warning("bad_password from Instagram: %s", e)
            if client.relogin_attempt > 0:
                raise ReloginAttemptExceeded(e)
            raise e
        if isinstance(e, (LoginRequired, ClientLoginRequired)):
            if self._in_login or not self.cfg.ig_password:
                raise e
            if time.monotonic() - self._last_relogin < RELOGIN_COOLDOWN:
                log.warning("Session rejected again within %ss of a re-login, giving up", RELOGIN_COOLDOWN)
                raise e
            log.warning("login_required: session rejected, re-logging in once with the same device")
            self._relogin_core(client)
            return True
        if isinstance(e, ChallengeRequired):
            api_path = json_value(client.last_json, "challenge", "api_path")
            challenge = (client.last_json or {}).get("challenge") or {}
            manual = (
                api_path == "/challenge/"
                or "auth_platform" in str(api_path or "")
                or challenge.get("native_flow") is True
                or bool(challenge.get("bloks_action"))
            )
            if manual:
                log.warning("Challenge needs manual handling in the Instagram app (%s)", api_path)
                raise e
            client.challenge_resolve(client.last_json)
            return True
        if isinstance(e, FeedbackRequired):
            log.warning("feedback_required: %s", (client.last_json or {}).get("feedback_message", ""))
        elif isinstance(e, ClientThrottledError):
            log.warning("HTTP 429 from Instagram, back off and review proxy/account pressure")
        elif isinstance(e, PleaseWaitFewMinutes):
            log.warning("Instagram says: please wait a few minutes (%s)", e)
        raise e

    def _totp(self, cl) -> str:
        return cl.totp_generate_code(self.cfg.ig_totp_secret) if self.cfg.ig_totp_secret else ""

    def _attempt_login(self, cl, relogin: bool) -> None:
        cfg = self.cfg
        legacy = cfg.ig_legacy_login or (self._custom_profile and not cfg.ig_bloks_versioning_id)
        login = cl.login_legacy if legacy else cl.login
        username = (cfg.ig_username or "").strip()
        code = self._totp(cl)
        try:
            login(username, cfg.ig_password, relogin=relogin, verification_code=code)
        except TwoFactorRequired:
            code = self.prompter.ask("Instagram needs your 2FA code (authenticator app, SMS or 8-digit backup code).")
            if not code:
                raise
            login(username, cfg.ig_password, relogin=relogin, verification_code=code)

    def _password_login(self, cl, relogin: bool = False) -> None:
        if not self.cfg.ig_password:
            raise BadCredentials("No IG_PASSWORD configured, so a password login is not possible")
        try:
            self._attempt_login(cl, relogin)
        except ClientError as e:
            current_ver = (getattr(cl, "device_settings", None) or {}).get("app_version")
            already_current = bool(
                self._app_overridden
                or (current_ver == DEFAULT_APP_VERSION and getattr(cl, "bloks_versioning_id", None))
            )
            if not _needs_app_migration(e) or already_current:
                raise
            log.warning("Instagram rejected the old app profile (%s); retrying once with %s", current_ver, DEFAULT_APP_VERSION)
            self._use_default_app_profile(cl)
            self._attempt_login(cl, relogin)

    def _relogin_core(self, cl) -> None:
        self._in_login = True
        self._last_relogin = time.monotonic()
        try:
            cl.relogin_attempt = 0
            self._password_login(cl, relogin=True)
            self.login_method = "password"
        finally:
            self._in_login = False

    def login_sync(self, session: dict | None) -> str:
        self._in_login = True
        try:
            cl = self._build_client(session)
            has_auth = bool((session or {}).get("authorization_data"))
            if not has_auth and self.cfg.ig_sessionid:
                try:
                    cl.login_by_sessionid(self.cfg.ig_sessionid)
                    self._check_cookie_account(cl)
                    self.login_method = "sessionid"
                    return self.login_method
                except Exception as e:
                    log.warning("sessionid login failed (%s), falling back to password", type(e).__name__)
                    cl = self._build_client(session)
            self._password_login(cl)
            self.login_method = "session" if has_auth else "password"
            return self.login_method
        finally:
            self._in_login = False


    def _check_cookie_account(self, cl) -> None:
        owner = (getattr(cl, "username", None) or "").lower()
        if owner and self.cfg.ig_username and owner != self.cfg.ig_username.lower():
            raise CookieError(f"That cookie belongs to @{owner}, but IG_USERNAME is @{self.cfg.ig_username}. Not logging in.")

    def cookie_login_sync(self, sessionid: str, upgrade: bool, session: dict | None) -> str:
        previous = self.cl
        self._in_login = True
        try:
            base = {k: v for k, v in (session or {}).items() if k not in ("authorization_data", "cookies")} or None
            cl, error = None, None
            for candidate in sessionid_candidates(sessionid):
                cl = self._build_client(base)
                try:
                    cl.login_by_sessionid(candidate)
                    error = None
                    break
                except Exception as e:
                    error = e
                    log.info("sessionid form rejected (%s), trying the other form", type(e).__name__)
            if error is not None:
                raise error
            self._check_cookie_account(cl)
            self.login_method = "cookie"
            self.upgrade_note = ""
            if upgrade and self.cfg.ig_password:
                cookie_settings = cl.get_settings()
                try:
                    cl.relogin_attempt = 0
                    self._password_login(cl, relogin=True)
                    self.login_method = "password"
                except Exception as e:
                    log.warning("Password login on top of the cookie failed (%s), keeping the cookie session", type(e).__name__)
                    self.upgrade_note = describe_error(e, cl).text
                    self._build_client(cookie_settings)
                    self.login_method = "cookie"
            return self.login_method
        except Exception:
            self.cl = previous
            raise
        finally:
            self._in_login = False

    async def load_session(self) -> dict | None:
        data = await self.store.get(self.SESSION)
        if not isinstance(data, dict) or "uuids" not in data:
            return None
        meta = await self.store.get(self.META) or {}
        saved_user = (meta.get("username") or "").lower()
        if saved_user and self.cfg.ig_username and saved_user != self.cfg.ig_username.lower():
            log.warning("Saved session belongs to @%s but IG_USERNAME is @%s. Ignoring it.", saved_user, self.cfg.ig_username)
            return None
        return data

    async def save_session(self, settings: dict, force: bool = False) -> None:
        authed = bool(settings.get("authorization_data"))
        if not authed and await self.store.get(self.SESSION):
            return
        digest = hashlib.sha256(json.dumps(settings, sort_keys=True, default=str).encode()).hexdigest()
        if not force and digest == self._saved_hash:
            return
        await self.store.set(self.SESSION, settings)
        await self.store.set(
            self.META,
            {"username": self.cfg.ig_username or getattr(self.cl, "username", ""), "saved_at": datetime.now(timezone.utc).isoformat(), "authenticated": authed},
        )
        self._saved_hash = digest

    async def forget_session(self) -> None:
        await self.store.set(self.SESSION, None)
        await self.store.set(self.META, None)
        self._saved_hash = None

    def info(self) -> str:
        cl = self.cl
        try:
            device = getattr(cl, "device_settings", {}) or {}
            last_login = getattr(cl, "last_login", None)
            age = f"{int((time.time() - last_login) // 3600)}h ago" if last_login else "unknown"
            acct = self.cfg.ig_username or getattr(cl, "username", None) or "not configured"
            return "\n".join(
                [
                    f"Account: @{acct} (id {getattr(cl, 'user_id', None) or 'not logged in'})",
                    f"Logged in via: {self.login_method}",
                    f"Last login: {age}",
                    f"Device: {device.get('manufacturer', '?')} {device.get('model', '?')} (Android {device.get('android_release', '?')})",
                    f"App version: {device.get('app_version', '?')}",
                    f"Locale/country/tz: {getattr(cl, 'locale', '?')} / {getattr(cl, 'country', '?')} / {getattr(cl, 'timezone_offset', '?')}",
                    f"Proxy: {_mask_proxy(self.cfg.ig_proxy)}",
                    f"2FA auto-code: {'on' if self.cfg.ig_totp_secret else 'off'}",
                    f"Login flow: {'legacy' if self.cfg.ig_legacy_login else 'CAA (default)'}",
                    f"Private requests this run: {getattr(cl, 'private_requests_count', '?')}",
                ]
            )
        except Exception as e:
            return f"Could not read session info: {type(e).__name__}"
