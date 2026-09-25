"""Leser config.yaml og kandidatlisten."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Config:
    raw: dict
    candidates: pd.DataFrame  # index: ticker, kolonner: navn, sektor

    def __getitem__(self, key):
        return self.raw[key]

    @property
    def tickers(self) -> list[str]:
        return list(self.candidates.index)


def load_config(path: Path | None = None) -> Config:
    path = path or ROOT / "config.yaml"
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    cand_path = ROOT / raw["univers"]["kandidatfil"]
    cand = pd.read_csv(cand_path, dtype=str).dropna(subset=["ticker"])
    cand["ticker"] = cand["ticker"].str.strip()
    cand["navn"] = cand["navn"].str.strip()
    cand["sektor"] = cand["sektor"].fillna("Ukjent").str.strip()
    cand = cand.drop_duplicates("ticker").set_index("ticker")

    _validate(raw)
    return Config(raw=raw, candidates=cand)


def _validate(raw: dict) -> None:
    o = raw["optimering"]
    if o["maks_vekt"] * o["maks_antall"] < 1:
        raise ValueError("maks_vekt × maks_antall må være minst 1, ellers kan porteføljen ikke bli fullinvestert.")
    if o["min_antall"] > o["maks_antall"]:
        raise ValueError("min_antall kan ikke være større enn maks_antall.")
    if raw["estimering"]["hovedmetode"] not in ("black_litterman", "bayes_stein"):
        raise ValueError("hovedmetode må være 'black_litterman' eller 'bayes_stein'.")
