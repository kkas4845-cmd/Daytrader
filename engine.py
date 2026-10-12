"""Reglene: nivåer, oppsett, posisjonsstørrelse og tilbaketest. Ingen nettverk her."""
import math
import re
from datetime import timedelta

BAR = timedelta(minutes=5)

# ---------------------------------------------------------------- indicators
def rsi_series(closes, n=14):
    out = [None] * len(closes)
    if len(closes) <= n:
        return out
    gain = loss = 0.0
    for k in range(1, n + 1):
        d = closes[k] - closes[k - 1]
        gain += max(d, 0)
        loss += max(-d, 0)
    gain, loss = gain / n, loss / n
    out[n] = 100 if loss == 0 else 100 - 100 / (1 + gain / loss)
    for k in range(n + 1, len(closes)):
        d = closes[k] - closes[k - 1]
        gain = (gain * (n - 1) + max(d, 0)) / n
        loss = (loss * (n - 1) + max(-d, 0)) / n
        out[k] = 100 if loss == 0 else 100 - 100 / (1 + gain / loss)
    return out


def atr(bars, i, n=14):
    trs = []
    for k in range(max(1, i - n + 1), i + 1):
        b, pc = bars[k], bars[k - 1]["c"]
        trs.append(max(b["h"] - b["l"], abs(b["h"] - pc), abs(b["l"] - pc)))
    return sum(trs) / len(trs) if trs else 0.0


def prev_session(bars, i0):
    """High/low/close of the session before the one starting at i0."""
    if i0 == 0:
        return None
    d = bars[i0 - 1]["t"].date()
    day = [b for b in bars[:i0] if b["t"].date() == d]
    return {"h": max(b["h"] for b in day), "l": min(b["l"] for b in day), "c": day[-1]["c"]}


def signal_at(bars, i0, i, cfg, rsi=None):
    """Is there a buy setup on the completed bar i? (i0 = first bar of the day.)
    Returns a dict with setup, entry, stop, target and why – or None."""
    n = i - i0 + 1
    if n < 4 or i < 30:
        return None
    b, pb = bars[i], bars[i - 1]
    p = b["c"]
    today = bars[i0:i + 1]
    a = atr(bars, i)
    if a <= 0:
        return None
    zone = max(p * 0.0015, 0.25 * a)          # levels are zones, not lines
    vol = sum(x["v"] for x in today)
    vwap = sum((x["h"] + x["l"] + x["c"]) / 3 * x["v"] for x in today) / vol if vol else p
    avgv = sum(x["v"] for x in bars[i - 20:i]) / 20
    vol_strong = avgv > 0 and b["v"] >= 1.2 * avgv

    rng, body = b["h"] - b["l"], b["c"] - b["o"]
    bullish = body > 0
    momentum = bullish and rng > 0 and body >= 0.6 * rng and rng >= 0.8 * a
    engulf = bullish and pb["c"] < pb["o"] and b["c"] >= pb["o"] and b["o"] <= pb["c"]
    reaction = bullish and rng > 0 and (b["o"] - b["l"]) >= 0.5 * rng
    candle = ("oppslukende lys" if engulf else "momentum-lys" if momentum
              else "reaksjonslys" if reaction else "")

    levels = {}
    prev = prev_session(bars, i0)
    if prev:
        piv = (prev["h"] + prev["l"] + prev["c"]) / 3
        levels = {"gårsdagens topp": prev["h"], "gårsdagens bunn": prev["l"], "pivot": piv,
                  "R1": 2 * piv - prev["l"], "S1": 2 * piv - prev["h"]}
    if n > 6:
        levels["åpningstoppen"] = max(x["h"] for x in today[:6])
        levels["åpningsbunnen"] = min(x["l"] for x in today[:6])
    low3 = min(x["l"] for x in bars[i - 2:i + 1])

    def make(setup, level, why):
        stop = low3 - 0.25 * zone
        risk = p - stop
        if risk < p * 0.001 or risk > p * 0.015:      # too tight or too wide a stop
            return None
        return {"setup": setup, "level": level, "entry": p, "stop": stop,
                "target": p + cfg["rr"] * risk, "why": why, "t": b["t"]}

    # RSI divergence: price makes a lower low, RSI a higher low
    div = False
    if rsi and rsi[i] is not None:
        window = range(max(i0, i - 20), i - 2)
        if len(window) >= 3:
            k = min(window, key=lambda j: bars[j]["l"])
            now_rsi = min(r for r in rsi[i - 2:i + 1] if r is not None)
            div = rsi[k] is not None and low3 < bars[k]["l"] and now_rsi > rsi[k]

    # --- Strategy 2: the failure test (false breakdown) -------------------
    if cfg["failure"] and bullish:
        for name, S in levels.items():
            before = bars[max(i0, i - 8):i - 2]
            if len(before) < 3 or not all(x["c"] > S for x in before):
                continue                                  # it must have been support
            if not (low3 < S - 0.2 * zone and p > S):
                continue                                  # broke below, closed back above
            conf = [c for c in ("høyt volum" if vol_strong else "",
                                "RSI-divergens" if div else "", candle) if c]
            if cfg["need_confirm"] and not (vol_strong or div):
                continue
            s = make("Feiltest", name,
                     f"Kursen brøt under {name} ({S:.2f}) og lukket over igjen – falskt brudd. "
                     f"Bekreftelse: {', '.join(conf) or 'ingen'}.")
            if s:
                return s

    # --- Strategy 3: opening range breakout (the classic day trading bot setup) ---
    if cfg.get("orb") and 6 < n <= 30 and bullish and vol_strong and p > vwap:
        top = levels["åpningstoppen"]
        if pb["c"] <= top < p:
            stop = min(b["l"], top) - 0.25 * zone
            risk = p - stop
            if p * 0.001 <= risk <= p * 0.015:
                return {"setup": "Åpningsbrudd", "level": "åpningstoppen", "entry": p, "stop": stop,
                        "target": p + cfg["rr"] * risk, "t": b["t"],
                        "why": f"Kursen brøt over toppen fra de første 30 minuttene ({top:.2f}) "
                               f"på høyt volum og ligger over VWAP."}

    # --- Strategy 1: the pullback ------------------------------------------
    if cfg["pullback"] and candle and p > vwap:
        hi = max(range(i0, i + 1), key=lambda j: bars[j]["h"])
        recent_high = 2 <= i - hi <= 12                   # new high, then a pullback
        higher_lows = (min(x["l"] for x in bars[i - 5:i + 1]) > min(x["l"] for x in bars[i - 11:i - 5])
                       if n >= 12 else low3 > min(x["l"] for x in today))
        if recent_high and higher_lows:
            supports = dict(levels, VWAP=vwap)
            for name, S in sorted(supports.items(), key=lambda kv: -kv[1]):
                if S >= p or not (S - zone <= low3 <= S + zone):
                    continue                              # must touch the zone and hold
                if cfg["need_confirm"] and not vol_strong:
                    continue
                s = make("Pullback", name,
                         f"Opptrend over VWAP. Kursen falt tilbake til {name} ({S:.2f}), holdt, "
                         f"og snudde opp med {candle}"
                         f"{' på høyt volum' if vol_strong else ''}.")
                if s:
                    return s
    return None


def position_size(sig, equity, cash, fx, cfg, entry=None):
    """The 1 % rule. Returns (qty, reason-if-zero)."""
    entry = entry or sig["entry"]
    risk = entry - sig["stop"]
    if risk <= 0:
        return 0, "stop over kursen"
    qty = math.floor(equity * cfg["risk_pct"] / 100 / (risk * fx))
    cap = math.floor(min(equity * cfg["max_pos_pct"] / 100, cash - cfg["fee_nok"]) / (entry * fx))
    qty = min(qty, cap)
    if qty < 1:
        return 0, "ikke råd til én aksje innenfor grensene"
    if qty * cfg["rr"] * risk * fx < 3 * 2 * cfg["fee_nok"]:
        return 0, "kurtasjen ville spist for mye av gevinsten"
    return qty, ""


def day_start(bars, i):
    d = bars[i]["t"].date()
    while i > 0 and bars[i - 1]["t"].date() == d:
        i -= 1
    return i


def clean_tickers(raw):
    if isinstance(raw, str):
        raw = re.split(r"[,\s]+", raw)
    out = []
    for t in raw or []:
        t = str(t).strip().upper()
        if re.fullmatch(r"[A-Z0-9][A-Z0-9.\-]{0,11}", t) and t not in out:
            out.append(t)
    return out[:15]


def backtest(data, cfg, fx_fn):
    """Replay the rules bar by bar. Buys at the close of the signal bar; a bar
    that touches both stop and target counts as a stop (the cautious choice)."""
    eq = cash = float(cfg["start_cash"])
    trades, pos = [], {}
    rsi = {t: rsi_series([b["c"] for b in bars]) for t, bars in data.items()}
    fx = {t: fx_fn(t) for t in data}
    index = {t: {b["t"]: i for i, b in enumerate(bars)} for t, bars in data.items()}
    days = sorted({b["t"].date() for bars in data.values() for b in bars})[1:]
    for d in days:
        n_today, pnl_today, start_eq = 0, 0.0, eq
        times = sorted({b["t"] for bars in data.values() for b in bars if b["t"].date() == d})
        for ts in times:
            for t, bars in data.items():
                i = index[t].get(ts)
                if i is None:
                    continue
                b = bars[i]
                i0 = day_start(bars, i)
                last = i + 1 >= len(bars) or bars[i + 1]["t"].date() != d
                day_len = sum(1 for x in bars[i0:] if x["t"].date() == d)
                left = day_len - (i - i0 + 1)
                if t in pos:
                    p = pos[t]
                    px, why = None, ""
                    if b["l"] <= p["stop"]:
                        px, why = min(p["stop"], b["o"]), ("stop på inngang" if p["stop"] >= p["entry"] else "stop-loss")
                    elif b["h"] >= p["target"]:
                        px, why = max(p["target"], b["o"]), "målet er nådd"
                    elif last or left * 5 < cfg["flat_min"]:
                        px, why = b["c"], "solgt før stenging"
                    elif cfg.get("breakeven") and b["h"] >= p["entry"] + p["r0"]:
                        p["stop"] = max(p["stop"], p["entry"])
                    if px is not None:
                        del pos[t]
                        pnl = p["qty"] * (px - p["entry"]) * fx[t] - 2 * cfg["fee_nok"]
                        cash += p["qty"] * px * fx[t] - cfg["fee_nok"]
                        eq += pnl
                        pnl_today += pnl
                        trades.append({"in": p["time"], "out": str(ts), "ticker": t, "setup": p["setup"],
                                       "qty": p["qty"], "entry": p["entry"], "exit": px,
                                       "pnl": round(pnl, 2), "exit_why": why,
                                       "r": round((px - p["entry"]) / p["r0"], 2)})
                    continue
                if (n_today >= cfg["max_trades"] or pnl_today <= -cfg["daily_loss_pct"] / 100 * start_eq
                        or (i - i0) * 5 < cfg["skip_open_min"] or left * 5 < cfg["stop_new_min"]):
                    continue
                sig = signal_at(bars, i0, i, cfg, rsi[t])
                if not sig:
                    continue
                qty, _ = position_size(sig, eq, cash, fx[t], cfg)
                if not qty:
                    continue
                cash -= qty * sig["entry"] * fx[t] + cfg["fee_nok"]
                pos[t] = dict(sig, qty=qty, time=str(ts), r0=sig["entry"] - sig["stop"])
                n_today += 1
    by = {}
    for s in ("Pullback", "Feiltest", "Åpningsbrudd"):
        tr = [x for x in trades if x["setup"] == s]
        by[s] = {"n": len(tr), "pnl": sum(x["pnl"] for x in tr),
                 "win_rate": (sum(1 for x in tr if x["pnl"] > 0) / len(tr)) if tr else 0}
    wins = sum(1 for x in trades if x["pnl"] > 0)
    return {"days": len(days), "n": len(trades), "start": cfg["start_cash"], "end": round(eq, 2),
            "win_rate": wins / len(trades) if trades else 0,
            "avg_r": sum(x["r"] for x in trades) / len(trades) if trades else 0,
            "fees": 2 * cfg["fee_nok"] * len(trades), "by_setup": by,
            "from": str(days[0]) if days else "", "to": str(days[-1]) if days else "",
            "trades": trades[-40:][::-1]}
