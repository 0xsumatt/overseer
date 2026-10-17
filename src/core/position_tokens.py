from __future__ import annotations

import re
import time


from core.positions import PositionReadError

_TOKEN = re.compile(r"ro:([0-9]+):(single|all):([0-9]+):([0-9a-fA-F]{16,})\Z")



def validate_token(token: str, account_index: str) -> str:
    if not isinstance(token, str) or len(token) > 2048:
        raise PositionReadError("Supply a Lighter read-only token, not a trading key.")
    token = token.strip()
    match = _TOKEN.fullmatch(token)
    if match is None:
        raise PositionReadError("Only Lighter ro: read-only tokens are accepted; trading credentials are not.")
    index, scope, expiry, _ = match.groups()
    if scope == "single" and int(index) != int(account_index):
        raise PositionReadError("This single-account token belongs to another Lighter account index.")
    if int(expiry) <= time.time():
        raise PositionReadError("The read-only token has expired. Create a replacement on Lighter.")
    return token


