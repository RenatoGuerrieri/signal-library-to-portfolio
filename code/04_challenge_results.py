from __future__ import annotations

import hashlib
import io
import json
import math
import multiprocessing as mp
import os
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import statsmodels.api as sm
from scipy.stats import spearmanr

from analysis_common import (
    INTERMEDIATE_DIR,
    RANDOM_SEED,
    REGISTRY_PARQUET,
    RESULTS_DIR,
    RETURN_LABELS,
    SCORE_PANEL,
    SOURCE_DIR,
    atomic_write_json,
    ensure_directories,
    normalise_path,
    sha256_file,
    write_frame,
)


def stable_seed(*parts: object) -> int:
    text = "|".join(str(part) for part in parts)
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    return RANDOM_SEED + int.from_bytes(digest[:4], "little")


def circular_indices(
    observations: int, replications: int, block_length: int, seed: int
) -> np.ndarray:
    rng = np.random.default_rng(seed)
    blocks = int(math.ceil(observations / block_length))
    starts = rng.integers(0, observations, size=(replications, blocks))
    offsets = np.arange(block_length)
    return ((starts[:, :, None] + offsets) % observations).reshape(replications, -1)[
        :, :observations
    ]


def bootstrap_task(task: dict[str, object]) -> dict[str, object]:
    values = np.asarray(task["values"], dtype=float)
    valid = np.isfinite(values)
    values = values[valid]
    result = {key: value for key, value in task.items() if key != "values"}
    if len(values) < 12:
        result.update(
            {
                "observations": len(values),
                "estimate": float(np.nanmean(values)) if len(values) else math.nan,
                "ci_2_5": math.nan,
                "bootstrap_median": math.nan,
                "ci_97_5": math.nan,
                "p_two_sided": math.nan,
            }
        )
        return result
    indices = circular_indices(
        len(values),
        int(task["replications"]),
        int(task["block_length_months"]),
        stable_seed(
            task["population"],
            task["variant"],
            task["allocation"],
            task["metric"],
            task["block_length_months"],
        ),
    )
    estimates = values[indices].mean(axis=1)
    low, median, high = np.quantile(estimates, [0.025, 0.5, 0.975])
    probability_non_positive = (estimates <= 0).mean()
    probability_non_negative = (estimates >= 0).mean()
    result.update(
        {
            "observations": len(values),
            "estimate": float(values.mean()),
            "ci_2_5": float(low),
            "bootstrap_median": float(median),
            "ci_97_5": float(high),
            "p_two_sided": float(min(1.0, 2 * min(probability_non_positive, probability_non_negative))),
        }
    )
    return result


def bootstrap_composites(frame: pd.DataFrame) -> pd.DataFrame:
    tasks = []
    metrics = ["ic", "spread", "spread_winsorised", "net_spread_25bps"]
    for keys, part in frame.groupby(["population", "variant", "allocation"], sort=True):
        for block_length, replications in ((6, 2_000), (3, 1_000), (12, 1_000)):
            for metric in metrics:
                tasks.append(
                    {
                        "population": keys[0],
                        "variant": keys[1],
                        "allocation": keys[2],
                        "metric": metric,
                        "block_length_months": block_length,
                        "replications": replications,
                        "values": part.sort_values("signal_date")[metric].tolist(),
                    }
                )
    workers = min(8, os.cpu_count() or 4)
    context = mp.get_context("spawn")
    with ProcessPoolExecutor(max_workers=workers, mp_context=context) as executor:
        rows = list(executor.map(bootstrap_task, tasks, chunksize=4))
    return pd.DataFrame(rows)


def read_french_csv(path: Path) -> pd.DataFrame:
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    header = next(index for index, line in enumerate(lines) if line.startswith(","))
    frame = pd.read_csv(io.StringIO("\n".join(lines[header:])), dtype=str)
    date_column = frame.columns[0]
    dates = frame[date_column].astype(str).str.strip()
    frame = frame[dates.str.fullmatch(r"\d{6}")].copy()
    frame["date"] = pd.PeriodIndex(dates[dates.str.fullmatch(r"\d{6}")], freq="M")
    for column in frame.columns:
        if column not in {date_column, "date"}:
            frame[column] = pd.to_numeric(frame[column].str.strip(), errors="coerce") / 100.0
    return frame.drop(columns=[date_column])


def factor_regressions(monthly: pd.DataFrame) -> pd.DataFrame:
    factor_root = SOURCE_DIR / "fama_french_2024"
    ff5 = read_french_csv(factor_root / "ff5" / "F-F_Research_Data_5_Factors_2x3.csv")
    momentum = read_french_csv(factor_root / "mom" / "F-F_Momentum_Factor.CSV")
    factors = ff5.merge(momentum, on="date", how="inner", validate="one_to_one")
    factors = factors.rename(columns={column: column.strip() for column in factors.columns})
    monthly = monthly.copy()
    monthly["date"] = pd.to_datetime(monthly["date"]).dt.to_period("M")
    rows = []
    specifications = {
        "ff3_plus_momentum": ["Mkt-RF", "SMB", "HML", "Mom"],
        "ff5": ["Mkt-RF", "SMB", "HML", "RMW", "CMA"],
        "ff5_plus_momentum": ["Mkt-RF", "SMB", "HML", "RMW", "CMA", "Mom"],
    }
    for period, start in (("1990-2024", "1990-01"), ("2016-2024", "2016-01")):
        start_period = pd.Period(start, freq="M")
        end_period = pd.Period("2024-12", freq="M")
        for composite, part in monthly.groupby("composite"):
            merged = part.merge(factors, on="date", how="inner", validate="many_to_one")
            merged = merged[
                (merged["date"] >= start_period) & (merged["date"] <= end_period)
            ]
            for specification, columns in specifications.items():
                sample = merged[["return", *columns]].dropna()
                fit = sm.OLS(sample["return"], sm.add_constant(sample[columns])).fit(
                    cov_type="HAC", cov_kwds={"maxlags": 3, "use_correction": True}
                )
                row = {
                    "period": period,
                    "composite": composite,
                    "specification": specification,
                    "months": len(sample),
                    "monthly_intercept": float(fit.params["const"]),
                    "annualised_intercept": float(fit.params["const"] * 12),
                    "intercept_hac_t": float(fit.tvalues["const"]),
                    "intercept_p_value": float(fit.pvalues["const"]),
                    "adjusted_r_squared": float(fit.rsquared_adj),
                }
                row.update({f"beta_{column}": float(fit.params[column]) for column in columns})
                rows.append(row)
    return pd.DataFrame(rows)


def direct_recalculation(composites: pd.DataFrame, registry: pd.DataFrame) -> pd.DataFrame:
    target = composites[
        (composites["population"] == "gbp_fixed")
        & (composites["variant"] == "broad_deduplicated")
        & (composites["allocation"] == "equal_signal")
        & composites["ic"].notna()
    ].sort_values("signal_date")
    positions = np.linspace(0, len(target) - 1, 5).round().astype(int)
    selected = target.iloc[positions]
    identifiers = registry.loc[
        registry["broad_eligible"] & registry["dedupe_representative"],
        "canonical_signal_id",
    ].tolist()
    connection = duckdb.connect()
    rows = []
    for expected in selected.itertuples(index=False):
        scores = connection.execute(
            f"""
            SELECT ticker, canonical_signal_id, score_rank_pct
            FROM read_parquet('{normalise_path(SCORE_PANEL)}')
            WHERE signal_date = ?
              AND canonical_signal_id IN (SELECT UNNEST(?))
            """,
            [expected.signal_date, identifiers],
        ).fetchdf()
        labels = connection.execute(
            f"""
            SELECT ticker, gbp_return_clean_21d
            FROM read_parquet('{normalise_path(RETURN_LABELS)}')
            WHERE signal_date = ?
            """,
            [expected.signal_date],
        ).fetchdf()
        pivot = scores.pivot(
            index="ticker", columns="canonical_signal_id", values="score_rank_pct"
        )
        score = pivot.mean(axis=1, skipna=True).rank(method="average", pct=True)
        frame = score.rename("score").to_frame().join(labels.set_index("ticker"), how="inner").dropna()
        recalculated_ic = float(spearmanr(frame["score"], frame["gbp_return_clean_21d"]).statistic)
        top = frame.loc[frame["score"] >= 0.8, "gbp_return_clean_21d"]
        bottom = frame.loc[frame["score"] <= 0.2, "gbp_return_clean_21d"]
        recalculated_spread = float(top.mean() - bottom.mean())
        rows.append(
            {
                "signal_date": expected.signal_date,
                "observations": len(frame),
                "reported_ic": expected.ic,
                "recalculated_ic": recalculated_ic,
                "absolute_ic_difference": abs(expected.ic - recalculated_ic),
                "reported_spread": expected.spread,
                "recalculated_spread": recalculated_spread,
                "absolute_spread_difference": abs(expected.spread - recalculated_spread),
            }
        )
    connection.close()
    return pd.DataFrame(rows)


def evidence_summaries(
    composites: pd.DataFrame,
    standalone: pd.DataFrame,
    primary: pd.DataFrame,
    marginal: pd.DataFrame,
) -> dict[str, pd.DataFrame]:
    standalone_rows = []
    for population, part in standalone.groupby("population"):
        standalone_rows.append(
            {
                "population": population,
                "signals": len(part),
                "estimable": int(part["ic_21d_mean"].notna().sum()),
                "positive": int((part["ic_21d_mean"] > 0).sum()),
                "negative": int((part["ic_21d_mean"] < 0).sum()),
                "q_le_010": int((part["ic_21d_q_bh"] <= 0.10).sum()),
                "positive_q_le_010": int(
                    ((part["ic_21d_q_bh"] <= 0.10) & (part["ic_21d_mean"] > 0)).sum()
                ),
                "median_mean_ic": part["ic_21d_mean"].median(),
            }
        )
    primary_rows = []
    for population, part in primary.groupby("population"):
        primary_rows.append(
            {
                "population": population,
                "signals": len(part),
                "estimable": int(part["primary_ic_mean"].notna().sum()),
                "positive": int((part["primary_ic_mean"] > 0).sum()),
                "negative": int((part["primary_ic_mean"] < 0).sum()),
                "q_le_010": int((part["primary_ic_q_bh"] <= 0.10).sum()),
                "positive_q_le_010": int(
                    ((part["primary_ic_q_bh"] <= 0.10) & (part["primary_ic_mean"] > 0)).sum()
                ),
                "median_mean_ic": part["primary_ic_mean"].median(),
            }
        )
    marginal_join = marginal.merge(
        standalone[
            ["population", "canonical_signal_id", "ic_21d_mean", "ic_21d_q_bh"]
        ],
        on=["population", "canonical_signal_id"],
        how="left",
        validate="one_to_one",
    )
    marginal_rows = []
    for population, part in marginal_join.groupby("population"):
        marginal_rows.append(
            {
                "population": population,
                "signals": len(part),
                "positive_marginal": int((part["marginal_ic_mean"] > 0).sum()),
                "negative_marginal": int((part["marginal_ic_mean"] < 0).sum()),
                "positive_standalone_negative_marginal": int(
                    ((part["ic_21d_mean"] > 0) & (part["marginal_ic_mean"] < 0)).sum()
                ),
                "marginal_q_le_010": int((part["marginal_ic_q_bh"] <= 0.10).sum()),
                "positive_marginal_q_le_010": int(
                    ((part["marginal_ic_q_bh"] <= 0.10) & (part["marginal_ic_mean"] > 0)).sum()
                ),
                "median_marginal_ic": part["marginal_ic_mean"].median(),
            }
        )

    attribution = composites[
        [
            "population",
            "variant",
            "allocation",
            "top_quintile_return_mean",
            "bottom_quintile_return_mean",
            "short_leg_contribution_mean",
            "spread_mean",
            "mean_turnover_two_legs",
        ]
    ].copy()
    attribution["break_even_cost_bps"] = (
        attribution["spread_mean"] / attribution["mean_turnover_two_legs"] * 10_000
    )
    return {
        "standalone_counts": pd.DataFrame(standalone_rows),
        "primary_counts": pd.DataFrame(primary_rows),
        "marginal_counts": pd.DataFrame(marginal_rows),
        "long_short_attribution": attribution,
    }


def validate_invariants() -> tuple[pd.DataFrame, dict[str, object]]:
    composites = pd.read_parquet(RESULTS_DIR / "local_composite_date.parquet")
    allocations = pd.read_parquet(RESULTS_DIR / "local_allocation_weights.parquet")
    weights = pd.read_parquet(RESULTS_DIR / "local_anonymised_weights.parquet")
    exposures = pd.read_parquet(RESULTS_DIR / "local_exposures.parquet")
    implementation = pd.read_parquet(RESULTS_DIR / "local_implementation_date.parquet")

    checks = []

    def add(name: str, passed: bool, value: object, requirement: str) -> None:
        checks.append(
            {"check": name, "passed": bool(passed), "value": value, "requirement": requirement}
        )

    duplicate_keys = composites.duplicated(
        ["population", "variant", "allocation", "signal_date"]
    ).sum()
    add("composite keys unique", duplicate_keys == 0, int(duplicate_keys), "zero duplicates")

    global_minimum = composites.loc[
        (composites["population"] == "gbp_fixed") & composites["ic"].notna(), "observations"
    ].min()
    us_minimum = composites.loc[
        (composites["population"] == "us_domestic_fixed") & composites["ic"].notna(),
        "observations",
    ].min()
    add("global population floor", global_minimum >= 1_000, int(global_minimum), ">= 1000")
    add("US population floor", us_minimum >= 300, int(us_minimum), ">= 300")

    inverse = composites[composites["allocation"] == "inverse_redundancy"]
    add(
        "inverse redundancy finite",
        inverse["ic"].notna().all() and len(inverse) > 0,
        int(inverse["ic"].notna().sum()),
        f"all {len(inverse)} rows",
    )

    allocation_sums = allocations.groupby(
        ["population", "variant", "allocation", "signal_date"]
    )["weight"].sum()
    allocation_error = float((allocation_sums - 1.0).abs().max())
    add("group allocation sums", allocation_error < 1e-10, allocation_error, "maximum error < 1e-10")

    weight_sums = weights.groupby(["signal_date", "allocation"])["target_weight"].agg(
        net="sum", gross=lambda series: series.abs().sum()
    )
    max_net = float(weight_sums["net"].abs().max())
    max_gross_error = float((weight_sums["gross"] - 2.0).abs().max())
    add("anonymised target net", max_net < 1e-10, max_net, "maximum absolute net < 1e-10")
    add(
        "anonymised target gross",
        max_gross_error < 1e-10,
        max_gross_error,
        "maximum gross error from 2 < 1e-10",
    )

    exposure_sums = exposures.groupby(["signal_date", "allocation", "dimension"])[
        "net_weight"
    ].sum()
    exposure_error = float(exposure_sums.abs().max())
    add("exposure reconciliation", exposure_error < 1e-10, exposure_error, "maximum error < 1e-10")

    cancellation = implementation["trade_cancellation"].dropna()
    cancellation_valid = ((cancellation >= -1e-12) & (cancellation <= 1 + 1e-12)).all()
    add(
        "trade cancellation bounds",
        cancellation_valid,
        [float(cancellation.min()), float(cancellation.max())],
        "within [0, 1]",
    )

    cost_columns = [
        "net_spread_10bps",
        "net_spread_25bps",
        "net_spread_50bps",
        "net_spread_100bps",
    ]
    valid_cost = composites.dropna(subset=cost_columns)
    monotone = (
        (valid_cost[cost_columns[0]] >= valid_cost[cost_columns[1]])
        & (valid_cost[cost_columns[1]] >= valid_cost[cost_columns[2]])
        & (valid_cost[cost_columns[2]] >= valid_cost[cost_columns[3]])
    ).all()
    add("cost monotonicity", monotone, int(len(valid_cost)), "all finite rows non-increasing")

    checks_frame = pd.DataFrame(checks)
    summary = {
        "checks": len(checks_frame),
        "checks_passed": int(checks_frame["passed"].sum()),
        "all_checks_passed": bool(checks_frame["passed"].all()),
    }
    return checks_frame, summary


def main() -> None:
    ensure_directories()
    composites_date = pd.read_parquet(RESULTS_DIR / "local_composite_date.parquet")
    composites_summary = pd.read_csv(RESULTS_DIR / "local_composite_summary.csv")
    standalone = pd.read_csv(RESULTS_DIR / "local_standalone_summary.csv")
    primary = pd.read_csv(RESULTS_DIR / "local_primary_horizon_summary.csv")
    marginal = pd.read_csv(RESULTS_DIR / "local_marginal_summary.csv")
    registry = pd.read_parquet(REGISTRY_PARQUET)

    print("running parallel block bootstrap", flush=True)
    bootstrap = bootstrap_composites(composites_date)
    write_frame(bootstrap, RESULTS_DIR / "composite_block_bootstrap.csv")

    print("running independent direct recalculation", flush=True)
    recalculation = direct_recalculation(composites_date, registry)
    write_frame(recalculation, RESULTS_DIR / "direct_recalculation_audit.csv")

    print("running Fama/French factor diagnostics", flush=True)
    monthly = pd.read_csv(RESULTS_DIR / "external_composite_monthly.csv")
    regressions = factor_regressions(monthly)
    write_frame(regressions, RESULTS_DIR / "external_factor_regressions.csv")

    summaries = evidence_summaries(composites_summary, standalone, primary, marginal)
    for name, frame in summaries.items():
        write_frame(frame, RESULTS_DIR / f"evidence_{name}.csv")

    checks, validation = validate_invariants()
    write_frame(checks, RESULTS_DIR / "challenge_invariant_checks.csv")
    validation["direct_recalculation_max_ic_difference"] = float(
        recalculation["absolute_ic_difference"].max()
    )
    validation["direct_recalculation_max_spread_difference"] = float(
        recalculation["absolute_spread_difference"].max()
    )
    validation["direct_recalculation_passed"] = bool(
        (recalculation["absolute_ic_difference"] < 1e-12).all()
        and (recalculation["absolute_spread_difference"] < 1e-12).all()
    )
    validation["bootstrap_rows"] = len(bootstrap)
    validation["factor_regression_rows"] = len(regressions)
    validation["all_checks_passed"] = bool(
        validation["all_checks_passed"] and validation["direct_recalculation_passed"]
    )
    atomic_write_json(RESULTS_DIR / "challenge_validation.json", validation)

    factor_root = SOURCE_DIR / "fama_french_2024"
    metadata = {
        "release": "December 2024 CRSP legacy-format archive",
        "official_page": "https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/data_library_202412_archive.html",
        "five_factor_url": "https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp_202412/F-F_Research_Data_5_Factors_2x3_CSV.zip",
        "momentum_url": "https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp_202412/F-F_Momentum_Factor_CSV.zip",
        "files": {
            str(path.relative_to(factor_root)): {
                "bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            }
            for path in factor_root.rglob("*")
            if path.is_file()
        },
    }
    atomic_write_json(RESULTS_DIR / "fama_french_source_metadata.json", metadata)
    print(json.dumps(validation, indent=2), flush=True)


if __name__ == "__main__":
    main()
