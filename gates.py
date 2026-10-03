"""The single live-execution gate list.

Every surface that decides whether an order may be sent (dashboard status,
order ticket, confirm endpoint) evaluates these 13 gates through `evaluate`.
A gate that cannot be evaluated is reported with pass=None and must be
treated as failing. Nothing here sends orders.
"""
from __future__ import annotations

import hashlib
import json
import pathlib

GATES = (
    ("sig_auth", "SIG authentication"),
    ("payload_verified", "Payload vs real SIG API"),
    ("venue_ids", "Venue identifiers"),
    ("fees_known", "Fees known"),
    ("mapping_approved", "Mapping approved"),
    ("settlement_approved", "Settlement approved"),
    ("quotes_fresh", "Quotes fresh"),
    ("liquidity", "Liquidity present"),
    ("no_breaker", "No circuit breaker"),
    ("risk_limits", "Risk limits pass"),
    ("recon_clean", "Reconciliation clean"),
    ("kill_switch_off", "Kill switch off"),
    ("live_mode", "Live mode selected"),
)
GATE_IDS = tuple(gate_id for gate_id, _ in GATES)
LIVE_MODES = {"human_confirmed", "constrained_live"}
SESSION_OK = {"NORMALIZED", "UNVERIFIED_SCHEMA"}
LIMIT_KEYS = {"per_trade_capital": (int, float), "daily_loss": (int, float),
              "venue_exposure": dict, "event_exposure": (int, float),
              "max_quote_age_s": (int, float), "max_slippage": (int, float),
              "max_residual_contracts": (int, float), "min_net_edge": (int, float),
              "manual_approval": bool, "auto_hedge": bool}


def load_limits(path: str | pathlib.Path) -> dict:
    """Load and validate risk limits; raises ValueError on anything malformed."""
    data = json.loads(pathlib.Path(path).read_text())
    if not isinstance(data, dict):
        raise ValueError("risk limits must be an object")
    for key, kind in LIMIT_KEYS.items():
        value = data.get(key)
        if not isinstance(value, kind) or (kind is not bool and isinstance(value, bool)):
            raise ValueError(f"risk limit {key} missing or invalid")
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value < 0:
            raise ValueError(f"risk limit {key} must be nonnegative")
    if data["auto_hedge"]:
        raise ValueError("auto_hedge must stay false")
    res = data.get("high_ev_reserve")             # optional: capital above venue_exposure.sig
    if res is not None and not (isinstance(res, dict) and all(
            isinstance(res.get(k), (int, float)) and not isinstance(res.get(k), bool) and res[k] >= 0
            for k in ("capital", "min_roi"))):
        raise ValueError("risk limit high_ev_reserve must have nonnegative capital and min_roi")
    return data


def _gate(gate_id, passed, detail, scope):
    return {"id": gate_id, "name": dict(GATES)[gate_id], "pass": passed,
            "detail": detail, "scope": scope}


def _per_candidate(gate_id, detail="evaluated per opportunity"):
    return _gate(gate_id, None, detail, "candidate")


def _system_gates(system: dict) -> dict:
    limits, limits_error = system.get("limits"), system.get("limits_error")
    session = system.get("sig_session")
    kill = system.get("kill_switch") or {}
    recon = system.get("recon")
    mode = system.get("mode", "research")
    age = system.get("sig_snapshot_age_s")
    breakers = system.get("active_breakers") or {}

    if session in SESSION_OK:
        auth = _gate("sig_auth", True, f"SIG session readable ({session})", "system")
    else:
        auth = _gate("sig_auth", False, f"SIG session {session or 'not checked yet'}", "system")

    if limits is None:
        fresh = _gate("quotes_fresh", False, "no quote-age limit configured", "system")
    elif age is None:
        fresh = _gate("quotes_fresh", False, "no SIG snapshot yet", "system")
    else:
        ok = age <= limits["max_quote_age_s"]
        fresh = _gate("quotes_fresh", ok, f"SIG snapshot {age:.1f}s old (max {limits['max_quote_age_s']:g}s)", "system")

    if not recon:
        rec = _gate("recon_clean", False, "reconciliation not run", "system")
    else:
        rec = _gate("recon_clean", recon.get("status") == "RECONCILED",
                    f"last reconciliation {recon.get('status', 'UNKNOWN')} at {recon.get('as_of', '?')}", "system")

    return {
        "sig_auth": auth,
        "payload_verified": _gate("payload_verified", bool(system.get("payload_verified")),
                                  "PLACE_PAYLOAD_CONFIRMED is True" if system.get("payload_verified")
                                  else "PLACE_PAYLOAD_CONFIRMED is False", "system"),
        "quotes_fresh": fresh,
        "no_breaker": _gate("no_breaker", None, f"{len(breakers)} market(s) under an active breaker; "
                            "evaluated per opportunity", "candidate"),
        "risk_limits": (_gate("risk_limits", True, "limits loaded", "system") if limits is not None
                        else _gate("risk_limits", False, f"limits unavailable: {limits_error or 'not loaded'}", "system")),
        "recon_clean": rec,
        "kill_switch_off": _gate("kill_switch_off", not kill.get("engaged"),
                                 f"engaged: {kill.get('reason') or 'no reason recorded'}" if kill.get("engaged")
                                 else "not engaged", "system"),
        "live_mode": _gate("live_mode", mode in LIVE_MODES, f"mode is {mode}", "system"),
    }


def _candidate_gates(system: dict, candidate: dict, base: dict) -> dict:
    limits = system.get("limits")
    legs = candidate.get("legs") or []
    breakers = system.get("active_breakers") or {}
    out = {}
    if not legs:
        for gate_id in ("venue_ids", "fees_known", "liquidity", "quotes_fresh", "no_breaker"):
            out[gate_id] = _gate(gate_id, False, "candidate has no legs", "candidate")
    else:
        unresolved = [leg.get("market_id") for leg in legs if not leg.get("venue_id_known")]
        out["venue_ids"] = _gate("venue_ids", not unresolved,
                                 f"unresolved: {unresolved}" if unresolved else "all legs resolved", "candidate")
        unknown_fees = [leg.get("market_id") for leg in legs if leg.get("fee") is None]
        out["fees_known"] = _gate("fees_known", not unknown_fees,
                                  f"fee unknown for {unknown_fees}" if unknown_fees else "all fees known", "candidate")
        short = [(leg.get("market_id"), leg.get("visible_qty", 0), leg.get("qty"))
                 for leg in legs if (leg.get("visible_qty") or 0) < (leg.get("qty") or 0)]
        out["liquidity"] = _gate("liquidity", not short,
                                 "; ".join(f"{m}: {v:g} of {q:g} visible" for m, v, q in short) if short
                                 else "visible depth covers every leg", "candidate")
        ages = [leg.get("quote_age_s") for leg in legs]
        if limits is None or any(a is None for a in ages):
            out["quotes_fresh"] = _gate("quotes_fresh", False, "quote age or limit unknown", "candidate")
        else:
            worst = max(ages)
            out["quotes_fresh"] = _gate("quotes_fresh", worst <= limits["max_quote_age_s"],
                                        f"oldest leg {worst:.1f}s (max {limits['max_quote_age_s']:g}s)", "candidate")
        hit = sorted({int(leg["market_id"]) for leg in legs
                      if str(leg.get("venue", "sig")).lower() == "sig" and int(leg["market_id"]) in breakers})
        out["no_breaker"] = _gate("no_breaker", not hit,
                                  f"active breaker on {hit}" if hit else "no active breaker", "candidate")

    mapping = candidate.get("mapping")
    out["mapping_approved"] = _gate("mapping_approved", mapping in ("approved", "single_venue"),
                                    f"mapping {mapping or 'unknown'}", "candidate")
    settlement = candidate.get("settlement")
    out["settlement_approved"] = _gate("settlement_approved", settlement == "approved",
                                       candidate.get("settlement_detail") or f"settlement {settlement or 'unknown'}",
                                       "candidate")
    if limits is None:
        out["risk_limits"] = base["risk_limits"]
    else:
        problems = []
        capital = candidate.get("capital")
        if capital is None or capital > limits["per_trade_capital"]:
            problems.append(f"capital {capital} > per-trade {limits['per_trade_capital']:g}")
        slippage = candidate.get("slippage")
        if slippage is None or slippage > limits["max_slippage"]:
            problems.append(f"slippage {slippage} > max {limits['max_slippage']:g}")
        out["risk_limits"] = _gate("risk_limits", not problems, "; ".join(problems) or "within limits", "candidate")
    return out


def evaluate(system: dict, candidate: dict | None = None) -> list[dict]:
    """Return all 13 gates in GATES order.

    `system` holds process-wide state (session, kill switch, mode, limits,
    snapshot age, breakers, reconciliation). `candidate`, when given, holds
    one opportunity or ticket: legs with market_id, venue, qty, visible_qty,
    quote_age_s, fee, venue_id_known; plus mapping, settlement, capital and
    slippage. Missing values fail their gate.
    """
    gates = _system_gates(system)
    if candidate is None:
        for gate_id in ("venue_ids", "fees_known", "mapping_approved", "settlement_approved", "liquidity"):
            gates[gate_id] = _per_candidate(gate_id)
    else:
        gates.update(_candidate_gates(system, candidate, gates))
    return [gates[gate_id] for gate_id in GATE_IDS]


def all_pass(gates: list[dict]) -> bool:
    return len(gates) == len(GATE_IDS) and all(g["pass"] is True for g in gates)


def summary(gates: list[dict]) -> dict:
    return {"pass": sum(g["pass"] is True for g in gates),
            "fail": sum(g["pass"] is False for g in gates),
            "per_candidate": sum(g["pass"] is None for g in gates),
            "total": len(gates)}


def gate_hash(gates: list[dict]) -> str:
    """Fingerprint of gate outcomes. Details (e.g. live quote ages) are excluded
    so the hash changes only when a gate flips, which is what confirm checks."""
    material = json.dumps([(g["id"], g["pass"]) for g in gates], separators=(",", ":"))
    return hashlib.sha256(material.encode()).hexdigest()
