import html

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

NOT_CONNECTED = "Instagram is not connected. Use /connect."
BUSY = "Another job is still running. Please wait."

CORE_COMMANDS = {
    "start": ("Open the main menu", "/start"),
    "help": ("List commands, or details for one", "/help [command]"),
    "status": ("Account and watcher status", "/status"),
    "watchlist": ("Manage watched accounts", "/watchlist"),
    "settings": ("Preferences and features", "/settings"),
    "connect": ("Connect your Instagram account", "/connect"),
    "logout": ("Disconnect and delete your data", "/logout"),
    "cancel": ("Cancel what the bot is waiting for", "/cancel"),
}


def esc(text) -> str:
    return html.escape(str(text if text is not None else ""), quote=False)


def kb(*rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([list(r) for r in rows if r])


GREEN, RED, BLUE = "success", "danger", "primary"
STYLE_BY_LABEL = {
    "Connect": GREEN,
    "Send as ZIP": GREEN,
    "Resume": GREEN,
    "Resume all": GREEN,
    "Keep": GREEN,
    "One by one": BLUE,
    "Pause": RED,
    "Pause all": RED,
    "Cookie": BLUE,
    "Cancel": RED,
    "Disconnect": RED,
    "Yes, disconnect": RED,
    "Yes, remove": RED,
    "Remove": RED,
}


def button(text: str, style: str | None = None, **kwargs) -> InlineKeyboardButton:
    if not style:
        return InlineKeyboardButton(text, **kwargs)
    try:
        return InlineKeyboardButton(text, style=style, **kwargs)
    except TypeError:
        return InlineKeyboardButton(text, api_kwargs={"style": style}, **kwargs)


def btn(label: str, data: str, style: str | None = None) -> InlineKeyboardButton:
    return button(label, style or STYLE_BY_LABEL.get(label), callback_data=data)


def screen(title: str, body: str = "") -> str:
    head = f"<b>{esc(title)}</b>"
    return f"{head}\n\n{body}" if body else head


def onoff(value) -> str:
    return "On" if value else "Off"


def bad(text) -> str:
    return f"❌ {text}"


def fail(text) -> str:
    return bad(esc(text))
