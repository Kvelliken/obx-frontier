"""Beslutning, simulering og backtest.

Tidslogikk (lik for backtest og live):
- Beslutning tas etter børsslutt på beslutningsdagen, med data til og med den dagen.
- Handelen skjer til sluttkurs neste handelsdag (inngangsdagen).
- Transaksjonskostnad = kostnadssats × Σ|ny vekt − drevet vekt|.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np
import pandas as pd

from .model import estimate, select_universe
from .optimizer import optimize

log = logging.getLogger(__name__)


@dataclass
class Decision:
    date: pd.Timestamp
    weights: pd.Series          # målvekter (kun > 0)
    prev_weights: pd.Series     # drevne vekter rett før beslutning
    turnover: float             # Σ|Δw| / 2 (én vei)
    notes: list[str]
    est: object
    uni: object
    mode: str


def decide(md, cfg, asof, method, prev: pd.Series | None) -> Decision | None:
    uni = select_universe(md, cfg, asof)
    est = estimate(md, uni, cfg, asof, method)
    if est is None:
        return None
    sectors = cfg.candidates["sektor"].reindex(est.tickers).fillna("Ukjent").to_numpy()
    caps = uni.caps.reindex(est.tickers).to_numpy(dtype=float)
    prev_vec, outside = None, 0.0
    if prev is not None and prev.sum() > 0:
        # posisjoner utenfor dagens univers må selges; de teller med i omsetningen
        prev_vec = prev.reindex(est.tickers).fillna(0).to_numpy()
        outside = float(prev.drop(est.tickers, errors="ignore").sum())
    res = optimize(est.mu_excess, est.cov, caps, sectors, cfg["optimering"], prev_vec, outside)
    if res is None:
        log.warning("%s %s: ingen løsning", method, asof.date())
        return None
    w = pd.Series(res.weights, index=est.tickers)
    w = w[w > 1e-6]
    prev_s = prev[prev > 1e-9] if prev is not None else pd.Series(dtype=float)
    all_t = w.index.union(prev_s.index)
    turnover = float((w.reindex(all_t).fillna(0) - prev_s.reindex(all_t).fillna(0)).clip(lower=0).sum())
    return Decision(asof, w, prev_s, turnover, res.notes, est, uni, res.mode)


class Portfolio:
    """Holder styr på beholdning, verdi og handler dag for dag."""

    def __init__(self, tickers, cost):
        self.tickers = list(tickers)
        self.pos = {t: i for i, t in enumerate(self.tickers)}
        self.cost = cost
        self.h = None
        self.pending = None
        self.nav = {}
        self.entries = []

    def weights(self) -> pd.Series | None:
        if self.h is None:
            return None
        return pd.Series(self.h / self.h.sum(), index=self.tickers)

    def schedule(self, weights: pd.Series, decision_date):
        vec = np.zeros(len(self.tickers))
        for t, v in weights.items():
            vec[self.pos[t]] = v
        self.pending = (vec / vec.sum(), decision_date)

    def step(self, d, r: np.ndarray):
        if self.h is not None:
            self.h = self.h * (1 + r)
        if self.pending is not None:
            new, dec = self.pending
            val = 1.0 if self.h is None else self.h.sum()
            cur = np.zeros_like(new) if self.h is None else self.h / val
            traded = float(np.abs(new - cur).sum())
            val *= 1 - self.cost * traded
            self.h = new * val
            self.entries.append({"beslutning": dec, "inngang": d, "handlet": traded})
            self.pending = None
        if self.h is not None:
            self.nav[d] = float(self.h.sum())

    def nav_series(self) -> pd.Series:
        return pd.Series(self.nav, dtype=float)


def daily_returns(md) -> pd.DataFrame:
    return md.adj.pct_change(fill_method=None).fillna(0.0)


def first_trading_days(index: pd.DatetimeIndex, start) -> set:
    s = pd.Series(index, index=index)
    firsts = s.groupby(index.to_period("M")).first()
    return {d for d in firsts if d >= pd.Timestamp(start)}


def run_backtest(md, cfg, method, rets=None):
    rets = daily_returns(md) if rets is None else rets
    cost = float(cfg["optimering"]["transaksjonskostnad"])
    port = Portfolio(rets.columns, cost)
    decisions = first_trading_days(rets.index, cfg["backtest"]["startdato"])
    arr = rets.to_numpy()
    log_rows = []
    for i, d in enumerate(rets.index):
        port.step(d, arr[i])
        if d in decisions:
            dec = decide(md, cfg, d, method, port.weights())
            if dec is not None:
                port.schedule(dec.weights, d)
                log_rows.append({
                    "dato": d.strftime("%Y-%m-%d"),
                    "antall": int(len(dec.weights)),
                    "omsetning": round(dec.turnover, 4),
                    "univers": len(dec.uni.tickers),
                })
    return port, log_rows


# ───────────────────────── Nøkkeltall ─────────────────────────

def metrics(nav: pd.Series, bench: pd.Series, rf_annual: pd.Series) -> dict | None:
    nav = nav.dropna()
    if len(nav) < 21:
        return None
    b = bench.reindex(nav.index).ffill()
    r = nav.pct_change().dropna()
    rb = b.pct_change().reindex(r.index).fillna(0)
    rf_d = (1 + rf_annual.reindex(r.index).ffill().fillna(0)) ** (1 / 252) - 1
    years = (nav.index[-1] - nav.index[0]).days / 365.25
    ann = lambda x: float(x.iloc[-1] / x.iloc[0]) ** (1 / years) - 1 if years > 0 else np.nan

    def dd(x):
        return float((x / x.cummax() - 1).min())

    vol = float(r.std() * np.sqrt(252))
    vol_b = float(rb.std() * np.sqrt(252))
    active = r - rb
    te = float(active.std() * np.sqrt(252))
    beta = float(np.cov(r, rb)[0, 1] / rb.var()) if rb.var() > 0 else np.nan
    cagr, cagr_b = ann(nav), ann(b)
    sharpe = float((r - rf_d).mean() / r.std() * np.sqrt(252)) if r.std() > 0 else np.nan
    sharpe_b = float((rb - rf_d).mean() / rb.std() * np.sqrt(252)) if rb.std() > 0 else np.nan
    ir = float(active.mean() * 252 / te) if te > 0 else np.nan

    def clean(x):
        return None if x is None or not np.isfinite(x) else round(float(x), 4)

    return {
        "fra": nav.index[0].strftime("%Y-%m-%d"),
        "til": nav.index[-1].strftime("%Y-%m-%d"),
        "portefolje": {
            "total": clean(nav.iloc[-1] / nav.iloc[0] - 1), "arlig": clean(cagr), "vol": clean(vol),
            "sharpe": clean(sharpe), "maks_fall": clean(dd(nav)), "beta": clean(beta),
            "tracking_error": clean(te), "informasjonsrate": clean(ir),
        },
        "indeks": {
            "total": clean(b.iloc[-1] / b.iloc[0] - 1), "arlig": clean(cagr_b), "vol": clean(vol_b),
            "sharpe": clean(sharpe_b), "maks_fall": clean(dd(b)), "beta": 1.0,
            "tracking_error": None, "informasjonsrate": None,
        },
    }


def yearly_returns(nav: pd.Series, bench: pd.Series) -> list[dict]:
    nav = nav.dropna()
    b = bench.reindex(nav.index).ffill()
    out = []
    for y, grp in nav.groupby(nav.index.year):
        prev_end = nav[nav.index < grp.index[0]]
        start_p = prev_end.iloc[-1] if len(prev_end) else grp.iloc[0]
        bprev = b[b.index < grp.index[0]]
        start_b = bprev.iloc[-1] if len(bprev) else b.loc[grp.index[0]]
        out.append({
            "ar": int(y),
            "portefolje": round(float(grp.iloc[-1] / start_p - 1), 4),
            "indeks": round(float(b.loc[grp.index[-1]] / start_b - 1), 4),
            "hele_aret": bool(len(prev_end) > 0 and grp.index[-1].month == 12 and grp.index[-1].day >= 20),
        })
    return out


def series_payload(nav: pd.Series, bench: pd.Series, weekly: bool) -> dict:
    nav = nav.dropna()
    b = bench.reindex(nav.index).ffill()
    p = nav / nav.iloc[0] * 100
    bi = b / b.iloc[0] * 100
    if weekly:
        p_w = p.resample("W-FRI").last()
        b_w = bi.resample("W-FRI").last()
        p_w.index = [min(ix, p.index[-1]) for ix in p_w.index]
        b_w.index = p_w.index
        p, bi = p_w.dropna(), b_w.reindex(p_w.dropna().index)
    return {
        "datoer": [d.strftime("%Y-%m-%d") for d in p.index],
        "portefolje": [round(float(x), 2) for x in p],
        "indeks": [round(float(x), 2) for x in bi],
    }
