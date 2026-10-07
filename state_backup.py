"""
state_backup.py — keep the bot's books somewhere a reinstall or a new machine can't lose.

Strategy ledgers, the execution / intent / manual journals and the kill switch live only in
logs/, which git ignores. When they were left behind on another machine, positions came
over without costs (placeholder 0.50 exits, 5-6 Oct). This zips that state into a folder
outside the repo (default ~/sig-arb-state; the repo is public, so never commit it) and
restores it.

    python state_backup.py                  # back up now
    python state_backup.py --list           # show backups
    python state_backup.py --restore FILE   # unpack into logs/ (bot must be stopped)

The supervisor calls backup() once an hour and keeps the newest KEEP archives.
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import pathlib
import sys
import zipfile

ROOT = pathlib.Path(__file__).parent
LOGS = ROOT / "logs"
DEST = pathlib.Path(os.environ.get("SIG_STATE_DIR", pathlib.Path.home() / "sig-arb-state"))
KEEP = 72                                   # three days of hourly archives
# caches and reports the bot rebuilds on its own
SKIP = {"bot_books.json", "markets_cache.json", "positions.json", "bot_status.json", "supervisor_state.json",
        "portfolio_snapshot.json", "transactions_snapshot.json", "supabase_public.json"}


def state_files(logs: pathlib.Path = LOGS) -> list[pathlib.Path]:
    out = [p for p in logs.iterdir() if p.is_file() and p.name not in SKIP
           and (p.suffix in (".json", ".jsonl") or p.name == "KILL_SWITCH")]
    return sorted(out)


def backup(logs: pathlib.Path = LOGS, dest: pathlib.Path = DEST, keep: int = KEEP) -> pathlib.Path:
    dest.mkdir(parents=True, exist_ok=True)
    path = dest / f"sig-arb-state-{dt.datetime.now():%Y%m%d-%H%M%S}.zip"
    tmp = path.with_suffix(".tmp")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        for f in state_files(logs):
            z.write(f, f.name)
    tmp.replace(path)
    for old in sorted(dest.glob("sig-arb-state-*.zip"))[:-keep]:
        old.unlink(missing_ok=True)
    return path


def restore(archive: pathlib.Path, logs: pathlib.Path = LOGS) -> list[str]:
    logs.mkdir(exist_ok=True)
    with zipfile.ZipFile(archive) as z:
        names = [n for n in z.namelist() if "/" not in n and "\\" not in n]
        for n in names:
            (logs / n).write_bytes(z.read(n))
    return names


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--restore", type=pathlib.Path)
    a = ap.parse_args(argv)
    if a.list:
        for p in sorted(DEST.glob("sig-arb-state-*.zip")):
            print(f"{p.name}  {p.stat().st_size / 1024:,.0f} KB")
        return 0
    if a.restore:
        import supervisor
        if supervisor.read_pid() or supervisor.other_bots():
            print("bot is running: stop it first (python go_live.py stop)")
            return 1
        print("restored:", ", ".join(restore(a.restore)))
        return 0
    p = backup()
    print(f"backed up {len(state_files())} files to {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
