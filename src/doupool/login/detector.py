from __future__ import annotations

import logging
from dataclasses import dataclass


_LOG = logging.getLogger("doupool.login")


@dataclass(frozen=True, slots=True)
class DoubaoIdentity:
    user_id: str
    nickname: str | None = None

    def as_mapping(self) -> dict[str, str | None]:
        return {"user_id": self.user_id, "nickname": self.nickname}