"""
sig_client.py — thin HTTP client for sig.thesuper.market (read + trade).

Auth: the site uses your browser session cookie. Put the full Cookie header
value in a `.env` file next to this script:

    SIG_COOKIE=...paste from DevTools...
    SIG_TOURNAMENT=bda92870-621e-47b0-bc3c-3602c5c26f55   # optional

Never commit .env.
"""
from __future__ import annotations

import base64
import datetime as dt
import json
import os
import pathlib
import re
import urllib.parse
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List

import requests

from arb_engine import Book

BASE = "https://sig.thesuper.market"
ENV_PATH = pathlib.Path(__file__).with_name(".env")
# Public Supabase project key (role "anon"), read once from the site's JS and cached.
SUPABASE_PUBLIC = pathlib.Path(__file__).with_name("logs") / "supabase_public.json"
# @supabase/ssr splits cookies longer than this into .0, .1, ... chunks.
COOKIE_CHUNK = 3180
DEFAULT_TOURNAMENT = "bda92870-621e-47b0-bc3c-3602c5c26f55"

# The payload below matches the site's own order builder (read from the JS bundle
# with dump_order_logic_console.js, 1 Oct). Enabled 1 Oct for the competition without
# a manual capture: a live response missing quantityTraded is treated as UNKNOWN, which
# halts the bot and engages the kill switch. `go_live.py compare` still verifies a capture.
PLACE_PAYLOAD_CONFIRMED = True


class RateLimited(RuntimeError):
    """SIG (Vercel) answered 429. Callers must back off; never retry immediately."""

    def __init__(self, path: str, retry_after: float | None = None):
        super().__init__(f"{path} -> 429 Too Many Requests")
        self.retry_after = retry_after


def load_env(path: str | os.PathLike = None) -> None:
    """Minimal .env loader (no python-dotenv dependency)."""
    p = pathlib.Path(path or pathlib.Path(__file__).with_name(".env"))
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


class Client:
    def __init__(self, cookie: str | None = None, tournament: str | None = None,
                 concurrency: int = 8, timeout: float = 20):
        load_env()
        self.cookie = cookie or os.environ.get("SIG_COOKIE", "")
        self.tournament = tournament or os.environ.get("SIG_TOURNAMENT", DEFAULT_TOURNAMENT)
        self.concurrency, self.timeout = concurrency, timeout
        self.s = requests.Session()
        self.s.headers.update({"Content-Type": "application/json", "User-Agent": "sig-arb/0.2"})
        if self.cookie:
            self.s.headers["Cookie"] = self.cookie
        sess = decode_supabase_cookie(self.cookie) if self.cookie else {}
        self.session = sess
        self.access_token = os.environ.get("SIG_ACCESS_TOKEN") or sess.get("access_token")
        self.profile_id = os.environ.get("SIG_PROFILE_ID") or (sess.get("user") or {}).get("id")
        # Renewal needs the cookie's refresh token; a pinned SIG_ACCESS_TOKEN opts out.
        self.can_refresh = bool(sess.get("refresh_token") and not os.environ.get("SIG_ACCESS_TOKEN"))

    # ------------------------------------------------------------ session
    def supabase_ref(self) -> str | None:
        for c in self.cookie.split(";"):
            k = c.strip().split("=", 1)[0]
            if k.startswith("sb-") and "-auth-token" in k:
                return k[3:k.index("-auth-token")]
        return None

    def supabase_anon_key(self, ref: str, cache: pathlib.Path = None) -> str:
        """The site's public anon key for project `ref` (cached; found in its JS bundle)."""
        cache = cache or SUPABASE_PUBLIC
        try:
            j = json.loads(cache.read_text())
            if j.get("ref") == ref and j.get("anon_key"):
                return j["anon_key"]
        except (OSError, ValueError):
            pass
        html = self.s.get(BASE + "/", timeout=self.timeout).text
        for src in sorted(set(re.findall(r'/_next/static/[^"\']+\.js', html))):
            js = self.s.get(BASE + src, timeout=self.timeout).text
            for tok in re.findall(r"eyJ[\w-]{10,}\.eyJ[\w-]{20,}\.[\w-]{10,}", js):
                claims = jwt_claims(tok)
                if claims.get("role") == "anon" and claims.get("ref") == ref:
                    cache.parent.mkdir(parents=True, exist_ok=True)
                    cache.write_text(json.dumps({"ref": ref, "anon_key": tok}))
                    return tok
        raise RuntimeError("Supabase anon key not found in the site bundle")

    def refresh_session(self, env_path: pathlib.Path = None) -> float | None:
        """Swap the refresh token for a new session, as the browser does hourly.
        Supabase rotates refresh tokens, so the new session is written to .env at once;
        a restart must never reuse the retired token. Returns token seconds left."""
        if not self.can_refresh:
            raise PermissionError("no refresh token in SIG_COOKIE (or SIG_ACCESS_TOKEN is pinned)")
        ref = self.supabase_ref()
        r = requests.post(f"https://{ref}.supabase.co/auth/v1/token", params={"grant_type": "refresh_token"},
                          json={"refresh_token": self.session["refresh_token"]}, timeout=self.timeout,
                          headers={"apikey": self.supabase_anon_key(ref), "Content-Type": "application/json"})
        if r.status_code != 200:
            raise PermissionError(f"session refresh failed ({r.status_code}): {r.text[:200]}")
        sess = r.json()
        self.session, self.access_token = sess, sess["access_token"]
        self.profile_id = (sess.get("user") or {}).get("id") or self.profile_id
        self.cookie = replace_session_cookie(self.cookie, ref, sess)
        self.s.headers["Cookie"] = self.cookie
        save_env_value("SIG_COOKIE", self.cookie, env_path or ENV_PATH)
        return self.token_seconds_left()

    def token_seconds_left(self) -> float | None:
        """Seconds until the access token's JWT `exp`; None when absent or unreadable.
        Supabase tokens are short-lived (about an hour); a fresh cookie renews them."""
        exp = jwt_claims(self.access_token).get("exp") if self.access_token else None
        return None if exp is None else float(exp) - dt.datetime.now(dt.timezone.utc).timestamp()

    # ---------------------------------------------------------------- http
    def _get(self, path: str, **params):
        # Reads are idempotent: retry once, since SIG's API sometimes stalls for 10s+.
        try:
            r = self.s.get(BASE + path, params=params, timeout=self.timeout)
        except (requests.Timeout, requests.ConnectionError):
            r = self.s.get(BASE + path, params=params, timeout=self.timeout)
        if r.status_code == 429:
            try:
                retry_after = float(r.headers.get("Retry-After"))
            except (TypeError, ValueError):
                retry_after = None
            raise RateLimited(path, retry_after)
        if r.status_code in (401, 403):
            raise PermissionError(f"{path} -> {r.status_code}. Is SIG_COOKIE set / still valid?")
        r.raise_for_status()
        return r.json()

    def _post(self, path: str, body: dict):
        r = self.s.post(BASE + path, data=json.dumps(body), timeout=self.timeout)
        try:
            data = r.json()
        except ValueError:
            data = {"raw": r.text[:500]}
        if r.status_code >= 400:
            raise RuntimeError(f"{path} {r.status_code}: {data}")
        return data

    # ---------------------------------------------------------------- read
    def markets(self) -> List[dict]:
        """Every market. Pages are ~20 markets and slow, so once the first page reports
        totalMarkets the remaining offsets are fetched in parallel."""
        first = self._get("/api/markets/page-data", offset=0)
        out, total, step = list(first["markets"]), first.get("totalMarkets"), len(first["markets"])
        if isinstance(total, int) and step and first.get("nextOffset") == step:
            offsets = list(range(step, total, step))
            with ThreadPoolExecutor(self.concurrency) as ex:
                for j in ex.map(lambda o: self._get("/api/markets/page-data", offset=o), offsets):
                    out += j["markets"]
            seen, uniq = set(), []
            for m in out:
                if m["id"] not in seen:
                    seen.add(m["id"]); uniq.append(m)
            return uniq
        off = first.get("nextOffset") if first["markets"] else None
        while off is not None:
            j = self._get("/api/markets/page-data", offset=off)
            out += j["markets"]
            off = j.get("nextOffset") if j["markets"] else None
        return out

    def book_raw(self, market_id: int) -> dict:
        return self._get(f"/api/markets/{market_id}/orders", marketId=market_id,
                         tournamentId=self.tournament)

    def levels(self, market_id: int) -> List[dict]:
        return levels_from_response(self.book_raw(market_id))

    def all_levels(self, ids: List[int]) -> Dict[int, List[dict]]:
        with ThreadPoolExecutor(self.concurrency) as ex:
            return dict(zip(ids, ex.map(self.levels, ids)))

    def books(self, ids: List[int]) -> Dict[int, Book]:
        return {i: Book.from_levels(i, L) for i, L in self.all_levels(ids).items()}

    def balance(self) -> float | None:
        try:
            j = self._get("/api/tournaments/member-balance", tournamentId=self.tournament)
        except Exception:
            return None
        for k in ("balance", "userBalance", "memberBalance", "cash"):
            if isinstance(j, dict) and k in j:
                return float(j[k])
        return None

    def portfolio(self) -> dict:
        """Holdings (avg price in the held side's terms), cash and daily P&L."""
        return self._get("/api/portfolio/page-data", tournamentId=self.tournament)

    def deployed_capital(self) -> float:
        """Capital tied up in open positions: sum of |quantity| x average price paid."""
        return sum(abs(float(h.get("quantity") or 0)) * float(h.get("averagePricePaid") or 0)
                   for h in self.portfolio().get("holdings", []))

    def my_orders(self, market_id: int) -> List[dict]:
        j = self.book_raw(market_id)
        if "myOrders" in j:
            return j["myOrders"] or []
        return [o for b in j.get("books", []) for o in (b.get("myOrders") or [])]

    def holdings(self, market_id: int) -> list:
        j = self._get(f"/api/markets/{market_id}/live", tournamentId=self.tournament)
        return j.get("userHoldings", [])

    # --------------------------------------------------------------- trade
    def quote(self, exchange_id: int, order_type: str, price: float, qty: float) -> dict:
        """Collateral/linkage preview (NOT a fill estimate). Cookie auth."""
        return self._post("/api/trading/orders/quote", {
            "exchangeId": exchange_id, "orderType": order_type,
            "priceLimit": round(price, 4), "quantity": qty, "tournamentId": self.tournament})

    def place_raw(self, exchange_id: int, order_type: str, price_limit: float, quantity: float,
                  dry_run: bool = True, idempotency_key: str | None = None) -> dict:
        """Exactly what the site sends. quantity: +YES / -NO (shares).
        price_limit is in the terms of the side traded (NO orders: NO price)."""
        body = {"createdAt": dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds")
                .replace("+00:00", "Z"),
                "exchangeId": int(exchange_id), "profileId": self.profile_id,
                "orderType": order_type, "priceLimit": round(float(price_limit), 4),
                "quantity": int(quantity), "open": True, "tournamentId": self.tournament,
                "idempotencyKey": idempotency_key or str(uuid.uuid4())}
        if dry_run or not PLACE_PAYLOAD_CONFIRMED:
            return {"dryRun": True, "body": body}
        if not self.access_token:
            raise PermissionError("no Supabase access_token (set SIG_COOKIE with the sb-*-auth-token cookie)")
        if not self.profile_id:
            raise PermissionError("no profile id (set SIG_PROFILE_ID or a cookie with the Supabase user)")
        r = self.s.post(BASE + "/api/trading/orders/place", data=json.dumps(body), timeout=self.timeout,
                        headers={"Authorization": f"Bearer {self.access_token}"})
        try:
            data = r.json()
        except ValueError:
            data = {"raw": r.text[:500]}
        data["_status"] = r.status_code
        if r.status_code >= 500:
            data["_unknown"] = True          # outcome unknown: reconcile before retrying
        elif r.status_code in (401, 403):
            raise PermissionError(f"place {r.status_code}: session expired or not allowed")
        elif r.status_code >= 400:
            raise RuntimeError(f"place {r.status_code}: {data}")
        return data

    def place(self, market_id: int, exchange_id: int, yes_side: str, yes_price: float, qty: float,
              holdings: float = 0.0, dry_run: bool = True, client_order_id: str | None = None) -> dict:
        """Trade in YES terms; converted to the engine's representation.
        yes_side BUY = get longer YES, SELL = get shorter YES. Returns the (last) response,
        with all sub-orders under 'orders'.

        Each sub-order's idempotencyKey derives from client_order_id, so resending the same
        intent after a timeout cannot double-fill. A sub-order with an unknown outcome (5xx)
        stops the rest and sets '_unknown': reconcile before doing anything else."""
        client_order_id = client_order_id or str(uuid.uuid4())
        resps = []
        for i, o in enumerate(orders_for(yes_side, yes_price, qty, holdings)):
            resps.append(self.place_raw(exchange_id, o["orderType"], o["priceLimit"], o["quantity"],
                                        dry_run, idempotency_key=idempotency_key(client_order_id, i)))
            # A live response without quantityTraded means we cannot tell what filled:
            # treat it as unknown so the bot halts instead of assuming zero.
            if not resps[-1].get("dryRun") and "quantityTraded" not in resps[-1]:
                resps[-1]["_unknown"] = True
                resps[-1]["_unknown_reason"] = "response has no quantityTraded"
            if resps[-1].get("_unknown"):
                break
        filled = sum(abs(r.get("quantityTraded", 0) or 0) for r in resps if not r.get("dryRun"))
        out = dict(resps[-1]); out["orders"] = resps
        out["clientOrderId"] = client_order_id
        out["filledQuantity"] = qty if all(r.get("dryRun") for r in resps) else filled
        if any(r.get("_unknown") for r in resps):
            out["_unknown"] = True
        return out

    def cancel(self, order_id: str, dry_run: bool = True):
        if dry_run or not order_id:
            return {"dryRun": True}
        r = self.s.post(BASE + "/api/trading/orders/cancel", timeout=self.timeout,
                        data=json.dumps({"orderId": order_id, "tournamentId": self.tournament}),
                        headers={"Authorization": f"Bearer {self.access_token}"})
        return {"_status": r.status_code, "body": r.text[:500]}


# ------------------------------------------------------------------ helpers
def idempotency_key(client_order_id: str, index: int = 0) -> str:
    """Stable UUID per (client order, sub-order), so a retry reuses the same key."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"sig-arb:{client_order_id}:{index}"))


def levels_from_response(j: dict) -> List[dict]:
    """Accept both book formats: legacy {levels:[...]} and live {books:[{bids,asks}]}."""
    if "levels" in j:
        return j["levels"]
    out = []
    for b in j.get("books", []):
        ex = b.get("exchangeId")
        out += [{"exchangeId": ex, "side": "BUY", "isYes": True, "price": l["price"],
                 "quantity": l["quantity"]} for l in b.get("bids", [])]
        out += [{"exchangeId": ex, "side": "SELL", "isYes": True, "price": l["price"],
                 "quantity": l["quantity"]} for l in b.get("asks", [])]
    return out


def orders_for(yes_side: str, yes_price: float, qty: float, holdings: float = 0.0) -> List[dict]:
    """Map a YES-terms intent onto engine orders (signed qty, side-specific price).
    holdings: +YES / -NO currently held on this exchange."""
    qty = int(qty)
    out = []
    if yes_side == "BUY":                       # want more YES
        close = min(qty, int(max(0, -holdings)))       # first sell NO we hold
        if close:
            out.append({"orderType": "SELL", "quantity": -close, "priceLimit": round(1 - yes_price, 4)})
        if qty - close:
            out.append({"orderType": "BUY", "quantity": qty - close, "priceLimit": round(yes_price, 4)})
    elif yes_side == "SELL":                    # want less YES
        close = min(qty, int(max(0, holdings)))         # first sell YES we hold
        if close:
            out.append({"orderType": "SELL", "quantity": close, "priceLimit": round(yes_price, 4)})
        if qty - close:
            out.append({"orderType": "BUY", "quantity": -(qty - close), "priceLimit": round(1 - yes_price, 4)})
    else:
        raise ValueError(yes_side)
    return out


def jwt_claims(token: str) -> dict:
    """Unverified JWT payload (only used to read `exp`); {} if it is not a JWT."""
    try:
        part = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
    except (IndexError, ValueError, AttributeError):
        return {}
    return claims if isinstance(claims, dict) else {}


def replace_session_cookie(cookie_header: str, ref: str, session: dict) -> str:
    """Cookie header with the sb-<ref>-auth-token cookie(s) replaced by `session`,
    encoded and chunked the way @supabase/ssr does."""
    name = f"sb-{ref}-auth-token"
    keep = [c.strip() for c in cookie_header.split(";")
            if c.strip() and not c.strip().split("=", 1)[0].startswith(name)]
    value = "base64-" + base64.urlsafe_b64encode(json.dumps(session, separators=(",", ":")).encode()).decode().rstrip("=")
    if len(value) <= COOKIE_CHUNK:
        parts = [f"{name}={value}"]
    else:
        parts = [f"{name}.{i}={value[o:o + COOKIE_CHUNK]}" for i, o in enumerate(range(0, len(value), COOKIE_CHUNK))]
    return "; ".join(keep + parts)


def save_env_value(key: str, value: str, env_path: pathlib.Path) -> None:
    """Set KEY=value in .env (other lines kept), atomically, mode 600."""
    lines = env_path.read_text().splitlines() if env_path.exists() else []
    lines = [l for l in lines if not l.strip().startswith(key + "=")]
    lines.insert(0, f"{key}={value}")
    tmp = env_path.with_suffix(".tmp")
    tmp.write_text("\n".join(lines) + "\n")
    tmp.chmod(0o600)
    tmp.replace(env_path)
    os.environ[key] = value


def decode_supabase_cookie(cookie_header: str) -> dict:
    """Extract the Supabase session JSON from sb-<ref>-auth-token(.N) cookies."""
    parts = {}
    for c in cookie_header.split(";"):
        if "=" not in c:
            continue
        k, v = c.strip().split("=", 1)
        if k.startswith("sb-") and "-auth-token" in k:
            idx = int(k.rsplit(".", 1)[1]) if k.rsplit(".", 1)[-1].isdigit() else 0
            parts[idx] = urllib.parse.unquote(v)
    if not parts:
        return {}
    raw = "".join(parts[i] for i in sorted(parts))
    if raw.startswith("base64-"):
        b = raw[7:]
        raw = base64.urlsafe_b64decode(b + "=" * (-len(b) % 4)).decode()
    try:
        j = json.loads(raw)
    except ValueError:
        return {}
    if isinstance(j, list):                     # very old supabase-js format
        j = {"access_token": j[0], "refresh_token": j[1]}
    return j
