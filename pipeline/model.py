"""Univers og estimering av forventet avkastning og kovarians.

Alt beregnes kun med data til og med beslutningsdatoen, slik at backtesten
ikke «ser inn i fremtiden».
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
from sklearn.covariance import LedoitWolf

WEEKS = 52


@dataclass
class Universe:
    tickers: list[str]
    adv_mnok: pd.Series      # median daglig omsetning (MNOK) for de godkjente
    caps: pd.Series          # maks vekt per aksje (min av maks_vekt og likviditetsgrense)
    excluded_liq: pd.Series  # ADV for aksjer som ble for illikvide


@dataclass
class Estimate:
    tickers: list[str]
    mu_excess: np.ndarray    # forventet årlig meravkastning over risikofri rente
    cov: np.ndarray          # årlig kovarians
    market_w: np.ndarray     # markedsvekter i universet (brukt i BL, og som referansepunkt)
    info: dict


def select_universe(md, cfg, asof: pd.Timestamp) -> Universe:
    u = cfg["univers"]
    e = cfg["estimering"]
    win = int(u["likviditetsvindu_dager"])
    hist = md.adj.loc[:asof]
    if len(hist) < win:
        return Universe([], pd.Series(dtype=float), pd.Series(dtype=float), pd.Series(dtype=float))

    close = md.close.loc[:asof].iloc[-win:]
    vol = md.volume.loc[:asof].iloc[-win:]
    turnover = (close * vol).fillna(0)
    adv = turnover.median() / 1e6
    traded = (vol > 0).mean()

    weekly = hist.resample("W-FRI").last().iloc[-(int(e["vindu_uker"]) + 1):]
    if len(weekly) > int(e["vindu_uker"]):
        coverage = weekly.notna().mean()
    else:  # for kort historikk totalt
        coverage = pd.Series(0.0, index=hist.columns)
    has_price_now = hist.iloc[-1].notna()

    liquid = (adv >= u["min_median_omsetning_mnok"]) & (traded >= u["min_andel_handelsdager"])
    ok = liquid & (coverage >= e["min_dekning"]) & has_price_now
    tickers = [t for t in hist.columns if ok.get(t, False)]

    o = cfg["optimering"]
    liq_cap = u["deltakelsesgrad"] * u["maks_dager_aa_selge"] * adv * 1e6 / u["portefoljestorrelse_nok"]
    caps = np.minimum(liq_cap, o["maks_vekt"]).reindex(tickers)
    excluded = adv[(~liquid) & (coverage >= e["min_dekning"])].sort_values(ascending=False)
    return Universe(tickers, adv.reindex(tickers), caps, excluded)


def _weekly_returns(md, tickers, asof, weeks):
    px = md.adj.loc[:asof, tickers].resample("W-FRI").last().iloc[-(weeks + 1):]
    px = px.ffill(limit=2)
    r = px.pct_change(fill_method=None).iloc[1:]
    return r.dropna(axis=0, how="any")


def _market_weights(md, tickers, asof, adv: pd.Series) -> tuple[np.ndarray, str]:
    price = md.close.loc[:asof, tickers].iloc[-1]
    shares = md.shares.reindex(tickers)
    mcap = price * shares
    if mcap.notna().mean() >= 0.8:
        # manglende markedsverdi erstattes med median av (markedsverdi/omsetning) × omsetning
        ratio = (mcap / adv).median()
        mcap = mcap.fillna(adv * ratio)
        src = "markedsverdi"
    else:
        mcap = adv.copy()
        src = "omsetning (markedsverdi manglet)"
    w = mcap.clip(lower=0).to_numpy(dtype=float)
    return w / w.sum(), src


def estimate(md, uni: Universe, cfg, asof: pd.Timestamp, method: str) -> Estimate | None:
    e = cfg["estimering"]
    tickers = uni.tickers
    if len(tickers) < 5:
        return None
    R = _weekly_returns(md, tickers, asof, int(e["vindu_uker"]))
    if len(R) < 52:
        return None
    rf = float(md.rf_annual.loc[:asof].iloc[-1])

    lw = LedoitWolf().fit(R.to_numpy())
    S_w = lw.covariance_                       # ukentlig
    S = S_w * WEEKS
    mu_hat_w = R.mean().to_numpy()             # ukentlig aritmetisk snitt
    T, N = R.shape
    w_mkt, mkt_src = _market_weights(md, tickers, asof, uni.adv_mnok.reindex(tickers))
    info = {"observasjoner": int(T), "lw_shrinkage": float(lw.shrinkage_), "markedsvekter": mkt_src}

    if method == "bayes_stein":
        # Jorion (1986): krymper snittene mot avkastningen til minimum-varians-porteføljen
        Sinv = np.linalg.inv(S_w)
        ones = np.ones(N)
        mu0 = ones @ Sinv @ mu_hat_w / (ones @ Sinv @ ones)
        d = mu_hat_w - mu0
        phi = (N + 2) / ((N + 2) + T * d @ Sinv @ d)
        phi = float(np.clip(phi, 0, 1))
        mu = ((1 - phi) * mu_hat_w + phi * mu0) * WEEKS
        mu_excess = mu - rf
        info["krympingsgrad"] = phi
    elif method == "black_litterman":
        bl = e["black_litterman"]
        tau, delta = float(bl["tau"]), float(bl["risikoaversjon"])
        pi = delta * S @ w_mkt                           # likevekts-meravkastning
        Q = mu_hat_w * WEEKS - rf                        # «synspunkt»: historisk meravkastning
        years = T / WEEKS
        Omega = np.diag(np.diag(S) / years)              # usikkerhet = standardfeil i snittet
        tS = tau * S
        mu_excess = pi + tS @ np.linalg.solve(tS + Omega, Q - pi)
        info["tau"] = tau
        info["risikoaversjon"] = delta
    else:
        raise ValueError(method)

    return Estimate(tickers, np.asarray(mu_excess, float), np.asarray(S, float), w_mkt, info)
