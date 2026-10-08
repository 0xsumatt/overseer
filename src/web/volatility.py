"""Rolling realised volatility from chronological, completed 1-minute closes."""

from collections import deque
import math


def rolling_volatility(rows, *, end: int, hours: int, window_hours: int) -> dict:
    start = end - hours * 3600
    window_minutes = window_hours * 60
    warmup = start - window_hours * 3600
    bucket_minutes = 1 if hours <= 24 else 5 if hours <= 48 else 15
    contributions = {}
    previous = None
    last_close = None
    for row in rows:
        timestamp = int(row["ts"].timestamp())
        close = float(row["close"])
        if timestamp % 60 or not math.isfinite(close) or close <= 0:
            previous = None
            continue
        if timestamp >= end:
            break
        if previous is not None and timestamp - previous[0] == 60:
            change = math.log(close / previous[1])
            contributions[timestamp + 60] = change * change
            last_close = timestamp + 60
        previous = (timestamp, close)

    window = deque()
    squared_returns = 0.0
    observed = 0
    points = []
    for timestamp in range(warmup + 60, end + 1, 60):
        contribution = contributions.get(timestamp)
        window.append(contribution)
        if contribution is not None:
            squared_returns += contribution
            observed += 1
        if len(window) > window_minutes:
            expired = window.popleft()
            if expired is not None:
                squared_returns -= expired
                observed -= 1
        if timestamp >= start and (
            timestamp in (start, end) or timestamp % (bucket_minutes * 60) == 0
        ):
            points.append({
                "time": timestamp,
                "rv_pct": 100 * math.sqrt(max(0.0, squared_returns))
                if observed == window_minutes else None,
                "returns": observed,
            })

    return {
        "points": points,
        "expected_returns": window_minutes,
        "bucket_minutes": bucket_minutes,
        "last_close_time": last_close,
    }
