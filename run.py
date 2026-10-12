"""Kjører boten. Brukes av GitHub Actions (se .github/workflows/daytrader.yml).

  python run.py                 én økt: sjekker hvert minutt i opptil 70 min
  python run.py --minutes 5     kortere økt (til testing)
  python run.py --backtest      bare tilbaketest

Data ligger i mappen data/ (grenen "data" på GitHub):
  config.json    innstillinger – endres fra nettsiden
  commands.json  knappetrykk fra nettsiden (selg, selg alt, test, nullstill)
  state.json     alt boten vet – skrives bare av boten
"""
import argparse
import json
import subprocess
import time
from pathlib import Path

import bot

DATA = Path("data")


def read(name, default):
    try:
        return json.loads((DATA / name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def write_state(dt):
    DATA.mkdir(exist_ok=True)
    (DATA / "state.json").write_text(json.dumps(dt.to_state(), ensure_ascii=False, indent=1,
                                                default=str), encoding="utf-8")


def git(*args, check=True):
    return subprocess.run(["git", "-C", str(DATA), *args], capture_output=True, text=True,
                          check=check)


def sync(push=False, msg="oppdatering"):
    """Pull the user's changes (config/commands) and, if asked, push our state."""
    if not (DATA / ".git").exists():
        return
    if push:
        git("add", "state.json")
        if git("diff", "--cached", "--quiet", check=False).returncode:
            git("commit", "-q", "-m", msg)
    for _ in range(4):
        r = git("pull", "-q", "--rebase", "--autostash", "origin", "data", check=False)
        if r.returncode:
            git("rebase", "--abort", check=False)
            time.sleep(3)
            continue
        if not push or git("push", "-q", "origin", "HEAD:data", check=False).returncode == 0:
            return
        time.sleep(3)
    print("Advarsel: fikk ikke synkronisert med GitHub denne gangen.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=70)
    ap.add_argument("--backtest", action="store_true")
    a = ap.parse_args()
    start = time.time()
    sync()
    cfg = bot.clean_config(read("config.json", {}))
    dt = bot.DayTrader(read("state.json", {}), cfg)
    if a.backtest:
        for cmd in read("commands.json", {}).get("commands", []):
            if cmd.get("type") == "backtest" and cmd.get("id") not in dt.done:
                dt.done.append(cmd["id"])
        dt.run_backtest()
        write_state(dt)
        sync(True, "tilbaketest")
        return
    last_push, closed_rounds = 0.0, 0
    while True:
        cfg = bot.clean_config(read("config.json", {}))
        dt.cfg = cfg
        for cmd in read("commands.json", {}).get("commands", []):
            if cmd.get("id") and cmd["id"] not in dt.done:
                dt.done.append(cmd["id"])
                dt.handle(cmd)
        try:
            dt.step()
        except Exception as e:
            dt.status = f"Feil: {e}"
            print("Feil:", e)
        write_state(dt)
        trade = dt.changed
        dt.changed = False
        if trade or time.time() - last_push > 240:
            sync(True, "handel" if trade else "status")
            last_push = time.time()
        print(time.strftime("%H:%M:%S"), dt.status, dt.data_msg, flush=True)
        closed = dt.status.startswith(("Børsen er stengt", "Av"))
        left = a.minutes * 60 - (time.time() - start)
        opens_soon = bool(dt.next_open and dt.cfg["on"] and
                          (bot.parse(dt.next_open) - dt.now()).total_seconds() < left)
        closed_rounds = closed_rounds + 1 if closed and not dt.pos and not opens_soon else 0
        if time.time() - start > a.minutes * 60 or closed_rounds >= 3:
            break
        time.sleep(60)
        sync()
    write_state(dt)
    sync(True, "slutt på økt")


if __name__ == "__main__":
    main()
