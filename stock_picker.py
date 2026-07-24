"""Stock picker engine — volume-peak sustained surge model.

Finds the highest-volume day within N trading days (the "anchor"),
then checks whether the anchor + next 2 days ALL exceed the N-day average.
"""
from __future__ import annotations

from repository import stock_repo


def pick_stocks(config: dict) -> list[dict]:
    """Volume-peak + sustained surge picker.

    Pipeline:
      1. Find the day with the highest volume in the most recent N days (anchor).
      2. Check anchor, anchor+1, anchor+2 — ALL THREE must have
         volume > N-day average volume × volume_ratio.

    Args:
        config: {
            "lookback_days": int,   N trading days
            "volume_ratio": float,  e.g. 2.0
        }

    Returns:
        list of result dicts sorted by anchor volume ratio descending.
    """
    n = config.get("lookback_days", 120)
    volume_ratio = config.get("volume_ratio", 2.0)
    min_turnover = config.get("min_turnover", 25.0)

    # ── Load all stock IDs and metadata ──
    all_stocks = stock_repo.get_all_stock_ids()
    stock_map = {s["id"]: s for s in all_stocks}
    stock_ids = [s["id"] for s in all_stocks]

    if not stock_ids:
        return []

    # ── Batch load history ──
    history_map = stock_repo.load_history_batch(stock_ids, max(int(n * 1.5), n + 15))

    # ── Per-stock: volume peak + 3-day sustained surge ──
    candidates: list[dict] = []

    for sid in stock_ids:
        rows = history_map.get(sid)
        if not rows or len(rows) < n + 1:
            continue

        # Selection: N historical trading days (excludes today).
        # Display: today's row for reference only.
        recent = rows[-(n + 1):-1]
        display = rows[-1]

        try:
            close = float(display.get("close", 0) or 0)
            if close <= 0:
                continue

            # ── Step 1: N-day volumes & average ──
            vols = [float(r.get("volume", 0) or 0) for r in recent]
            avg_vol = sum(vols) / len(vols) if vols else 0
            if avg_vol <= 0:
                continue

            # ── Step 2: find anchor = highest-volume day ──
            anchor_idx = max(range(len(vols)), key=lambda i: vols[i])
            anchor_vol = vols[anchor_idx]

            # ── Step 2b: anchor turnover ≥ threshold ──
            anchor_turn = float(recent[anchor_idx].get("turn", 0) or 0)
            if anchor_turn < min_turnover:
                continue

            # ── Step 3: need anchor+2 within N days (3-day window) ──
            if anchor_idx + 2 >= n:
                continue

            # ── Step 4: all 3 days must exceed avg × ratio ──
            passed = True
            for i in range(anchor_idx, anchor_idx + 3):
                if vols[i] <= avg_vol * volume_ratio:
                    passed = False
                    break

            if not passed:
                continue

        except (ValueError, TypeError, ZeroDivisionError):
            continue

        # ── Assemble result ──
        anchor_ratio = anchor_vol / avg_vol
        anchor_date = str(recent[anchor_idx].get("date", ""))
        # Format: "2026-07-24" → "2026-07-24" (keep as-is)
        days_since_anchor = n - 1 - anchor_idx

        volume = float(display.get("volume", 0) or 0)
        turnover = float(display.get("turn", 0) or 0)
        pct_change = float(display.get("pct_chg", 0) or 0)
        preclose = float(display.get("preclose", 0) or 0)
        high_v = float(display.get("high", close) or close)
        low_v = float(display.get("low", close) or close)
        amplitude = (high_v - low_v) / preclose * 100 if preclose > 0 else 0

        stock_info = stock_map.get(sid, {})
        code = stock_info.get("code", str(sid))
        name = stock_info.get("name", code)

        candidates.append({
            "stock_id": sid,
            "code": code,
            "name": name,
            "price": close,
            "pct_change": pct_change,
            "volume": volume,
            "turnover": turnover,
            "amplitude": amplitude,
            "vol_ratio": anchor_ratio,
            "anchor_date": anchor_date,
            "days_since_anchor": days_since_anchor,
        })

    # Sort by anchor volume ratio descending.
    candidates.sort(key=lambda x: x["vol_ratio"], reverse=True)
    return candidates
