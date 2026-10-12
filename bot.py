"""Daytrader-boten: kurser, kjøp, salg og testkonto. Ingen tråder, ingen server –
run.py kaller step() hvert minutt. Alt lagres som JSON i mappen data/."""
import base64
import json
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import engine
from engine import BAR

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
      "Accept": "application/json"}

DEFAULTS = {
    "on": True,
    "tickers": ["AAPL", "NVDA", "TSLA", "AMD", "MSFT", "META", "AMZN"],
    "start_cash": 10000, "risk_pct": 1.0, "rr": 2.0, "max_trades": 3,
    "daily_loss_pct": 3.0, "max_pos_pct": 100, "fee_nok": 5,
    "pullback": True, "failure": True, "orb": True, "need_confirm": True,
    "breakeven": False,
    "skip_open_min": 15, "stop_new_min": 45, "flat_min": 10,
    "ntfy": "",
}
LIMITS = {"start_cash": (1000, 10_000_000), "risk_pct": (0.1, 5), "rr": (1, 5),
          "max_trades": (1, 20), "daily_loss_pct": (0.5, 20), "max_pos_pct": (5, 100),
          "fee_nok": (0, 500), "skip_open_min": (0, 120), "stop_new_min": (10, 240),
          "flat_min": (5, 60)}


def utcnow():
    return datetime.now(timezone.utc)


def iso(t):
    return t.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse(s):
    return datetime.fromisoformat(s)


def clean_config(raw):
    """Read config.json safely: unknown keys ignored, bad values -> default."""
    cfg = dict(DEFAULTS)
    for k, v in (raw or {}).items():
        if k not in DEFAULTS:
            continue
        try:
            if k == "tickers":
                v = engine.clean_tickers(v) or DEFAULTS["tickers"]
            elif k == "ntfy":
                v = "".join(ch for ch in str(v).strip() if ch.isalnum() or ch in "-_")[:64]
            elif isinstance(DEFAULTS[k], bool):
                v = v if isinstance(v, bool) else str(v).lower() in ("1", "true", "ja", "on")
            else:
                lo, hi = LIMITS[k]
                v = min(hi, max(lo, float(v)))
                if isinstance(DEFAULTS[k], int):
                    v = int(v)
            cfg[k] = v
        except (TypeError, ValueError):
            pass
    if os.environ.get("NTFY_TOPIC"):
        cfg["ntfy"] = os.environ["NTFY_TOPIC"].strip()
    return cfg


# ---------------------------------------------------------------- Yahoo
def _get(url):
    last = None
    for attempt in range(3):
        for host in ("query1", "query2"):
            try:
                req = urllib.request.Request(url.replace("HOST", host), headers=UA)
                with urllib.request.urlopen(req, timeout=20) as r:
                    return json.loads(r.read().decode("utf-8"))
            except Exception as e:
                last = e
        time.sleep(2 + 3 * attempt)
    raise last


def fetch_one(sym, rng="5d"):
    url = (f"https://HOST.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(sym)}"
           f"?interval=5m&range={rng}&includePrePost=false")
    res = _get(url)["chart"]["result"][0]
    meta = res["meta"]
    tz = timezone(timedelta(seconds=meta.get("gmtoffset", 0)))
    q = res["indicators"]["quote"][0]
    bars = []
    for k, ts in enumerate(res.get("timestamp") or []):
        o, h, l, c = q["open"][k], q["high"][k], q["low"][k], q["close"][k]
        if None in (o, h, l, c):
            continue
        bars.append({"t": datetime.fromtimestamp(ts, tz), "o": o, "h": h, "l": l, "c": c,
                     "v": q["volume"][k] or 0})
    reg = (meta.get("currentTradingPeriod") or {}).get("regular") or {}
    return {"bars": bars, "cur": meta.get("currency") or "USD",
            "price": meta.get("regularMarketPrice") or (bars[-1]["c"] if bars else None),
            "open": datetime.fromtimestamp(reg["start"], tz) if reg.get("start") else None,
            "close": datetime.fromtimestamp(reg["end"], tz) if reg.get("end") else None}


def fetch(tickers, rng="5d"):
    out = {}
    for t in tickers:
        try:
            out[t] = fetch_one(t, rng)
        except Exception as e:
            out[t] = {"bars": [], "err": str(e)[:160]}
    return out


_fx = {"NOK": (0, 1.0)}


def fx_to_nok(cur):
    cur = (cur or "USD").upper()
    hit = _fx.get(cur)
    if hit and (cur == "NOK" or time.time() - hit[0] < 900):
        return hit[1]
    try:
        rate = float(fetch_one(f"{cur}NOK=X", "1d")["price"])
        _fx[cur] = (time.time(), rate)
        return rate
    except Exception:
        return hit[1] if hit else {"USD": 10.0, "EUR": 11.5, "SEK": 1.0, "DKK": 1.55}.get(cur, 10.0)


def push(topic, title, msg):
    if not topic:
        return
    try:
        t = "=?UTF-8?B?" + base64.b64encode(title.encode()).decode() + "?="
        req = urllib.request.Request(f"https://ntfy.sh/{urllib.parse.quote(topic)}",
                                     data=msg.encode("utf-8"), headers={"Title": t})
        urllib.request.urlopen(req, timeout=10).close()
    except Exception:
        pass


# ---------------------------------------------------------------- the bot
class DayTrader:
    def __init__(self, state, cfg, fetch=fetch, now=utcnow, fx=fx_to_nok, notify=push):
        self.fetch, self.now, self.fx, self.notify = fetch, now, fx, notify
        self.cfg = cfg
        s = state or {}
        self.cash = s.get("cash", float(cfg["start_cash"]))
        self.pos = s.get("pos", {})
        self.trades = s.get("trades", [])
        self.log = s.get("log", [])
        self.day = s.get("day", {"date": "", "pnl": 0.0, "n": 0})
        self.done = s.get("done", [])            # handled command ids
        self.seen = s.get("seen", {})            # last judged bar per ticker
        self.prices = s.get("prices", {})
        self.cur = s.get("cur", {})
        self.backtest_result = s.get("backtest")
        self.status = s.get("status", "")
        self.data_msg = s.get("data_msg", "")
        self.last_ok = s.get("last_ok")
        self.next_open = None
        self.changed = False

    def to_state(self):
        return {"cash": self.cash, "pos": self.pos, "trades": self.trades[-500:],
                "log": self.log[-200:], "day": self.day, "done": self.done[-200:],
                "seen": self.seen, "prices": self.prices, "cur": self.cur,
                "backtest": self.backtest_result, "status": self.status,
                "data_msg": self.data_msg, "last_ok": self.last_ok,
                "equity": self.equity(), "updated": iso(self.now()),
                "cfg": dict(self.cfg, ntfy="") if os.environ.get("NTFY_TOPIC") else self.cfg,
                "next_open": self.next_open,
                "stats": self.stats(), "halted": self.halted()}

    def say(self, msg):
        self.log.append({"t": iso(self.now()), "msg": msg})
        self.log = self.log[-200:]
        self.changed = True

    def equity(self):
        return self.cash + sum(p["qty"] * self.prices.get(t, p["entry"]) * p["fx"]
                               for t, p in self.pos.items())

    def stats(self):
        tr = self.trades
        if not tr:
            return {"n": 0}
        return {"n": len(tr), "win_rate": sum(1 for x in tr if x["pnl"] > 0) / len(tr),
                "avg_r": sum(x["r"] for x in tr) / len(tr), "pnl": sum(x["pnl"] for x in tr)}

    def halted(self):
        if self.day["n"] >= self.cfg["max_trades"]:
            return f"maks {self.cfg['max_trades']} handler i dag er brukt"
        if self.day["pnl"] <= -self.cfg["daily_loss_pct"] / 100 * (self.equity() - self.day["pnl"]):
            return f"dagens tapsgrense ({self.cfg['daily_loss_pct']:g} %) er nådd"
        return ""

    # -- orders --------------------------------------------------------------------------
    def _buy(self, t, sig, price, fx, bar_start):
        qty, why_not = engine.position_size(sig, self.equity(), self.cash, fx, self.cfg, price)
        if not qty:
            self.say(f"{t}: {sig['setup']}-signal, men ingen handel – {why_not}.")
            return
        self.cash -= qty * price * fx + self.cfg["fee_nok"]
        risk = price - sig["stop"]
        self.pos[t] = {"qty": qty, "entry": price, "stop": sig["stop"], "stop0": sig["stop"],
                       "target": price + self.cfg["rr"] * risk, "setup": sig["setup"],
                       "why": sig["why"], "fx": fx, "time": iso(self.now()),
                       "watch_from": iso(bar_start)}
        self.day["n"] += 1
        msg = (f"KJØP {qty} {t} til {price:.2f} ({sig['setup']}). Stop {sig['stop']:.2f}, "
               f"mål {self.pos[t]['target']:.2f}. {sig['why']}")
        self.say(msg)
        self.notify(self.cfg["ntfy"], f"Daytrader: kjøp {t}", msg)

    def _sell(self, t, price, reason):
        p = self.pos.pop(t)
        self.cash += p["qty"] * price * p["fx"] - self.cfg["fee_nok"]
        pnl = p["qty"] * (price - p["entry"]) * p["fx"] - 2 * self.cfg["fee_nok"]
        r0 = p["entry"] - p.get("stop0", p["stop"])
        r = (price - p["entry"]) / r0 if r0 > 0 else 0.0
        self.day["pnl"] += pnl
        self.trades.append({"in": p["time"], "out": iso(self.now()), "ticker": t,
                            "setup": p["setup"], "qty": p["qty"], "entry": p["entry"],
                            "exit": round(price, 4), "pnl": round(pnl, 2), "r": round(r, 2),
                            "why": p["why"], "exit_why": reason})
        msg = f"SELG {p['qty']} {t} til {price:.2f} – {reason}. Resultat {pnl:+.0f} kr ({r:+.1f} R)."
        self.say(msg)
        self.notify(self.cfg["ntfy"], f"Daytrader: solgt {t}", msg)

    def manage(self, t, d, now):
        """Exit rules for one open position. Walks through every 5-minute bar since the
        buy, so a stop or target touched between two checks is still caught."""
        p = self.pos[t]
        bars = d.get("bars") or []
        price = d.get("price") or (bars[-1]["c"] if bars else None)
        if price is None:
            return
        op, cl = d.get("open"), d.get("close")
        if op and parse(p["time"]) < op:            # bought on an earlier day
            if op <= now < cl:
                self._sell(t, price, "lå igjen fra en tidligere dag – solgt ved første mulighet")
            return
        frm = parse(p["watch_from"])
        steps = [(b["o"], b["h"], b["l"]) for b in bars if b["t"] >= frm] + [(price, price, price)]
        r0 = p["entry"] - p["stop0"]
        for o, h, l in steps:
            if l <= p["stop"]:
                fill = min(p["stop"], o)
                return self._sell(t, fill, "stop på inngang (sikret mot tap)"
                                  if p["stop"] >= p["entry"] else "stop-loss")
            if h >= p["target"]:
                return self._sell(t, max(p["target"], o), "målet er nådd")
            if self.cfg["breakeven"] and p["stop"] < p["entry"] and h >= p["entry"] + r0:
                p["stop"] = p["entry"]
                self.say(f"{t}: kursen har steget 1 R – stop flyttet opp til kjøpskursen {p['entry']:.2f}.")
        if cl and now >= cl - timedelta(minutes=self.cfg["flat_min"]):
            self._sell(t, price, "selger før børsen stenger")

    # -- commands from the web page ---------------------------------------------------------
    def handle(self, cmd, data=None):
        typ, t = cmd.get("type"), str(cmd.get("ticker") or "").upper()
        if typ == "sell" and t in self.pos:
            d = (data or self.fetch([t])).get(t, {})
            price = d.get("price") or self.prices.get(t)
            if price:
                self._sell(t, price, "solgt manuelt fra nettsiden")
        elif typ == "sell_all":
            if self.pos:
                data = data or self.fetch(list(self.pos))
                for t in list(self.pos):
                    price = data.get(t, {}).get("price") or self.prices.get(t)
                    if price:
                        self._sell(t, price, "solgt manuelt (Selg alt)")
            self.say("Selg alt er utført.")
        elif typ == "reset":
            self.cash, self.pos, self.trades = float(self.cfg["start_cash"]), {}, []
            self.day = {"date": "", "pnl": 0.0, "n": 0}
            self.say(f"Testkontoen er startet på nytt med {self.cfg['start_cash']:.0f} kr.")
        elif typ == "backtest":
            self.run_backtest()
        self.changed = True

    def run_backtest(self):
        try:
            raw = self.fetch(self.cfg["tickers"], "1mo")
            data = {t: d["bars"] for t, d in raw.items() if len(d.get("bars") or []) > 100}
            if not data:
                err = next((d["err"] for d in raw.values() if d.get("err")), "ingen data")
                raise RuntimeError(f"fikk ikke kurser fra Yahoo ({err})")
            fxs = {t: self.fx(raw[t].get("cur")) for t in data}
            r = engine.backtest(data, dict(self.cfg), lambda t: fxs[t])
            r["ran"] = iso(self.now())
            self.backtest_result = r
        except Exception as e:
            self.backtest_result = {"error": f"Testen feilet: {e}", "ran": iso(self.now())}

    # -- one round, every minute -------------------------------------------------------------
    def step(self):
        now, cfg = self.now(), self.cfg
        wanted = list(dict.fromkeys(list(self.pos) + (cfg["tickers"] if cfg["on"] else [])))
        if not wanted:
            self.status = "Av"
            return {}
        data = self.fetch(wanted)
        good = [t for t, d in data.items() if d.get("bars")]
        errs = [f"{t}: {d['err']}" for t, d in data.items() if d.get("err")]
        if not good:
            self.data_msg = "Får ikke kurser fra Yahoo. " + (errs[0] if errs else "")
            self.status = "Venter på kurser"
            return data
        self.last_ok = iso(now)
        missing = [t for t in wanted if t not in good]
        self.data_msg = ("Fant ikke kurser for: " + ", ".join(missing)) if missing else ""
        today = now.astimezone(timezone(timedelta(hours=-4))).date().isoformat()
        if self.day["date"] != today:
            self.day = {"date": today, "pnl": 0.0, "n": 0}

        for t in good:                              # prices + exits first
            d = data[t]
            self.prices[t] = d.get("price") or d["bars"][-1]["c"]
            self.cur[t] = d.get("cur", "USD")
            if t in self.pos:
                self.manage(t, d, now)

        upcoming = [d["open"] for d in data.values() if d.get("open") and d["open"] > now]
        self.next_open = iso(min(upcoming)) if upcoming else None
        live = 0
        stop_reason = self.halted()
        for t in good:
            d = data[t]
            op, cl = d.get("open"), d.get("close")
            is_open = bool(op and cl and op <= now < cl)
            live += is_open
            if (not cfg["on"] or not is_open or t in self.pos or t not in cfg["tickers"]
                    or stop_reason or len(d["bars"]) < 40):
                continue
            if not (op + timedelta(minutes=cfg["skip_open_min"]) <= now
                    < cl - timedelta(minutes=cfg["stop_new_min"])):
                continue
            bars = d["bars"]
            i = len(bars) - 1
            if bars[i]["t"] + BAR > now:            # still forming – judge the one before
                i -= 1
            key = iso(bars[i]["t"])
            if now - bars[i]["t"] > 3 * BAR or self.seen.get(t) == key:
                continue
            self.seen[t] = key
            i0 = engine.day_start(bars, i)
            sig = engine.signal_at(bars, i0, i, cfg, engine.rsi_series([b["c"] for b in bars[:i + 1]]))
            if not sig:
                continue
            price = self.prices[t]
            if price > sig["entry"] + 0.25 * (sig["entry"] - sig["stop"]) or price <= sig["stop"]:
                self.say(f"{t}: {sig['setup']}-signal, men kursen løp fra – ingen handel.")
                continue
            self._buy(t, sig, price, self.fx(d.get("cur")), bars[i]["t"] + BAR)
            stop_reason = self.halted()

        if not cfg["on"]:
            self.status = "Av – passer bare på åpne posisjoner." if self.pos else "Av"
        elif not live:
            self.status = "Børsen er stengt nå. Boten venter til den åpner."
        else:
            self.status = (f"Følger {live} aksjer. " +
                           (f"Ingen nye kjøp: {stop_reason}." if stop_reason else "Leter etter oppsett."))
        return data
