"""Kjører hele løypa: data → live-rebalansering → backtest → site/data/resultat.json

Bruk:
    python -m pipeline.run          # ekte data fra Yahoo og SSB
    python -m pipeline.run --demo   # syntetiske data, ingen nett (for lokal testing)
"""
from __future__ import annotations

import argparse
import json
import logging
import time
import warnings
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from .config import ROOT, load_config
from .data import load_market_data, synthetic_market_data
from .engine import daily_returns, metrics, run_backtest, series_payload, yearly_returns
from .live import METHODS, load_state, replay, save_state, update_live
from .model import estimate, select_universe
from .optimizer import efficient_frontier, tangency

log = logging.getLogger("obx")
MONTHS = ["januar", "februar", "mars", "april", "mai", "juni", "juli",
          "august", "september", "oktober", "november", "desember"]
METHOD_NAMES = {"black_litterman": "Black–Litterman", "bayes_stein": "Bayes–Stein"}


def r4(x):
    return None if x is None or not np.isfinite(x) else round(float(x), 4)


def liquidity_now(md, cfg):
    win = int(cfg["univers"]["likviditetsvindu_dager"])
    t = (md.close.iloc[-win:] * md.volume.iloc[-win:]).fillna(0)
    return t.median() / 1e6


def method_report(md, cfg, method, state, rets, bt_port, bt_log, adv_all):
    latest = rets.index[-1]
    rf_now = float(md.rf_annual.iloc[-1])
    names = cfg.candidates["navn"]
    sectors = cfg.candidates["sektor"]
    opt = cfg["optimering"]
    u = cfg["univers"]

    uni = select_universe(md, cfg, latest)
    est = estimate(md, uni, cfg, latest, method)
    entries = state["metoder"][method]
    cost = float(opt["transaksjonskostnad"])
    live = replay(entries, rets, cost)
    last = entries[-1] if entries else None

    # nåværende vekter: drevet beholdning, eller målvekter hvis handelen skjer i morgen
    target = pd.Series(last["vekter"]) if last else pd.Series(dtype=float)
    now_w = live.weights()
    pending = live.pending is not None
    if now_w is None or pending:
        now_w = target.copy()
    now_w = now_w[now_w > 1e-6]

    # ── effisient front med dagens estimater ──
    front = {}
    if est is not None:
        caps = uni.caps.reindex(est.tickers).to_numpy(float)
        sec = sectors.reindex(est.tickers).fillna("Ukjent").to_numpy()
        curve = efficient_frontier(est.mu_excess, est.cov, caps, sec, opt["maks_sektorvekt"],
                                   int(cfg["effisient_front"]["punkter"]))
        tw = tangency(est.mu_excess, est.cov, caps, sec, opt["maks_sektorvekt"])
        vols = np.sqrt(np.diag(est.cov))

        def point(w):
            return {"vol": r4(np.sqrt(w @ est.cov @ w)), "avk": r4(w @ est.mu_excess + rf_now)}

        pw = now_w.reindex(est.tickers).fillna(0).to_numpy()
        front = {
            "kurve": [{"vol": r4(v), "avk": r4(r + rf_now)} for v, r in curve],
            "tangent": point(tw) if tw is not None else None,
            "portefolje": point(pw / pw.sum()) if pw.sum() > 0.5 else None,
            "marked": point(est.market_w),
            "aksjer": [
                {"ticker": t, "navn": names.get(t, t), "vol": r4(vols[i]), "avk": r4(est.mu_excess[i] + rf_now),
                 "i_portefolje": bool(t in now_w.index)}
                for i, t in enumerate(est.tickers)
            ],
            "rf": r4(rf_now),
        }
        exp = {t: est.mu_excess[i] + rf_now for i, t in enumerate(est.tickers)}
        vol_map = {t: vols[i] for i, t in enumerate(est.tickers)}
        if front["portefolje"]:
            p = front["portefolje"]
            front["portefolje"]["sharpe"] = r4((p["avk"] - rf_now) / p["vol"]) if p["vol"] else None
    else:
        exp, vol_map = {}, {}

    # ── beholdning ──
    holdings = []
    pv = float(u["portefoljestorrelse_nok"])
    for t in sorted(set(target.index) | set(now_w.index), key=lambda x: -target.get(x, 0)):
        adv = float(adv_all.get(t, np.nan))
        wn = float(now_w.get(t, 0))
        days = wn * pv / (u["deltakelsesgrad"] * adv * 1e6) if adv and adv > 0 else None
        holdings.append({
            "ticker": t, "navn": names.get(t, t), "sektor": sectors.get(t, "Ukjent"),
            "malvekt": r4(target.get(t, 0)), "navekt": r4(wn),
            "forventet_avk": r4(exp.get(t, np.nan)), "vol": r4(vol_map.get(t, np.nan)),
            "omsetning_mnok": r4(adv), "dager_aa_selge": r4(days) if days is not None else None,
            "i_univers": t in uni.tickers,
        })
    sec_w = now_w.groupby(sectors.reindex(now_w.index).fillna("Ukjent")).sum().sort_values(ascending=False)

    changes = []
    if last:
        before = pd.Series(last.get("vekter_for", {}), dtype=float)
        allt = target.index.union(before.index)
        for t in allt:
            a, b = float(before.get(t, 0)), float(target.get(t, 0))
            if abs(b - a) >= 0.005:
                changes.append({"ticker": t, "navn": names.get(t, t), "fra": r4(a), "til": r4(b)})
        changes.sort(key=lambda c: -(c["til"] - c["fra"]))

    # ── historikk ──
    bt_nav = bt_port.nav_series()
    live_nav = live.nav_series()
    bt = {
        "serie": series_payload(bt_nav, md.bench, weekly=True) if len(bt_nav) else None,
        "nokkeltall": metrics(bt_nav, md.bench, md.rf_annual),
        "arlig": yearly_returns(bt_nav, md.bench) if len(bt_nav) else [],
        "rebalanseringer": bt_log,
        "snitt_omsetning": r4(np.mean([x["omsetning"] for x in bt_log[1:]])) if len(bt_log) > 1 else None,
    }
    lv = {
        "start": entries[0]["beslutningsdato"] if entries else None,
        "serie": series_payload(live_nav, md.bench, weekly=False) if len(live_nav) >= 2 else None,
        "nokkeltall": metrics(live_nav, md.bench, md.rf_annual),
        "rebalanseringer": [
            {"dato": e["beslutningsdato"], "antall": len(e["vekter"]), "omsetning": e.get("omsetning"),
             "merknader": e.get("merknader", [])}
            for e in entries
        ],
    }

    return {
        "navn": METHOD_NAMES[method],
        "portefolje": {
            "beslutningsdato": last["beslutningsdato"] if last else None,
            "handel_venter": pending,
            "forste_kjop": bool(last) and not last.get("vekter_for"),
            "antall": int((target > 0).sum()),
            "omsetning": last.get("omsetning") if last else None,
            "merknader": last.get("merknader", []) if last else [],
            "beholdning": holdings,
            "sektorer": [{"sektor": s, "vekt": r4(v)} for s, v in sec_w.items()],
            "endringer": changes,
        },
        "front": front,
        "estimering": est.info if est is not None else {},
        "backtest": bt,
        "live": lv,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true", help="syntetiske data uten nett")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("cvxpy").setLevel(logging.WARNING)
    warnings.filterwarnings("ignore", message="Solution may be inaccurate")
    t0 = time.time()

    cfg = load_config()
    state_dir = ROOT / ("build/demo_state" if args.demo else "state")
    md = synthetic_market_data(cfg) if args.demo else load_market_data(cfg, state_dir)
    rets = daily_returns(md)
    latest = rets.index[-1]
    log.info("Siste kursdato: %s. Risikofri rente: %s", latest.date(), md.rf_source)

    state_path = state_dir / "live.json"
    state = load_state(state_path)
    update_live(md, cfg, state, rets)
    save_state(state_path, state)

    adv_all = liquidity_now(md, cfg)
    uni = select_universe(md, cfg, latest)
    methods = {}
    for m in METHODS:
        log.info("Backtest %s …", m)
        port, bt_log = run_backtest(md, cfg, m, rets)
        methods[m] = method_report(md, cfg, m, state, rets, port, bt_log, adv_all)

    nxt = (latest + pd.offsets.MonthBegin(1))
    names = cfg.candidates["navn"]
    out = {
        "generert": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "siste_kursdato": latest.strftime("%Y-%m-%d"),
        "neste_rebalansering": f"Første handelsdag i {MONTHS[nxt.month - 1]} {nxt.year}",
        "demo": bool(args.demo),
        "hovedmetode": cfg["estimering"]["hovedmetode"],
        "indeks": cfg["referanseindeks"]["navn"],
        "risikofri": {"rente": r4(float(md.rf_annual.iloc[-1])), "kilde": md.rf_source},
        "univers": {
            "kandidater": len(cfg.tickers),
            "med_data": int(md.adj.shape[1]),
            "godkjent": len(uni.tickers),
            "mangler_data": [{"ticker": t, "navn": names.get(t, t)} for t in md.missing],
            "for_illikvide": [
                {"ticker": t, "navn": names.get(t, t), "omsetning_mnok": r4(v)} for t, v in uni.excluded_liq.items()
            ],
        },
        "innstillinger": {
            "maks_vekt": cfg["optimering"]["maks_vekt"],
            "maks_sektorvekt": cfg["optimering"]["maks_sektorvekt"],
            "min_antall": cfg["optimering"]["min_antall"],
            "maks_antall": cfg["optimering"]["maks_antall"],
            "min_vekt": cfg["optimering"]["min_vekt"],
            "maks_omsetning": cfg["optimering"]["maks_omsetning"],
            "transaksjonskostnad": cfg["optimering"]["transaksjonskostnad"],
            "min_omsetning_mnok": cfg["univers"]["min_median_omsetning_mnok"],
            "vindu_uker": cfg["estimering"]["vindu_uker"],
            "backtest_start": cfg["backtest"]["startdato"],
            "portefoljestorrelse_nok": cfg["univers"]["portefoljestorrelse_nok"],
            "deltakelsesgrad": cfg["univers"]["deltakelsesgrad"],
        },
        "metoder": methods,
    }
    out_path = ROOT / "site" / "data" / "resultat.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":"), allow_nan=False), encoding="utf-8")
    log.info("Ferdig på %.0f s. Skrev %s (%.0f kB)", time.time() - t0, out_path, out_path.stat().st_size / 1024)


if __name__ == "__main__":
    main()
