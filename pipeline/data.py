"""Henter markedsdata.

- Kurser og volum: Yahoo Finance via yfinance
- Antall utestående aksjer (for markedsvekter i Black–Litterman): Yahoo, mellomlagret i state/
- Risikofri rente: 3 mnd. NIBOR (effektiv) fra SSB tabell 10701
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import requests

log = logging.getLogger(__name__)


@dataclass
class MarketData:
    adj: pd.DataFrame        # utbytte- og splittjustert sluttkurs (for avkastning)
    close: pd.DataFrame      # sluttkurs uten utbyttejustering (for omsetning og markedsverdi)
    volume: pd.DataFrame     # antall aksjer omsatt
    bench: pd.Series         # referanseindeks (OSEBX GI)
    shares: pd.Series        # utestående aksjer (kan mangle for noen)
    rf_annual: pd.Series     # risikofri rente per handelsdag, årlig effektiv
    rf_source: str
    missing: list[str] = field(default_factory=list)  # tickere uten data


# ───────────────────────── Yahoo ─────────────────────────

def _download_chunk(tickers: list[str], start: str) -> pd.DataFrame:
    import yfinance as yf

    last_err = None
    for attempt in range(4):
        try:
            df = yf.download(
                tickers, start=start, auto_adjust=False, actions=False,
                progress=False, group_by="column", threads=True, timeout=30,
            )
            if df is not None and not df.empty:
                return df
        except Exception as e:  # yfinance kaster mange ulike feil
            last_err = e
        wait = 5 * (attempt + 1)
        log.warning("Yahoo-nedlasting feilet (forsøk %d), prøver igjen om %ds: %s", attempt + 1, wait, last_err)
        time.sleep(wait)
    raise RuntimeError(f"Klarte ikke å hente kurser fra Yahoo: {last_err}")


def _field(df: pd.DataFrame, name: str, tickers: list[str]) -> pd.DataFrame:
    if isinstance(df.columns, pd.MultiIndex):
        out = df[name]
    else:  # én ticker
        out = df[[name]].rename(columns={name: tickers[0]})
    return out.reindex(columns=tickers)


def download_prices(tickers: list[str], bench_ticker: str, start: str):
    frames = {"Adj Close": [], "Close": [], "Volume": []}
    chunk = 25
    for i in range(0, len(tickers), chunk):
        part = tickers[i:i + chunk]
        df = _download_chunk(part, start)
        for k in frames:
            frames[k].append(_field(df, k, part))
    adj, close, vol = (pd.concat(frames[k], axis=1) for k in ("Adj Close", "Close", "Volume"))

    bdf = _download_chunk([bench_ticker], start)
    bench = _field(bdf, "Close", [bench_ticker])[bench_ticker]
    return adj, close, vol, bench


def fetch_shares(tickers: list[str], cache_path: Path, max_age_days: int = 30) -> pd.Series:
    """Utestående aksjer fra Yahoo. Mellomlagres i en JSON-fil som oppdateres månedlig."""
    cache = {}
    if cache_path.exists():
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
    fresh = {}
    today = date.today()
    need = []
    for t in tickers:
        entry = cache.get(t)
        if entry and (today - date.fromisoformat(entry["dato"])).days <= max_age_days:
            fresh[t] = entry
        else:
            need.append(t)

    if need:
        import yfinance as yf
        for t in need:
            shares = None
            try:
                fi = yf.Ticker(t).fast_info
                shares = fi.get("shares") if hasattr(fi, "get") else getattr(fi, "shares", None)
            except Exception as e:
                log.info("Fant ikke antall aksjer for %s: %s", t, e)
            if shares and np.isfinite(shares) and shares > 0:
                fresh[t] = {"aksjer": float(shares), "dato": today.isoformat()}
            elif t in cache:  # behold gammel verdi fremfor ingenting
                fresh[t] = cache[t]
            time.sleep(0.2)
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(fresh, indent=1, ensure_ascii=False, sort_keys=True), encoding="utf-8")

    return pd.Series({t: v["aksjer"] for t, v in fresh.items()}, dtype=float)


# ───────────────────────── Rensing ─────────────────────────

def clean_prices(adj, close, vol, bench):
    """Justerer alle serier til OSEBX sine handelsdager og fjerner åpenbare datafeil."""
    bench = bench.dropna()
    bench = bench[~bench.index.duplicated()]
    idx = bench.index

    def align(df):
        df = df[~df.index.duplicated()]
        return df.reindex(idx)

    adj, close, vol = align(adj), align(close), align(vol)

    # Siste rad kan være ufullstendig dersom Yahoo ikke har oppdatert alle aksjer ennå
    if len(adj) > 1 and adj.iloc[-1].notna().mean() < 0.5 * adj.iloc[-2].notna().mean():
        adj, close, vol, bench = adj.iloc[:-1], close.iloc[:-1], vol.iloc[:-1], bench.iloc[:-1]

    # Fjern enkeltstående kurs-«spikes» (feil hos Yahoo): stor bevegelse som reverseres dagen etter
    r = adj.pct_change(fill_method=None)
    up_down = (r > 0.5) & (r.shift(-1) < -0.33)
    down_up = (r < -0.4) & (r.shift(-1) > 0.66)
    bad = up_down | down_up
    if bad.to_numpy().any():
        log.warning("Fjerner %d mistenkelige kurspunkter", int(bad.to_numpy().sum()))
        adj = adj.mask(bad)
        close = close.mask(bad)

    # Fyll korte hull (maks 5 dager) – lengre hull betyr at aksjen ikke handlet
    adj = adj.ffill(limit=5)
    close = close.ffill(limit=5)
    vol = vol.fillna(0)

    missing = [c for c in adj.columns if adj[c].notna().sum() == 0]
    keep = [c for c in adj.columns if c not in missing]
    return adj[keep], close[keep], vol[keep], bench, missing


# ───────────────────────── Risikofri rente ─────────────────────────

_MONTHS = ["januar", "februar", "mars", "april", "mai", "juni", "juli",
           "august", "september", "oktober", "november", "desember"]
SSB_BASE = "https://data.ssb.no/api/pxwebapi/v2/tables/10701"


def _jsonstat_to_frame(js: dict) -> pd.DataFrame:
    ids, sizes = js["id"], js["size"]
    cats = []
    for d in ids:
        idx = js["dimension"][d]["category"]["index"]
        codes = sorted(idx, key=idx.get) if isinstance(idx, dict) else list(idx)
        cats.append(codes)
    values = js["value"]
    if isinstance(values, dict):  # sparse format
        values = [values.get(str(i)) for i in range(int(np.prod(sizes)))]
    rows = []
    for flat, v in enumerate(values):
        pos = np.unravel_index(flat, sizes)
        rows.append([cats[k][p] for k, p in enumerate(pos)] + [v])
    return pd.DataFrame(rows, columns=ids + ["verdi"])


def fetch_nibor_ssb() -> pd.Series:
    """Månedlig 3 mnd. NIBOR (effektiv, prosent) fra SSB. Returnerer desimal per månedsstart."""
    meta = requests.get(f"{SSB_BASE}/metadata", params={"lang": "no", "outputFormat": "json-stat2"}, timeout=30)
    meta.raise_for_status()
    m = meta.json()
    params = {"lang": "no", "outputFormat": "json-stat2"}
    nibor_dim = None
    for d in m["id"]:
        labels = m["dimension"][d]["category"].get("label", {})
        hit = [code for code, lab in labels.items() if "nibor" in str(lab).lower()]
        if hit and nibor_dim is None:
            nibor_dim = d
            params[f"valueCodes[{d}]"] = hit[0]
        elif d.lower() in ("tid", "time"):
            params[f"valueCodes[{d}]"] = "*"
        else:
            codes = list(labels) or ["*"]
            params[f"valueCodes[{d}]"] = codes[0]
    if nibor_dim is None:
        raise RuntimeError("Fant ikke NIBOR i SSB-tabell 10701")

    resp = requests.get(f"{SSB_BASE}/data", params=params, timeout=30)
    resp.raise_for_status()
    df = _jsonstat_to_frame(resp.json())
    time_col = next(c for c in df.columns if c.lower() in ("tid", "time"))
    df = df.dropna(subset=["verdi"])
    df["dato"] = pd.PeriodIndex(df[time_col].str.replace("M", "-"), freq="M").to_timestamp()
    s = df.set_index("dato")["verdi"].astype(float).sort_index() / 100.0
    if s.empty:
        raise RuntimeError("SSB returnerte ingen NIBOR-verdier")
    return s


def build_rf(dates: pd.DatetimeIndex, cfg_rf: dict) -> tuple[pd.Series, str]:
    fixed = float(cfg_rf["fast_rente"])
    if cfg_rf.get("kilde", "ssb") == "ssb":
        try:
            monthly = fetch_nibor_ssb()
            # månedssnittet er først kjent når måneden er over → brukes fra neste måned (ingen tjuvkikk)
            known = monthly.copy()
            known.index = known.index + pd.offsets.MonthBegin(1)
            daily = known.reindex(known.index.union(dates)).ffill().reindex(dates)
            daily = daily.fillna(fixed)  # før serien starter
            last = monthly.index[-1]
            return daily, f"3 mnd. NIBOR fra SSB, {_MONTHS[last.month - 1]} {last.year}"
        except Exception as e:
            log.warning("Kunne ikke hente NIBOR fra SSB (%s). Bruker fast rente %.2f %%.", e, fixed * 100)
            return pd.Series(fixed, index=dates), "fast rente fra config.yaml fordi SSB ikke svarte"
    return pd.Series(fixed, index=dates), "fast rente fra config.yaml"


# ───────────────────────── Samlet ─────────────────────────

def load_market_data(cfg, state_dir: Path) -> MarketData:
    tickers = cfg.tickers
    bench_t = cfg["referanseindeks"]["ticker"]
    log.info("Laster ned kurser for %d kandidater + %s", len(tickers), bench_t)
    adj, close, vol, bench = download_prices(tickers, bench_t, cfg["data"]["startdato"])
    adj, close, vol, bench, missing = clean_prices(adj, close, vol, bench)
    if missing:
        log.warning("Ingen data fra Yahoo for: %s", ", ".join(missing))
    if len(adj.columns) < 10:
        raise RuntimeError("For få aksjer med data – avbryter så forrige versjon av siden blir stående.")
    stale = (pd.Timestamp.today().normalize() - bench.index[-1]).days
    if stale > 10:
        raise RuntimeError(f"Siste kurs er {stale} dager gammel – Yahoo ser ut til å ha problemer.")

    shares = fetch_shares(list(adj.columns), state_dir / "utestaende_aksjer.json")
    rf, rf_src = build_rf(adj.index, cfg["risikofri_rente"])
    return MarketData(adj, close, vol, bench, shares, rf, rf_src, missing)


# ───────────────────────── Demo-data (uten nett) ─────────────────────────

def synthetic_market_data(cfg, seed: int = 7) -> MarketData:
    """Syntetiske data med sektorfaktorer, brukt til å teste hele løypa lokalt."""
    rng = np.random.default_rng(seed)
    tickers = cfg.tickers
    sectors = cfg.candidates["sektor"]
    dates = pd.bdate_range(cfg["data"]["startdato"], pd.Timestamp.today().normalize() - pd.Timedelta(days=1))
    n, T = len(tickers), len(dates)

    mkt = rng.normal(0.0004, 0.010, T)
    sec_names = sorted(sectors.unique())
    sec_f = {s: rng.normal(0, 0.007, T) for s in sec_names}
    betas = rng.uniform(0.6, 1.4, n)
    alphas = rng.normal(0.0001, 0.00025, n)
    idio = rng.uniform(0.008, 0.025, n)
    rets = np.empty((T, n))
    for j, t in enumerate(tickers):
        rets[:, j] = alphas[j] + betas[j] * mkt + sec_f[sectors[t]] + rng.normal(0, idio[j], T)
    adj = pd.DataFrame(100 * np.exp(np.cumsum(np.log1p(rets), axis=0)), index=dates, columns=tickers)
    # noen aksjer noteres sent (IPO) og én har ingen data
    for j in rng.choice(n, 8, replace=False):
        adj.iloc[: rng.integers(300, 3000), j] = np.nan
    adj.iloc[:, -1] = np.nan
    close = adj * 0.9
    size = np.exp(rng.normal(0, 1.3, n))
    vol = pd.DataFrame(rng.lognormal(0, 0.5, (T, n)) * size * 1e5, index=dates, columns=tickers)
    vol = vol.where(adj.notna(), 0)
    bench_r = mkt + 0.00005
    bench = pd.Series(1000 * np.exp(np.cumsum(np.log1p(bench_r))), index=dates)
    adj, close, vol, bench, missing = clean_prices(adj, close, vol, bench)
    shares = pd.Series(size * 1e8, index=tickers).reindex(adj.columns)
    rf = pd.Series(0.03, index=adj.index)
    return MarketData(adj, close, vol, bench, shares, rf, "demodata", missing)
