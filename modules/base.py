from dataclasses import dataclass
from typing import Awaitable, Callable


@dataclass
class Command:
    handler: Callable[[list[str]], Awaitable[str]]
    description: str
    usage: str = ""
    sensitive: bool = False
    takes_update: bool = False


class BaseModule:
    name = "base"
    description = ""
    toggleable = False
    label = ""

    def __init__(self, ctx):
        self.ctx = ctx

    def get_commands(self) -> dict[str, Command]:
        return {}

    def get_callbacks(self) -> dict:
        return {}

    async def on_start(self) -> None:
        pass

    async def on_stop(self) -> None:
        pass

    def cancel_pending(self, chat_id: int) -> bool:
        return False

    def is_awaiting(self, chat_id: int) -> bool:
        return False

    async def on_text(self, text: str, update) -> bool:
        return False
