"""Markowitz-optimering.

Maks Sharpe løses som et konvekst kvadratisk problem (Cornuéjols–Tütüncü-transformasjonen):
    min  yᵀΣy   gitt  μᵉᵀy = 1,  y ≥ 0,  κ = Σy
og vektene blir w = y / κ. Alle lineære grenser skaleres med κ.

Grensen på antall selskaper (maks 15) er ikke konveks, så den håndteres
iterativt: løs, behold de største posisjonene, løs på nytt.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

import cvxpy as cp
import numpy as np

log = logging.getLogger(__name__)
EPS = 1e-6


@dataclass
class Result:
    weights: np.ndarray
    mode: str                          # "maks_sharpe" eller "min_varians"
    turnover_limit_used: float | None
    notes: list[str] = field(default_factory=list)


def _solve(mu, cov, caps, sectors, max_sector, active, prev, max_turn, mode, outside=0.0):
    """Løser på delmengden `active`. Returnerer fullt vektvektor eller None om uløselig."""
    idx = np.flatnonzero(active)
    n = len(idx)
    if n == 0 or caps[idx].sum() < 1 - 1e-9:
        return None
    m, C, cp_caps = mu[idx], cov[np.ix_(idx, idx)], caps[idx]
    sec = sectors[idx]
    y = cp.Variable(n, nonneg=True)

    if mode == "maks_sharpe":
        k = cp.Variable(nonneg=True)
        cons = [m @ y == 1, cp.sum(y) == k, y <= cp.multiply(cp_caps, k)]
        scale = k
    else:
        cons = [cp.sum(y) == 1, y <= cp_caps]
        scale = 1.0
    for s in np.unique(sec):
        cons.append(cp.sum(y[sec == s]) <= max_sector * scale)
    if prev is not None and max_turn is not None:
        prev_in = prev[idx]
        prev_out = float(prev.sum() - prev_in.sum()) + outside   # posisjoner som må selges helt
        cons.append(cp.norm1(y - prev_in * scale) + prev_out * scale <= 2 * max_turn * scale)

    prob = cp.Problem(cp.Minimize(cp.quad_form(y, cp.psd_wrap(C))), cons)
    for solver in ("CLARABEL", "OSQP", "SCS"):
        try:
            prob.solve(solver=solver)
            if prob.status in ("optimal", "optimal_inaccurate"):
                break
        except Exception:
            continue
    if prob.status not in ("optimal", "optimal_inaccurate") or y.value is None:
        return None
    yv = np.maximum(y.value, 0)
    if yv.sum() <= 0:
        return None
    w = np.zeros(len(mu))
    w[idx] = yv / yv.sum()
    return w


def optimize(mu, cov, caps, sectors, opt_cfg, prev=None, outside=0.0) -> Result | None:
    """Maks Sharpe med vektgrenser, sektorgrenser, omsetningsgrense og 5–15 selskaper."""
    mu, cov, caps, sectors = map(np.asarray, (mu, cov, caps, sectors))
    n = len(mu)
    max_sector = float(opt_cfg["maks_sektorvekt"])
    min_n, max_n = int(opt_cfg["min_antall"]), int(opt_cfg["maks_antall"])
    min_w = float(opt_cfg["min_vekt"])
    mode = "maks_sharpe" if (mu > 0).any() else "min_varians"
    notes = [] if mode == "maks_sharpe" else ["Ingen aksjer har forventet meravkastning > 0 – bruker minimum varians."]
    if prev is not None and prev.sum() + outside <= 0:
        prev = None

    base_turn = float(opt_cfg["maks_omsetning"]) if prev is not None else None
    turn_levels = [base_turn] if base_turn is None else [base_turn, base_turn * 1.5, base_turn * 2, None]

    last_w = None
    for turn in turn_levels:
        active = np.ones(n, bool)
        w = _solve(mu, cov, caps, sectors, max_sector, active, prev, turn, mode, outside)
        if w is None:
            continue
        full_rank = list(np.argsort(-w, kind="stable"))
        ranking = full_rank
        ok = False
        for _ in range(25):
            held = np.flatnonzero(w > EPS)
            keep = [i for i in ranking if w[i] >= min_w - 1e-9][:max_n]
            if len(keep) < min_n:
                keep = [i for i in ranking if w[i] > EPS][:max(min_n, len(keep))]
            if len(held) <= max_n and all(w[i] >= min_w - 1e-9 for i in held):
                ok = True
                break
            new_active = np.zeros(n, bool)
            new_active[keep] = True
            w_new = _solve(mu, cov, caps, sectors, max_sector, new_active, prev, turn, mode, outside)
            # hvis uløselig: legg til neste kandidat fra rangeringen til det går
            extra = [i for i in full_rank if not new_active[i]]
            while w_new is None and extra and new_active.sum() < max_n:
                new_active[extra.pop(0)] = True
                w_new = _solve(mu, cov, caps, sectors, max_sector, new_active, prev, turn, mode, outside)
            if w_new is None:
                break
            w = w_new
            ranking = list(np.argsort(-w, kind="stable"))
        if ok:
            if turn != base_turn:
                notes.append(
                    "Omsetningsgrensen måtte lempes" + (f" til {turn * 100:.0f} %" if turn else " helt")
                    + " for å oppfylle de andre kravene."
                )
            w[w < EPS] = 0
            return Result(w / w.sum(), mode, turn, notes)
        last_w = w

    # Nødløsning: klarte ikke å oppfylle alt samtidig. Behold de største og tilpass grensene.
    if last_w is None:
        return None
    w = _force_limits(last_w, caps, min_n, max_n, min_w)
    notes.append("Optimeringen fant ingen løsning som oppfylte alle krav; vektene er tilpasset i etterkant.")
    return Result(w, mode, None, notes)


def _force_limits(w, caps, min_n, max_n, min_w):
    order = np.argsort(-w)
    keep = [i for i in order if w[i] >= min_w][:max_n]
    if len(keep) < min_n:
        keep = list(order[:min_n])
    out = np.zeros_like(w)
    out[keep] = np.maximum(w[keep], min_w)
    for _ in range(50):  # vannfylling mot vektgrensene
        out /= out.sum()
        over = out > caps + 1e-12
        if not over.any():
            break
        excess = (out[over] - caps[over]).sum()
        out[over] = caps[over]
        free = (out > 0) & ~over
        out[free] += excess * out[free] / out[free].sum()
    return out / out.sum()


# ───────────────────────── Effisient front ─────────────────────────

def efficient_frontier(mu, cov, caps, sectors, max_sector, points=40):
    """Minimum varians for en rekke målavkastninger (kun vekt- og sektorgrenser)."""
    mu, cov, caps, sectors = map(np.asarray, (mu, cov, caps, sectors))
    n = len(mu)
    w = cp.Variable(n, nonneg=True)
    base = [cp.sum(w) == 1, w <= caps]
    for s in np.unique(sectors):
        base.append(cp.sum(w[sectors == s]) <= max_sector)
    C = cp.psd_wrap(cov)

    cp.Problem(cp.Minimize(cp.quad_form(w, C)), base).solve(solver="CLARABEL")
    if w.value is None:
        return []
    r_min = float(mu @ w.value)
    cp.Problem(cp.Maximize(mu @ w), base).solve(solver="CLARABEL")
    r_max = float(mu @ w.value)

    target = cp.Parameter()
    prob = cp.Problem(cp.Minimize(cp.quad_form(w, C)), base + [mu @ w >= target])
    curve = []
    for t in np.linspace(r_min, r_max, points):
        target.value = t
        try:
            prob.solve(solver="CLARABEL")
        except Exception:
            continue
        if w.value is not None and prob.status in ("optimal", "optimal_inaccurate"):
            wv = np.maximum(w.value, 0)
            curve.append((float(np.sqrt(wv @ cov @ wv)), float(mu @ wv)))
    return curve


def tangency(mu, cov, caps, sectors, max_sector):
    """Maks Sharpe uten antalls- og omsetningsgrense (tangentpunktet på fronten)."""
    mu, caps, sectors = map(np.asarray, (mu, caps, sectors))
    if not (mu > 0).any():
        return None
    return _solve(mu, np.asarray(cov), caps, sectors, max_sector, np.ones(len(mu), bool), None, None, "maks_sharpe")
