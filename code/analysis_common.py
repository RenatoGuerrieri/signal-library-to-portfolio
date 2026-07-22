from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd
import statsmodels.api as sm


ROOT = Path(__file__).resolve().parents[1]
SPEC_DIR = ROOT / "specification"
SOURCE_DIR = ROOT / "source_data"
INTERMEDIATE_DIR = ROOT / "intermediate"
RESULTS_DIR = ROOT / "results"
FIGURES_DIR = ROOT / "figures"
PUBLIC_DIR = ROOT
MANUSCRIPT_DIR = ROOT / "manuscript"
LOG_DIR = ROOT / "logs"

DATA_ROOT = Path(os.environ.get("SIGNAL_LIBRARY_DATA_ROOT", ROOT / "source_data"))
PANEL_PATH = Path(os.environ.get("SIGNAL_PANEL_PATH", DATA_ROOT / "openassetpricing_native_signal_panel.parquet"))
FEASIBILITY_PATH = Path(os.environ.get("SIGNAL_FEASIBILITY_PATH", DATA_ROOT / "openassetpricing_local_feasibility.parquet"))
INVENTORY_PATH = Path(os.environ.get("SIGNAL_INVENTORY_PATH", DATA_ROOT / "openassetpricing_signal_inventory.parquet"))
ARCHIVED_PRICE_PATH = Path(os.environ.get("SIGNAL_ARCHIVED_PRICE_PATH", DATA_ROOT / "openassetpricing_price_panel.parquet"))
UNIVERSE_PATH = Path(os.environ.get("SIGNAL_UNIVERSE_PATH", DATA_ROOT / "expanded_target_universe.parquet"))
PRICES_PROD = Path(os.environ.get("SIGNAL_PRICE_ROOT", DATA_ROOT / "prices_prod"))
PRICE_VALIDATION = Path(os.environ.get("SIGNAL_PRICE_VALIDATION", PRICES_PROD / "validation_report.json"))

SCORE_PANEL = INTERMEDIATE_DIR / "scores_broad.parquet"
REGISTRY_PARQUET = INTERMEDIATE_DIR / "signal_registry.parquet"
RETURN_LABELS = INTERMEDIATE_DIR / "return_labels.parquet"

GROUP_ORDER = ["Accounting", "Event", "Other", "Price", "Trading"]
HORIZONS = [21, 63, 126, 252]
RANDOM_SEED = 20260721


def ensure_directories() -> None:
    for directory in (
        SPEC_DIR,
        SOURCE_DIR,
        INTERMEDIATE_DIR,
        RESULTS_DIR,
        FIGURES_DIR,
        PUBLIC_DIR,
        MANUSCRIPT_DIR,
        LOG_DIR,
    ):
        directory.mkdir(parents=True, exist_ok=True)


def normalise_path(path: Path) -> str:
    return path.as_posix().replace("'", "''")


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".part")
    temporary.write_text(text, encoding="utf-8", newline="\n")
    temporary.replace(path)


def atomic_write_json(path: Path, payload: object) -> None:
    atomic_write_text(
        path,
        json.dumps(payload, indent=2, sort_keys=True, default=json_default) + "\n",
    )


def write_frame(frame: pd.DataFrame, path: Path, *, index: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".part")
    if path.suffix.lower() == ".parquet":
        frame.to_parquet(temporary, index=index, compression="zstd")
    elif path.suffix.lower() == ".csv":
        frame.to_csv(temporary, index=index, lineterminator="\n")
    else:
        raise ValueError(f"Unsupported frame output: {path}")
    temporary.replace(path)


def json_default(value: object) -> object:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (pd.Timestamp, np.datetime64)):
        return str(pd.Timestamp(value))
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return None if not np.isfinite(value) else float(value)
    if isinstance(value, np.ndarray):
        return value.tolist()
    raise TypeError(f"Cannot serialise {type(value)!r}")


def sha256_file(path: Path, chunk_size: int = 8 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            block = handle.read(chunk_size)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def security_hash(value: str) -> str:
    return hashlib.sha256(("gc-signal-library|" + str(value)).encode("utf-8")).hexdigest()[:20]


def bh_adjust(pvalues: Iterable[float]) -> np.ndarray:
    values = np.asarray(list(pvalues), dtype=float)
    adjusted = np.full(values.shape, np.nan, dtype=float)
    valid = np.flatnonzero(np.isfinite(values))
    if not len(valid):
        return adjusted
    order = valid[np.argsort(values[valid])]
    ranked = values[order] * len(order) / np.arange(1, len(order) + 1)
    ranked = np.minimum.accumulate(ranked[::-1])[::-1]
    adjusted[order] = np.minimum(ranked, 1.0)
    return adjusted


def newey_west_mean(values: Iterable[float], maxlags: int = 1) -> dict[str, float | int]:
    series = pd.Series(values, dtype=float).dropna()
    if len(series) < max(8, maxlags + 3):
        return {
            "n": int(len(series)),
            "mean": float(series.mean()) if len(series) else math.nan,
            "se": math.nan,
            "t": math.nan,
            "p": math.nan,
        }
    design = np.ones((len(series), 1), dtype=float)
    fit = sm.OLS(series.to_numpy(), design).fit(
        cov_type="HAC", cov_kwds={"maxlags": int(maxlags), "use_correction": True}
    )
    return {
        "n": int(len(series)),
        "mean": float(fit.params[0]),
        "se": float(fit.bse[0]),
        "t": float(fit.tvalues[0]),
        "p": float(fit.pvalues[0]),
    }


def effective_rank(correlation: np.ndarray) -> dict[str, float]:
    matrix = np.asarray(correlation, dtype=float)
    matrix = (matrix + matrix.T) / 2.0
    np.fill_diagonal(matrix, 1.0)
    eigenvalues = np.linalg.eigvalsh(matrix)
    minimum = float(eigenvalues.min())
    clipped = np.clip(eigenvalues, 0.0, None)
    total = float(clipped.sum())
    if total <= 0:
        return {
            "participation_ratio": math.nan,
            "entropy_rank": math.nan,
            "top_eigen_share": math.nan,
            "minimum_eigenvalue": minimum,
        }
    weights = clipped / total
    positive = weights[weights > 0]
    return {
        "participation_ratio": float(total * total / np.square(clipped).sum()),
        "entropy_rank": float(np.exp(-(positive * np.log(positive)).sum())),
        "top_eigen_share": float(clipped.max() / total),
        "minimum_eigenvalue": minimum,
    }


def circular_block_weights(
    observations: int, replications: int, block_length: int, seed: int
) -> np.ndarray:
    rng = np.random.default_rng(seed)
    weights = np.zeros((replications, observations), dtype=np.float32)
    blocks = int(math.ceil(observations / block_length))
    offsets = np.arange(block_length)
    for replication in range(replications):
        starts = rng.integers(0, observations, size=blocks)
        indices = ((starts[:, None] + offsets[None, :]) % observations).ravel()[:observations]
        weights[replication] = np.bincount(indices, minlength=observations) / observations
    return weights


def quantile_interval(values: Iterable[float]) -> tuple[float, float, float]:
    clean = np.asarray(list(values), dtype=float)
    clean = clean[np.isfinite(clean)]
    if not len(clean):
        return math.nan, math.nan, math.nan
    low, median, high = np.quantile(clean, [0.025, 0.5, 0.975])
    return float(low), float(median), float(high)


def rank_percentile(series: pd.Series) -> pd.Series:
    return series.rank(method="average", pct=True)


def one_way_replacement(current: set[str], previous: set[str] | None) -> float:
    if previous is None or not current:
        return math.nan
    return 1.0 - len(current.intersection(previous)) / len(current)


def cross_section_winsorise(series: pd.Series, lower: float = 0.005, upper: float = 0.995) -> pd.Series:
    clean = series.dropna()
    if len(clean) < 20:
        return series.copy()
    low, high = clean.quantile([lower, upper])
    return series.clip(lower=low, upper=high)


def component_count(correlation: np.ndarray, threshold: float) -> tuple[int, int, int]:
    n = correlation.shape[0]
    parent = list(range(n))

    def find(value: int) -> int:
        while parent[value] != value:
            parent[value] = parent[parent[value]]
            value = parent[value]
        return value

    def union(left: int, right: int) -> None:
        a, b = find(left), find(right)
        if a != b:
            parent[b] = a

    for left in range(n):
        for right in range(left + 1, n):
            value = correlation[left, right]
            if np.isfinite(value) and abs(value) >= threshold:
                union(left, right)
    sizes: dict[int, int] = {}
    for value in range(n):
        root = find(value)
        sizes[root] = sizes.get(root, 0) + 1
    component_sizes = list(sizes.values())
    return len(component_sizes), sum(size == 1 for size in component_sizes), max(component_sizes)


def variant_identifiers(registry: pd.DataFrame) -> dict[str, list[str]]:
    broad = registry[registry["broad_eligible"]].copy()
    return {
        "broad": broad["canonical_signal_id"].tolist(),
        "native": broad.loc[broad["native"], "canonical_signal_id"].tolist(),
        "broad_deduplicated": broad.loc[broad["dedupe_representative"], "canonical_signal_id"].tolist(),
        "native_deduplicated": broad.loc[
            broad["native"] & broad["dedupe_representative"], "canonical_signal_id"
        ].tolist(),
    }


def markdown_table(frame: pd.DataFrame) -> str:
    display = frame.copy()
    return display.to_markdown(index=False)


def environment_record() -> dict[str, object]:
    import duckdb
    import matplotlib
    import pyarrow
    import scipy
    import statsmodels

    return {
        "python": os.sys.version,
        "numpy": np.__version__,
        "pandas": pd.__version__,
        "duckdb": duckdb.__version__,
        "pyarrow": pyarrow.__version__,
        "scipy": scipy.__version__,
        "statsmodels": statsmodels.__version__,
        "matplotlib": matplotlib.__version__,
        "random_seed": RANDOM_SEED,
    }
