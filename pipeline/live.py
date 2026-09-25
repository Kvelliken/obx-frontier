"""Live-sporing: hver rebalansering lagres i state/live.json og committes til repoet.

Kun vektene lagres. Avkastningen regnes ut på nytt hver dag fra kursene,
så historikken blir riktig selv om Yahoo justerer kurser for utbytte i ettertid.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import pandas as pd

from .engine import Portfolio, decide

log = logging.getLogger(__name__)
METHODS = ("black_litterman", "bayes_stein")


def load_state(path: Path) -> dict:
    if path.exists():
        st = json.loads(path.read_text(encoding="utf-8"))
    else:
        st = {}
    st.setdefault("versjon", 1)
    st.setdefault("metoder", {})
    for m in METHODS:
        st["metoder"].setdefault(m, [])
    return st


def save_state(path: Path, st: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(st, indent=1, ensure_ascii=False), encoding="utf-8")


def replay(entries: list[dict], rets: pd.DataFrame, cost: float) -> Portfolio:
    tickers = sorted(set(rets.columns) | {t for e in entries for t in e["vekter"]})
    r = rets.reindex(columns=tickers).fillna(0.0)
    port = Portfolio(tickers, cost)
    if not entries:
        return port
    by_date = {pd.Timestamp(e["beslutningsdato"]): e for e in entries}
    start = min(by_date)
    arr = r.to_numpy()
    for i, d in enumerate(r.index):
        if d < start:
            continue
        port.step(d, arr[i])
        if d in by_date:
            port.schedule(pd.Series(by_date[d]["vekter"]), d)
    return port


def update_live(md, cfg, state: dict, rets: pd.DataFrame) -> dict:
    """Rebalanserer hver metode hvis det ikke er gjort i inneværende måned. Returnerer dagens beslutninger."""
    cost = float(cfg["optimering"]["transaksjonskostnad"])
    latest = rets.index[-1]
    decisions = {}
    for m in METHODS:
        entries = state["metoder"][m]
        port = replay(entries, rets, cost)
        last = pd.Timestamp(entries[-1]["beslutningsdato"]) if entries else None
        if last is not None and last.to_period("M") == latest.to_period("M"):
            continue
        prev = port.weights()
        if port.pending is not None:  # handel som ikke er gjennomført ennå
            prev = pd.Series(port.pending[0], index=port.tickers)
        dec = decide(md, cfg, latest, m, prev)
        if dec is None:
            log.error("Live-rebalansering feilet for %s", m)
            continue
        entries.append({
            "beslutningsdato": latest.strftime("%Y-%m-%d"),
            "vekter": {t: round(float(v), 6) for t, v in dec.weights.items()},
            "vekter_for": {t: round(float(v), 6) for t, v in dec.prev_weights.items() if v > 1e-6},
            "omsetning": round(dec.turnover, 4),
            "merknader": dec.notes,
        })
        decisions[m] = dec
        log.info("Live-rebalansering %s %s: %d aksjer, omsetning %.1f %%",
                 m, latest.date(), len(dec.weights), dec.turnover * 100)
    return decisions
