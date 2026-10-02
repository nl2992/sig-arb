"""
go_live.py — readiness checklist for live SIG trading. It never places an order.

    python go_live.py check                    # every prerequisite, with the next action
    python go_live.py check --offline          # skip the read-only SIG requests
    python go_live.py compare capture.json     # compare your hand-placed test order with the bot's body
    python go_live.py set-cookie               # write the clipboard cookie (copy(document.cookie)) to .env
    python go_live.py close 312 --limit 0.195  # exit a position (e.g. after LEGGED); asks y/N first
    python go_live.py start                    # check, then run bot + dashboard in the background
    python go_live.py status                   # processes, heartbeat, live/dry, token, exposure, disk
    python go_live.py stop                     # stop everything cleanly

`compare` takes a JSON file you save from DevTools after placing ONE tiny order by hand:

    {"payload": <Network -> /api/trading/orders/place -> Payload>,
     "response": <the same request -> Response>}

Secrets are never printed: only whether a value is present and when the token expires.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import sys

import bot
import gates
import sig_client
from sig_client import Client

ROOT = pathlib.Path(__file__).parent
CAPTURE_REPORT = ROOT / "logs" / "order_capture_check.json"
# Raw /orders/place response keys the code reads: Client.place() sums quantityTraded,
# bot._fill_of() reads the average price and the order id (used for cancels).
FILL_QTY_KEY = "quantityTraded"
FILL_PX_KEYS = ("avgPrice", "averagePrice", "fillPrice")


def _row(name, status, detail, action=None):
    return {"check": name, "status": status, "detail": detail, "action": action}


def check(offline: bool = False, limits_path: pathlib.Path = bot.RISK_LIMITS,
          kill_switch: pathlib.Path = bot.KILL_SWITCH, capture_report: pathlib.Path = CAPTURE_REPORT,
          cli: Client | None = None) -> list:
    rows = []
    cli = cli or Client()

    if not cli.cookie:
        rows.append(_row("sig_cookie", "FAIL", "SIG_COOKIE is empty or .env is missing",
                         "cp .env.example .env, then paste the full cookie header from a signed-in "
                         "sig.thesuper.market request (DevTools -> Network -> any /api/ request)"))
    else:
        rows.append(_row("sig_cookie", "PASS", "SIG_COOKIE present"))

    if cli.access_token and cli.profile_id:
        left = cli.token_seconds_left()
        if left is None:
            rows.append(_row("sig_session", "WARN", "token present but its expiry is unreadable"))
        elif left < bot.TOKEN_MARGIN_S:
            rows.append(_row("sig_session", "FAIL", f"access token expires in {max(0, left) / 60:.1f} min",
                             "copy a fresh cookie into .env (the token lasts about an hour)"))
        else:
            rows.append(_row("sig_session", "PASS", f"access token and profile id present; "
                             f"token expires in {left / 60:.0f} min"))
    else:
        rows.append(_row("sig_session", "FAIL", "no Supabase sb-*-auth-token session in SIG_COOKIE",
                         "copy the WHOLE cookie header; it must include the sb-...-auth-token cookie(s)"))

    if offline:
        rows.append(_row("sig_read_access", "SKIP", "--offline"))
    elif not cli.cookie:
        rows.append(_row("sig_read_access", "SKIP", "no cookie"))
    else:
        try:
            markets = cli.markets()
            raw = cli.book_raw(markets[0]["id"]) if markets else {}
            shape = "books" if "books" in raw else "levels" if "levels" in raw else "unknown"
            balance = cli.balance()
            rows.append(_row("sig_read_access", "PASS" if markets and shape != "unknown" else "FAIL",
                             f"{len(markets)} markets; book format '{shape}'; balance "
                             f"{'readable' if balance is not None else 'unreadable'}",
                             None if shape != "unknown" else "book response shape changed; inspect book_raw()"))
        except PermissionError as exc:
            rows.append(_row("sig_read_access", "FAIL", str(exc), "copy a fresh cookie into .env"))
        except Exception as exc:
            rows.append(_row("sig_read_access", "FAIL", f"{type(exc).__name__}: {str(exc)[:200]}",
                             "if Python is blocked, use browser_relay.js and run the bot later"))

    try:
        limits = gates.load_limits(limits_path)
        rows.append(_row("risk_limits", "PASS",
                         f"gross cap {limits['venue_exposure'].get('sig')}, per trade {limits['per_trade_capital']}, "
                         f"per event {limits['event_exposure']}, min edge {limits['min_net_edge']}, "
                         f"manual_approval {str(limits['manual_approval']).lower()}"))
        rows.append(_row("auto_mode_enabled", "PASS" if not limits["manual_approval"] else "INFO",
                         "auto mode may send live orders" if not limits["manual_approval"]
                         else "auto mode dry-runs; confirm mode (y/N per trade) can go live",
                         None if not limits["manual_approval"] else
                         'set "manual_approval": false in config/risk_limits.json once confirm-mode fills look right'))
    except (OSError, ValueError) as exc:
        rows.append(_row("risk_limits", "FAIL", str(exc), "fix config/risk_limits.json"))

    engaged = bot.kill_switch_engaged(kill_switch)
    rows.append(_row("kill_switch", "FAIL" if engaged else "PASS", "engaged" if engaged else "off",
                     "review the reason in logs/KILL_SWITCH, reconcile, then release from the dashboard"
                     if engaged else None))

    try:
        report = json.loads(pathlib.Path(capture_report).read_text())
        ok = report.get("payload_match") and report.get("fill_fields_readable")
        rows.append(_row("test_order_compared", "PASS" if ok else "FAIL",
                         "manual test order matches the bot's body and fills are readable" if ok
                         else "; ".join(report.get("problems", [])) or "capture did not match",
                         None if ok else "fix sig_client.place_raw() / bot._fill_of(), then re-run compare"))
    except FileNotFoundError:
        rows.append(_row("test_order_compared", "INFO" if sig_client.PLACE_PAYLOAD_CONFIRMED else "TODO",
                         "no manual test order compared (optional: the bot halts if a fill is unreadable)"
                         if sig_client.PLACE_PAYLOAD_CONFIRMED else "no manual test order compared yet",
                         "place ONE tiny order by hand with DevTools open, save payload+response to "
                         "capture.json, run: python go_live.py compare capture.json"))
    except (OSError, ValueError):
        rows.append(_row("test_order_compared", "FAIL", "logs/order_capture_check.json unreadable",
                         "re-run python go_live.py compare capture.json"))

    rows.append(_row("payload_confirmed", "PASS" if sig_client.PLACE_PAYLOAD_CONFIRMED else "TODO",
                     f"PLACE_PAYLOAD_CONFIRMED = {sig_client.PLACE_PAYLOAD_CONFIRMED}",
                     None if sig_client.PLACE_PAYLOAD_CONFIRMED else
                     "after compare passes, set PLACE_PAYLOAD_CONFIRMED = True in sig_client.py"))
    return rows


def compare(capture: dict, cli: Client | None = None) -> dict:
    """Compare a hand-placed order with place_raw() and see whether _fill_of() can read its fill."""
    payload, response = capture.get("payload"), capture.get("response")
    if not isinstance(payload, dict) or not isinstance(response, dict):
        raise ValueError('capture must be {"payload": {...}, "response": {...}}')
    cli = cli or Client()
    ours = cli.place_raw(payload.get("exchangeId", 0), payload.get("orderType", "BUY"),
                         payload.get("priceLimit", 0.5), payload.get("quantity", 1))["body"]
    problems = []
    missing = sorted(set(payload) - set(ours))
    extra = sorted(set(ours) - set(payload))
    if missing:
        problems.append(f"site sends fields the bot does not: {missing}")
    if extra:
        problems.append(f"bot sends fields the site does not: {extra}")
    for key in sorted(set(payload) & set(ours)):
        if type(payload[key]) is not type(ours[key]) and not (
                isinstance(payload[key], (int, float)) and isinstance(ours[key], (int, float))):
            problems.append(f"{key}: site sends {type(payload[key]).__name__}, bot sends {type(ours[key]).__name__}")
    for key in ("profileId", "tournamentId"):
        if key in payload and payload.get(key) != ours.get(key):
            problems.append(f"{key} differs from your session/config")
    payload_match = not problems

    numeric = {k: v for k, v in response.items() if isinstance(v, (int, float)) and not isinstance(v, bool)}
    qty_key = FILL_QTY_KEY if FILL_QTY_KEY in response else None
    px_key = next((k for k in FILL_PX_KEYS if k in response), None)
    id_key = next((k for k in ("orderId", "id") if k in response), None)
    if qty_key is None:
        problems.append(f"Client.place() sums '{FILL_QTY_KEY}', which the response lacks; "
                        f"numeric response fields: {sorted(numeric)}")
    if id_key is None:
        problems.append(f"no order id field (orderId/id); response keys: {sorted(response)}")
    return {"payload_match": payload_match, "fill_fields_readable": qty_key is not None and id_key is not None,
            "fill_qty_key": qty_key, "fill_price_key": px_key, "order_id_key": id_key,
            "response_keys": sorted(response), "numeric_response_fields": numeric, "problems": problems,
            "note": None if px_key else "no average-price field; the bot assumes fills at the limit price"}


def set_cookie(cookie: str, env_path: pathlib.Path = ROOT / ".env") -> float | None:
    """Write SIG_COOKIE into .env (other lines kept, file mode 600). Returns token seconds left."""
    cookie = cookie.strip().strip('"').strip("'")
    if cookie.lower().startswith("cookie:"):
        cookie = cookie[7:].strip()
    session = sig_client.decode_supabase_cookie(cookie)
    if not session.get("access_token"):
        raise ValueError("clipboard has no sb-...-auth-token cookie; run copy(document.cookie) on "
                         "sig.thesuper.market while signed in, then retry")
    lines = env_path.read_text().splitlines() if env_path.exists() else \
        (ROOT / ".env.example").read_text().splitlines()
    lines = [l for l in lines if not l.strip().startswith("SIG_COOKIE=")]
    lines.insert(0, f"SIG_COOKIE={cookie}")
    env_path.write_text("\n".join(lines) + "\n")
    env_path.chmod(0o600)
    exp = sig_client.jwt_claims(session["access_token"]).get("exp")
    import time
    return None if exp is None else exp - time.time()


MANUAL_LOG = ROOT / "logs" / "manual_actions.jsonl"


def position(cli: Client, market_id: int, need_exchange: bool = True) -> tuple[float, int | None]:
    """Net YES-terms holding (+YES / -NO) and exchange id for one market."""
    rows = [h for h in cli.holdings(market_id) if str(h.get("settlementOption", "YES")).upper() == "YES"]
    qty = sum(float(h.get("quantity") or 0) for h in rows)
    ex = next((h.get("exchangeId") for h in rows if h.get("exchangeId")), None)
    if ex is None and need_exchange:
        ex = next((l.get("exchangeId") for l in cli.levels(market_id) if l.get("exchangeId")), None)
    return qty, ex


def close_plan(qty: float, limit: float) -> dict:
    """YES-terms order that takes the holding to zero. `limit` is the YES price:
    holding NO -> buy YES at <= limit; holding YES -> sell YES at >= limit."""
    if not 0 < limit < 1:
        raise ValueError("limit must be a YES price between 0 and 1")
    if qty == 0:
        raise ValueError("no position to close")
    side = "BUY" if qty < 0 else "SELL"
    return {"yes_side": side, "yes_price": limit, "qty": abs(qty), "holdings": qty,
            "orders": sig_client.orders_for(side, limit, abs(qty), holdings=qty)}


def close(cli: Client, market_id: int, limit: float, confirm=input, log_path: pathlib.Path = MANUAL_LOG) -> dict:
    qty, ex = position(cli, market_id)
    plan = close_plan(qty, limit)
    held = f"{abs(qty):g} {'NO' if qty < 0 else 'YES'}"
    action = (f"BUY YES <= {limit} (sells your NO at >= {1 - limit:.4f})" if plan["yes_side"] == "BUY"
              else f"SELL YES >= {limit}")
    print(f"Market #{market_id}: holding {held}. To close: {action} x {plan['qty']:g}")
    print("Engine orders:", json.dumps(plan["orders"]))
    if not sig_client.PLACE_PAYLOAD_CONFIRMED:
        raise PermissionError("PLACE_PAYLOAD_CONFIRMED is False; nothing sent")
    if confirm("Send this order LIVE? [y/N] ").strip().lower() != "y":
        return {"status": "CANCELLED"}
    resp = cli.place(market_id, ex, plan["yes_side"], limit, plan["qty"], holdings=qty, dry_run=False)
    after, _ = position(cli, market_id, need_exchange=False)
    result = {"status": "UNKNOWN" if resp.get("_unknown") else "SENT", "market_id": market_id,
              "before": qty, "after": after, "filled": resp.get("filledQuantity"),
              "client_order_id": resp.get("clientOrderId")}
    log_path.parent.mkdir(exist_ok=True)
    with log_path.open("a") as f:
        f.write(json.dumps({"ts": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                            "action": "close", "limit": limit, **result}) + "\n")
    return result


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check", help="readiness checklist")
    c.add_argument("--offline", action="store_true", help="skip read-only SIG requests")
    sub.add_parser("set-cookie", help="write the clipboard cookie to .env (macOS pbpaste)")
    st = sub.add_parser("start", help="readiness check, then run bot + dashboard under the supervisor")
    st.add_argument("--mode", choices=["auto", "confirm", "signal"], default="auto")
    st.add_argument("--strategy", default="arb,fv")
    st.add_argument("--dry", action="store_true", help="run without --live (no real orders)")
    st.add_argument("--release-kill-switch", action="store_true",
                    help="remove logs/KILL_SWITCH first (only do this after reviewing its reason)")
    sub.add_parser("stop", help="stop bot, dashboard and supervisor")
    sub.add_parser("status", help="show what is running")
    cl = sub.add_parser("close", help="exit a position in one market (asks y/N)")
    cl.add_argument("market_id", type=int)
    cl.add_argument("--limit", type=float, required=True, help="YES price limit")
    k = sub.add_parser("compare", help="compare a captured manual order")
    k.add_argument("capture", type=pathlib.Path)
    a = ap.parse_args(argv)

    if a.cmd in ("start", "stop", "status"):
        import supervisor
        if a.cmd == "stop":
            return supervisor.stop()
        if a.cmd == "status":
            return supervisor.status()
        if a.mode == "confirm":
            print("confirm mode needs a terminal for y/N prompts; run bot.py directly instead")
            return 1
        if a.release_kill_switch and bot.KILL_SWITCH.exists():
            print("releasing kill switch:", bot.KILL_SWITCH.read_text().splitlines()[0])
            bot.KILL_SWITCH.unlink()
        rows = check()
        blocking = [r for r in rows if r["status"] in ("FAIL", "TODO")]
        for r in blocking:
            print(f"[{r['status']}] {r['check']}: {r['detail']}" + (f"\n   -> {r['action']}" if r["action"] else ""))
        if blocking:
            print("not starting; fix the items above (kill switch: --release-kill-switch after review)")
            return 1
        args = ["--mode", a.mode, "--interval", "3", "--strategy", a.strategy] + ([] if a.dry else ["--live"])
        return supervisor.start(args)

    if a.cmd == "close":
        result = close(Client(), a.market_id, a.limit)
        print(json.dumps(result, indent=2))
        return 0 if result["status"] in ("SENT", "CANCELLED") and result.get("after", 0) == 0 or \
            result["status"] == "CANCELLED" else 1

    if a.cmd == "set-cookie":
        import subprocess
        import time
        paste = lambda: subprocess.run(["pbpaste"], capture_output=True, text=True).stdout
        if not sig_client.decode_supabase_cookie(paste().strip()).get("access_token"):
            print("Waiting for the cookie on the clipboard. In the signed-in sig.thesuper.market tab,\n"
                  "run  copy(document.cookie)  in the DevTools console. (Ctrl+C to cancel)", flush=True)
            while not sig_client.decode_supabase_cookie(paste().strip()).get("access_token"):
                time.sleep(1)
        left = set_cookie(paste())
        print("SIG_COOKIE saved to .env" + (f"; token expires in {left / 60:.0f} min" if left else ""))
        return 0

    if a.cmd == "check":
        rows = check(offline=a.offline)
        for r in rows:
            print(f"[{r['status']:<4}] {r['check']:<20} {r['detail']}")
            if r["action"]:
                print(f"{'':27}-> {r['action']}")
        blocking = [r for r in rows if r["status"] in ("FAIL", "TODO")]
        print("\nREADY: start with  python bot.py --mode confirm --interval 5 --live" if not blocking
              else f"\nNOT READY: {len(blocking)} item(s) above.")
        return 0 if not blocking else 1

    result = compare(json.loads(a.capture.read_text()))
    CAPTURE_REPORT.parent.mkdir(exist_ok=True)
    CAPTURE_REPORT.write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))
    print(f"\nsaved {CAPTURE_REPORT.relative_to(ROOT)}")
    return 0 if result["payload_match"] and result["fill_fields_readable"] else 1


if __name__ == "__main__":
    sys.exit(main())
