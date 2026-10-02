"""
supervisor.py — keep the bot and dashboard running without a terminal tab.

Use it through go_live.py:

    python go_live.py start      # readiness check, then run everything in the background
    python go_live.py status     # processes, heartbeat, live/dry, token, exposure, disk
    python go_live.py stop       # stop bot, dashboard and supervisor cleanly

The supervisor (a detached process, logs/supervisor.log):
  * restarts bot.py / dashboard.py if they exit (backoff 5 s doubling to 5 min);
  * engages the kill switch if free disk drops below MIN_FREE_GB (what stopped the bot
    on 1 Oct) and sends a macOS notification;
  * notifies when the kill switch engages, the bot's heartbeat goes stale, or the SIG
    token is about to expire without having been renewed;
  * rotates logs/bot.log and logs/dashboard.log past 50 MB.
It never places orders and never releases the kill switch.
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import signal
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).parent
LOGS = ROOT / "logs"
PID_FILE = LOGS / "supervisor.pid"
STATE_FILE = LOGS / "supervisor_state.json"
KILL_SWITCH = LOGS / "KILL_SWITCH"
BOT_STATUS = LOGS / "bot_status.json"
MIN_FREE_GB = 2.0
LOG_MAX_BYTES = 50 * 1024 * 1024
DEFAULT_BOT_ARGS = ["--mode", "auto", "--live", "--interval", "3", "--strategy", "arb,fv,ll,mm"]
DASHBOARD_ARGS = ["--port", "8876"]


def notify(message: str, title: str = "SIG bot") -> None:
    """macOS notification; silently skipped elsewhere."""
    safe = message.replace('"', "'")[:200]
    try:
        subprocess.run(["osascript", "-e", f'display notification "{safe}" with title "{title}"'],
                       timeout=5, capture_output=True)
    except (OSError, subprocess.SubprocessError):
        pass
    log(f"NOTIFY {message}")


def log(message: str) -> None:
    print(time.strftime("%Y-%m-%d %H:%M:%S"), message, flush=True)


def free_gb(path: pathlib.Path = ROOT) -> float:
    return shutil.disk_usage(path).free / 1024 ** 3


def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def read_pid() -> int | None:
    try:
        pid = int(PID_FILE.read_text().strip())
    except (OSError, ValueError):
        return None
    return pid if pid_alive(pid) else None


def other_bots() -> list[int]:
    """bot.py processes not started by this supervisor (e.g. in a terminal tab)."""
    out = subprocess.run(["pgrep", "-f", "python.*bot.py"], capture_output=True, text=True).stdout
    return [int(p) for p in out.split() if p.strip().isdigit() and int(p) != os.getpid()]


def rotate(path: pathlib.Path) -> None:
    if path.exists() and path.stat().st_size > LOG_MAX_BYTES:
        path.replace(path.with_suffix(path.suffix + ".1"))


class Child:
    def __init__(self, name: str, args: list[str]):
        self.name, self.args = name, args
        self.proc: subprocess.Popen | None = None
        self.backoff, self.next_start, self.started_at = 5.0, 0.0, 0.0

    def ensure(self) -> None:
        if self.proc and self.proc.poll() is None:
            return
        if self.proc is not None:
            code = self.proc.returncode
            ran = time.time() - self.started_at
            self.backoff = 5.0 if ran > 600 else min(300.0, self.backoff * 2)
            self.next_start = time.time() + self.backoff
            notify(f"{self.name} exited (code {code}) after {ran:.0f}s; restarting in {self.backoff:.0f}s")
            self.proc = None
        if time.time() < self.next_start:
            return
        logf = LOGS / f"{self.name}.log"
        rotate(logf)
        out = logf.open("a")
        self.proc = subprocess.Popen([sys.executable, str(ROOT / f"{self.name}.py"), *self.args],
                                     cwd=ROOT, stdout=out, stderr=subprocess.STDOUT,
                                     env={**os.environ, "PYTHONUNBUFFERED": "1"})
        self.started_at = time.time()
        log(f"started {self.name} pid {self.proc.pid}: {' '.join(self.args)}")

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.send_signal(signal.SIGINT)
            try:
                self.proc.wait(75)           # in-flight book reads + order cancels
            except subprocess.TimeoutExpired:
                self.proc.terminate()


def run(bot_args: list[str]) -> None:
    LOGS.mkdir(exist_ok=True)
    PID_FILE.write_text(str(os.getpid()))
    children = [Child("bot", bot_args), Child("dashboard", DASHBOARD_ARGS)]
    stopping = False

    def on_signal(signum, _frame):
        nonlocal stopping
        stopping = True
    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    seen_kill = KILL_SWITCH.exists()
    warned = {"stale": False, "token": False, "disk": False}
    log(f"supervisor started pid {os.getpid()}")
    try:
        while not stopping:
            gb = free_gb()
            if gb < MIN_FREE_GB and not KILL_SWITCH.exists():
                KILL_SWITCH.write_text(f"low disk: {gb:.1f} GB free\nactor=supervisor\n")
                if not warned["disk"]:
                    notify(f"Low disk ({gb:.1f} GB free): kill switch engaged")
                    warned["disk"] = True
            for c in children:
                c.ensure()
            engaged = KILL_SWITCH.exists()
            if engaged and not seen_kill:
                reason = KILL_SWITCH.read_text().splitlines()[0][:150] if KILL_SWITCH.exists() else ""
                notify(f"Kill switch engaged: {reason}")
            seen_kill = engaged
            try:
                st = json.loads(BOT_STATUS.read_text())
                age = time.time() - BOT_STATUS.stat().st_mtime
                stale = age > 300 and time.time() - children[0].started_at > 300
                if stale and not warned["stale"]:
                    notify(f"Bot heartbeat is {age / 60:.0f} min old")
                warned["stale"] = stale
                left = st.get("token_seconds_left")
                low = left is not None and left < 300
                if low and not warned["token"]:
                    notify("SIG token expiring and not renewed: run python go_live.py set-cookie")
                warned["token"] = low
            except (OSError, ValueError):
                pass
            STATE_FILE.write_text(json.dumps({
                "ts": time.time(), "pid": os.getpid(), "free_gb": round(gb, 1),
                "children": {c.name: {"pid": c.proc.pid if c.proc and c.proc.poll() is None else None,
                                      "started_at": c.started_at, "args": c.args} for c in children}}))
            time.sleep(5)
    finally:
        log("supervisor stopping")
        for c in children:
            c.stop()
        PID_FILE.unlink(missing_ok=True)


def start(bot_args: list[str]) -> int:
    if read_pid():
        print(f"already running (supervisor pid {read_pid()}); use: python go_live.py status")
        return 1
    strays = other_bots()
    if strays:
        print(f"bot.py is already running outside the supervisor (pid {', '.join(map(str, strays))}).\n"
              "Stop it first (Ctrl+C in its tab) so two bots never trade at once.")
        return 1
    LOGS.mkdir(exist_ok=True)
    out = (LOGS / "supervisor.log").open("a")
    proc = subprocess.Popen([sys.executable, str(ROOT / "supervisor.py"), "run", *bot_args], cwd=ROOT,
                            stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                            start_new_session=True, env={**os.environ, "PYTHONUNBUFFERED": "1"})
    for _ in range(20):
        if read_pid():
            break
        time.sleep(0.25)
    print(f"started supervisor pid {proc.pid}: bot {' '.join(bot_args)} + dashboard on http://127.0.0.1:8876\n"
          "logs: logs/bot.log, logs/dashboard.log, logs/supervisor.log")
    return 0


def stop() -> int:
    pid = read_pid()
    if not pid:
        print("supervisor is not running")
        return 0
    os.kill(pid, signal.SIGTERM)
    for _ in range(480):
        if not pid_alive(pid):
            print("stopped bot, dashboard and supervisor")
            return 0
        time.sleep(0.25)
    print(f"supervisor pid {pid} did not stop within 120s")
    return 1


def status() -> int:
    pid = read_pid()
    print(f"supervisor   {'running pid ' + str(pid) if pid else 'NOT running'}")
    try:
        state = json.loads(STATE_FILE.read_text()) if pid else {}
        for name, c in state.get("children", {}).items():
            print(f"{name:<12} {'running pid ' + str(c['pid']) if c['pid'] else 'restarting / down'}")
    except (OSError, ValueError):
        pass
    strays = [p for p in other_bots() if not pid]
    if strays:
        print(f"bot.py       running outside the supervisor (pid {', '.join(map(str, strays))})")
    print(f"kill switch  {'ENGAGED: ' + KILL_SWITCH.read_text().splitlines()[0] if KILL_SWITCH.exists() else 'off'}")
    try:
        st = json.loads(BOT_STATUS.read_text())
        age = time.time() - BOT_STATUS.stat().st_mtime
        left = st.get("token_seconds_left")
        print(f"heartbeat    {age:.0f}s ago | {'LIVE' if st.get('live') else 'DRY RUN'}"
              f"{' (' + '; '.join(st.get('live_blockers') or []) + ')' if not st.get('live') else ''}")
        print(f"token        {'unknown' if left is None else f'{left / 60:.0f} min left'}")
        print(f"exposure     {st.get('gross')} / {st.get('max_gross')}  | strategies {st.get('strategies')}")
        print(f"counts       {st.get('counts')}")
        if st.get("fv"):
            print(f"fair value   {st['fv']}")
        hc = st.get("holdings_check") or {}
        if hc.get("ok") is not None:
            print(f"holdings     {'match bot records' if hc['ok'] else 'DIFFERENCES: ' + json.dumps(hc.get('pending_differences'))}"
                  f" (checked {hc.get('checked_at')})")
        if st.get("error"):
            print(f"last error   {st['error']}")
    except (OSError, ValueError):
        print("heartbeat    none yet")
    print(f"disk         {free_gb():.1f} GB free")
    return 0


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "run":
        run(sys.argv[2:] or DEFAULT_BOT_ARGS)
    else:
        print(__doc__)
