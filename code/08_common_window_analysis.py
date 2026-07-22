from __future__ import annotations

import hashlib
import math
import multiprocessing as mp
import os
from concurrent.futures import ProcessPoolExecutor

import numpy as np
import pandas as pd

from analysis_common import RANDOM_SEED, RESULTS_DIR, newey_west_mean, write_frame


ALLOCATIONS = [
    "equal_signal",
    "equal_group",
    "inverse_redundancy",
    "trailing_positive_evidence",
]
METRICS = [
    "ic",
    "spread",
    "spread_winsorised",
    "net_spread_10bps",
    "net_spread_25bps",
    "net_spread_50bps",
    "net_spread_100bps",
]
SAMPLES = [
    ("gbp_fixed", "broad_deduplicated"),
    ("us_domestic_fixed", "us_deduplicated"),
]


def stable_seed(*parts: object) -> int:
    digest = hashlib.sha256("|".join(str(part) for part in parts).encode("utf-8")).digest()
    return RANDOM_SEED + int.from_bytes(digest[:4], "little")


def circular_indices(observations: int, replications: int, block_length: int, seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    blocks = int(math.ceil(observations / block_length))
    starts = rng.integers(0, observations, size=(replications, blocks))
    offsets = np.arange(block_length)
    return ((starts[:, :, None] + offsets) % observations).reshape(replications, -1)[:, :observations]


def bootstrap_task(task: dict[str, object]) -> dict[str, object]:
    matrix = np.asarray(task.pop("matrix"), dtype=float)
    indices = circular_indices(
        matrix.shape[0],
        int(task["replications"]),
        int(task["block_length_months"]),
        stable_seed(task["population"], task["variant"], task["metric"], task["block_length_months"]),
    )
    rows = []
    for column, allocation in enumerate(ALLOCATIONS):
        values = matrix[:, column]
        estimates = values[indices].mean(axis=1)
        low, median, high = np.quantile(estimates, [0.025, 0.5, 0.975])
        rows.append(
            {
                **task,
                "allocation": allocation,
                "observations": len(values),
                "estimate": float(values.mean()),
                "ci_2_5": float(low),
                "bootstrap_median": float(median),
                "ci_97_5": float(high),
                "p_two_sided": float(min(1.0, 2 * min((estimates <= 0).mean(), (estimates >= 0).mean()))),
            }
        )
    return {"rows": rows}


def main() -> None:
    source = pd.read_parquet(RESULTS_DIR / "local_composite_date.parquet")
    summary_rows: list[dict[str, object]] = []
    tasks: list[dict[str, object]] = []

    for population, variant in SAMPLES:
        sample = source[(source["population"] == population) & (source["variant"] == variant)].copy()
        for metric in METRICS:
            pivot = sample.pivot(index="signal_date", columns="allocation", values=metric)
            pivot = pivot.reindex(columns=ALLOCATIONS).dropna(how="any").sort_index()
            for allocation in ALLOCATIONS:
                stats = newey_west_mean(pivot[allocation], maxlags=1)
                summary_rows.append(
                    {
                        "population": population,
                        "variant": variant,
                        "allocation": allocation,
                        "metric": metric,
                        "start": pivot.index.min(),
                        "end": pivot.index.max(),
                        "n": stats["n"],
                        "mean": stats["mean"],
                        "se": stats["se"],
                        "t": stats["t"],
                        "p": stats["p"],
                    }
                )
            tasks.append(
                {
                    "population": population,
                    "variant": variant,
                    "metric": metric,
                    "block_length_months": 6,
                    "replications": 2_000,
                    "matrix": pivot.to_numpy(),
                }
            )

    context = mp.get_context("spawn")
    with ProcessPoolExecutor(max_workers=min(8, os.cpu_count() or 4), mp_context=context) as executor:
        bootstrap_outputs = list(executor.map(bootstrap_task, tasks))
    bootstrap_rows = [row for output in bootstrap_outputs for row in output["rows"]]

    write_frame(pd.DataFrame(summary_rows), RESULTS_DIR / "local_composite_common_window.csv")
    write_frame(pd.DataFrame(bootstrap_rows), RESULTS_DIR / "local_composite_common_window_bootstrap.csv")
    print("common-window analysis complete")


if __name__ == "__main__":
    main()
