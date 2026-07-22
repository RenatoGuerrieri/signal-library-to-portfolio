from __future__ import annotations

import argparse
import math
import time
import warnings

import duckdb
import numpy as np
import pandas as pd
from scipy.stats import spearmanr

from analysis_common import (
    INTERMEDIATE_DIR,
    RANDOM_SEED,
    REGISTRY_PARQUET,
    RESULTS_DIR,
    SCORE_PANEL,
    atomic_write_json,
    circular_block_weights,
    component_count,
    effective_rank,
    ensure_directories,
    normalise_path,
    quantile_interval,
    variant_identifiers,
    write_frame,
)


ARRAY_PATH = INTERMEDIATE_DIR / "score_structure_by_date.npz"


def tail_overlap(
    values: np.ndarray, threshold: float, upper: bool, minimum_common: int = 200
) -> np.ndarray:
    valid = np.isfinite(values).astype(np.float32)
    if upper:
        selected = ((values >= threshold) & np.isfinite(values)).astype(np.float32)
    else:
        selected = ((values <= threshold) & np.isfinite(values)).astype(np.float32)
    intersection = selected.T @ selected
    common = valid.T @ valid
    selected_with_other_valid = selected.T @ valid
    union = selected_with_other_valid + selected_with_other_valid.T - intersection
    return np.divide(
        intersection,
        union,
        out=np.full_like(intersection, np.nan, dtype=np.float32),
        where=(union > 0) & (common >= minimum_common),
    )


def build_date_arrays(
    connection: duckdb.DuckDBPyConnection, signal_ids: list[str]
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, pd.DataFrame]:
    dates = connection.execute(
        f"SELECT DISTINCT signal_date FROM read_parquet('{normalise_path(SCORE_PANEL)}') ORDER BY 1"
    ).fetchdf()["signal_date"]
    correlations: list[np.ndarray] = []
    top_overlap: list[np.ndarray] = []
    bottom_overlap: list[np.ndarray] = []
    date_audit: list[dict[str, object]] = []
    started = time.time()
    for sequence, date in enumerate(dates, start=1):
        frame = connection.execute(
            f"""
            SELECT ticker, canonical_signal_id, score_rank_pct
            FROM read_parquet('{normalise_path(SCORE_PANEL)}')
            WHERE signal_date = ?
            """,
            [date],
        ).fetchdf()
        pivot = frame.pivot(index="ticker", columns="canonical_signal_id", values="score_rank_pct")
        pivot = pivot.reindex(columns=signal_ids)
        values = pivot.to_numpy(dtype=float)
        correlation = pivot.corr(method="spearman", min_periods=200).to_numpy(dtype=np.float32)
        correlations.append(correlation)
        top_overlap.append(tail_overlap(values, 0.8, True))
        bottom_overlap.append(tail_overlap(values, 0.2, False))
        date_audit.append(
            {
                "signal_date": date,
                "securities": len(pivot),
                "observations": int(np.isfinite(values).sum()),
                "complete_signals": int((np.isfinite(values).sum(axis=0) == len(pivot)).sum()),
                "minimum_signal_count": int(np.isfinite(values).sum(axis=0).min()),
                "median_signal_count": float(np.median(np.isfinite(values).sum(axis=0))),
            }
        )
        if sequence % 12 == 0:
            print(
                f"structure date {sequence:,}/{len(dates):,} completed in "
                f"{time.time() - started:,.1f}s",
                flush=True,
            )
    return (
        pd.to_datetime(dates).to_numpy(dtype="datetime64[D]"),
        np.stack(correlations),
        np.stack(top_overlap),
        np.stack(bottom_overlap),
        pd.DataFrame(date_audit),
    )


def mean_matrix(values: np.ndarray) -> np.ndarray:
    with np.errstate(invalid="ignore"), warnings.catch_warnings():
        warnings.simplefilter("ignore", category=RuntimeWarning)
        matrix = np.nanmean(values, axis=0)
    matrix = (matrix + matrix.T) / 2.0
    np.fill_diagonal(matrix, 1.0)
    return matrix


def pair_table(
    registry: pd.DataFrame,
    signal_ids: list[str],
    correlation_dates: np.ndarray,
    top_dates: np.ndarray,
    bottom_dates: np.ndarray,
) -> pd.DataFrame:
    metadata = registry.set_index("canonical_signal_id")
    correlation_mean = mean_matrix(correlation_dates)
    correlation_median = np.nanmedian(correlation_dates, axis=0)
    top_mean = mean_matrix(top_dates)
    bottom_mean = mean_matrix(bottom_dates)
    rows = []
    for left in range(len(signal_ids)):
        for right in range(left + 1, len(signal_ids)):
            left_id, right_id = signal_ids[left], signal_ids[right]
            left_meta, right_meta = metadata.loc[left_id], metadata.loc[right_id]
            rows.append(
                {
                    "left_signal_id": left_id,
                    "right_signal_id": right_id,
                    "left_acronym": left_meta["oap_acronym"],
                    "right_acronym": right_meta["oap_acronym"],
                    "left_group": left_meta["cat_data"],
                    "right_group": right_meta["cat_data"],
                    "left_family": left_meta["signal_family"],
                    "right_family": right_meta["signal_family"],
                    "left_formula_id": left_meta["local_formula_id"],
                    "right_formula_id": right_meta["local_formula_id"],
                    "same_group": left_meta["cat_data"] == right_meta["cat_data"],
                    "same_family": left_meta["signal_family"] == right_meta["signal_family"],
                    "same_formula": left_meta["local_formula_id"] == right_meta["local_formula_id"],
                    "mean_score_correlation": correlation_mean[left, right],
                    "median_date_score_correlation": correlation_median[left, right],
                    "mean_top_jaccard": top_mean[left, right],
                    "mean_bottom_jaccard": bottom_mean[left, right],
                    "complete_correlation_dates": int(
                        np.isfinite(correlation_dates[:, left, right]).sum()
                    ),
                    "complete_top_overlap_dates": int(
                        np.isfinite(top_dates[:, left, right]).sum()
                    ),
                }
            )
    return pd.DataFrame(rows)


def summary_rows(
    registry: pd.DataFrame,
    signal_ids: list[str],
    correlation_dates: np.ndarray,
    top_dates: np.ndarray,
    bottom_dates: np.ndarray,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    positions = {signal_id: position for position, signal_id in enumerate(signal_ids)}
    rows = []
    network_rows = []
    for variant, identifiers in variant_identifiers(registry).items():
        indices = np.array([positions[value] for value in identifiers], dtype=int)
        correlation = mean_matrix(correlation_dates[:, indices][:, :, indices])
        top = mean_matrix(top_dates[:, indices][:, :, indices])
        bottom = mean_matrix(bottom_dates[:, indices][:, :, indices])
        upper = np.triu_indices(len(indices), 1)
        corr_values = correlation[upper]
        top_values = top[upper]
        bottom_values = bottom[upper]
        rank_overlap = spearmanr(
            np.abs(corr_values), top_values, nan_policy="omit"
        ).statistic
        ranks = effective_rank(correlation)
        rows.append(
            {
                "variant": variant,
                "characteristics": len(indices),
                "pairs": len(corr_values),
                "median_absolute_score_correlation": np.nanmedian(np.abs(corr_values)),
                "p90_absolute_score_correlation": np.nanquantile(np.abs(corr_values), 0.90),
                "p95_absolute_score_correlation": np.nanquantile(np.abs(corr_values), 0.95),
                "p99_absolute_score_correlation": np.nanquantile(np.abs(corr_values), 0.99),
                "pairs_abs_correlation_ge_070": int((np.abs(corr_values) >= 0.70).sum()),
                "pairs_abs_correlation_ge_080": int((np.abs(corr_values) >= 0.80).sum()),
                "pairs_abs_correlation_ge_090": int((np.abs(corr_values) >= 0.90).sum()),
                "median_top_jaccard": np.nanmedian(top_values),
                "p90_top_jaccard": np.nanquantile(top_values, 0.90),
                "pairs_top_jaccard_ge_050": int((top_values >= 0.50).sum()),
                "median_bottom_jaccard": np.nanmedian(bottom_values),
                "score_overlap_rank_correlation": rank_overlap,
                **ranks,
            }
        )
        for threshold in (0.70, 0.80, 0.90):
            components, isolated, largest = component_count(correlation, threshold)
            network_rows.append(
                {
                    "variant": variant,
                    "threshold": threshold,
                    "components": components,
                    "isolated_characteristics": isolated,
                    "largest_component": largest,
                }
            )
    return pd.DataFrame(rows), pd.DataFrame(network_rows)


def bootstrap_effective_rank(
    registry: pd.DataFrame,
    signal_ids: list[str],
    correlation_dates: np.ndarray,
    block_length: int,
    replications: int,
) -> pd.DataFrame:
    positions = {signal_id: position for position, signal_id in enumerate(signal_ids)}
    variants = {
        variant: np.array([positions[value] for value in identifiers], dtype=int)
        for variant, identifiers in variant_identifiers(registry).items()
    }
    weights = circular_block_weights(
        correlation_dates.shape[0], replications, block_length, RANDOM_SEED + block_length
    )
    finite = np.isfinite(correlation_dates).astype(np.float32)
    values = np.nan_to_num(correlation_dates, nan=0.0).astype(np.float32)
    estimates: dict[str, dict[str, list[float]]] = {
        variant: {"participation_ratio": [], "entropy_rank": [], "top_eigen_share": []}
        for variant in variants
    }
    batch_size = 100
    for start in range(0, replications, batch_size):
        current = weights[start : start + batch_size]
        numerator = np.einsum("bd,dij->bij", current, values, optimize=True)
        denominator = np.einsum("bd,dij->bij", current, finite, optimize=True)
        matrices = np.divide(
            numerator,
            denominator,
            out=np.zeros_like(numerator),
            where=denominator > 0,
        )
        for matrix in matrices:
            for variant, indices in variants.items():
                submatrix = matrix[np.ix_(indices, indices)]
                stats = effective_rank(submatrix)
                for metric in estimates[variant]:
                    estimates[variant][metric].append(stats[metric])
    rows = []
    for variant, metrics in estimates.items():
        for metric, values_for_metric in metrics.items():
            low, median, high = quantile_interval(values_for_metric)
            rows.append(
                {
                    "variant": variant,
                    "block_length_months": block_length,
                    "replications": replications,
                    "metric": metric,
                    "ci_2_5": low,
                    "bootstrap_median": median,
                    "ci_97_5": high,
                }
            )
    return pd.DataFrame(rows)


def grouped_pair_summary(pairs: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for label, mask in {
        "within_group": pairs["same_group"],
        "across_group": ~pairs["same_group"],
        "within_family": pairs["same_family"],
        "across_family": ~pairs["same_family"],
        "same_formula": pairs["same_formula"],
        "different_formula": ~pairs["same_formula"],
    }.items():
        subset = pairs[mask]
        rows.append(
            {
                "comparison": label,
                "pairs": len(subset),
                "median_absolute_score_correlation": subset[
                    "mean_score_correlation"
                ].abs().median(),
                "median_top_jaccard": subset["mean_top_jaccard"].median(),
                "median_bottom_jaccard": subset["mean_bottom_jaccard"].median(),
            }
        )
    return pd.DataFrame(rows)


def apply_exact_score_deduplication(
    registry: pd.DataFrame, exact_pairs: pd.DataFrame
) -> pd.DataFrame:
    updated = registry.copy()
    parent = {value: value for value in updated.loc[updated["broad_eligible"], "canonical_signal_id"]}

    def find(value: str) -> str:
        while parent[value] != value:
            parent[value] = parent[parent[value]]
            value = parent[value]
        return value

    def union(left: str, right: str) -> None:
        a, b = find(left), find(right)
        if a != b:
            parent[b] = a

    for row in exact_pairs.itertuples(index=False):
        union(row.left_signal_id, row.right_signal_id)
    components: dict[str, list[str]] = {}
    for value in parent:
        components.setdefault(find(value), []).append(value)

    for members in components.values():
        eligible_members = updated[
            updated["canonical_signal_id"].isin(members)
            & updated["dedupe_representative"]
        ].copy()
        if len(eligible_members) <= 1:
            continue
        eligible_members["quality_order"] = (
            eligible_members["signal_rep_quality"]
            .fillna("99_unknown")
            .str.extract(r"^(\d+)", expand=False)
            .fillna("99")
            .astype(int)
        )
        ordered = eligible_members.sort_values(
            [
                "native",
                "source_code_file_exists",
                "quality_order",
                "date_count",
                "median_cross_section",
                "canonical_signal_id",
            ],
            ascending=[False, False, True, False, False, True],
        )
        updated.loc[ordered.index[1:], "dedupe_representative"] = False
    return updated


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reuse-arrays", action="store_true")
    args = parser.parse_args()
    ensure_directories()
    registry = pd.read_parquet(REGISTRY_PARQUET)
    broad = registry[registry["broad_eligible"]].sort_values("canonical_signal_id")
    signal_ids = broad["canonical_signal_id"].tolist()
    connection = duckdb.connect()
    connection.execute("SET threads TO 8")

    if args.reuse_arrays and ARRAY_PATH.exists():
        arrays = np.load(ARRAY_PATH, allow_pickle=False)
        dates = arrays["dates"]
        correlation_dates = arrays["correlation"]
        top_dates = arrays["top_overlap"]
        bottom_dates = arrays["bottom_overlap"]
        date_audit = pd.read_csv(RESULTS_DIR / "score_structure_date_audit.csv")
    else:
        dates, correlation_dates, top_dates, bottom_dates, date_audit = build_date_arrays(
            connection, signal_ids
        )
        np.savez_compressed(
            ARRAY_PATH,
            dates=dates,
            signal_ids=np.asarray(signal_ids),
            correlation=correlation_dates,
            top_overlap=top_dates,
            bottom_overlap=bottom_dates,
        )
        write_frame(date_audit, RESULTS_DIR / "score_structure_date_audit.csv")

    pairs = pair_table(registry, signal_ids, correlation_dates, top_dates, bottom_dates)
    write_frame(pairs, RESULTS_DIR / "score_pair_metrics_private.parquet")
    write_frame(pairs, RESULTS_DIR / "score_pair_metrics_private.csv")
    anonymised = pairs.copy()
    anonymised["pair_number"] = np.arange(1, len(anonymised) + 1)
    anonymised = anonymised[
        [
            "pair_number",
            "left_group",
            "right_group",
            "same_group",
            "same_family",
            "same_formula",
            "mean_score_correlation",
            "median_date_score_correlation",
            "mean_top_jaccard",
            "mean_bottom_jaccard",
            "complete_correlation_dates",
        ]
    ]
    write_frame(anonymised, RESULTS_DIR / "score_pair_metrics_public.csv")

    exact = pairs[
        (pairs["mean_score_correlation"].abs() >= 0.999999)
        & (pairs["mean_top_jaccard"] >= 0.999999)
        & (pairs["mean_bottom_jaccard"] >= 0.999999)
    ].copy()
    write_frame(exact, RESULTS_DIR / "exact_duplicate_pairs.csv")

    registry = apply_exact_score_deduplication(registry, exact)
    write_frame(registry, REGISTRY_PARQUET)
    write_frame(registry, RESULTS_DIR / "signal_registry.csv")

    summary, network = summary_rows(
        registry, signal_ids, correlation_dates, top_dates, bottom_dates
    )
    write_frame(summary, RESULTS_DIR / "score_structure_summary.csv")
    write_frame(network, RESULTS_DIR / "score_network_summary.csv")
    write_frame(grouped_pair_summary(pairs), RESULTS_DIR / "score_pair_group_comparison.csv")

    bootstrap_frames = []
    for block_length, replications in ((6, 2_000), (3, 1_000), (12, 1_000)):
        print(f"bootstrapping effective rank: block={block_length}, replications={replications}")
        bootstrap_frames.append(
            bootstrap_effective_rank(
                registry,
                signal_ids,
                correlation_dates,
                block_length,
                replications,
            )
        )
    bootstrap = pd.concat(bootstrap_frames, ignore_index=True)
    write_frame(bootstrap, RESULTS_DIR / "score_effective_rank_bootstrap.csv")

    audit = {
        "signal_count": len(signal_ids),
        "date_count": len(dates),
        "pair_count": len(pairs),
        "exact_duplicate_pairs": len(exact),
        "all_pair_correlations_estimable": bool(
            np.isfinite(pairs["mean_score_correlation"]).all()
        ),
        "all_pair_top_overlaps_estimable": bool(np.isfinite(pairs["mean_top_jaccard"]).all()),
        "all_pair_bottom_overlaps_estimable": bool(
            np.isfinite(pairs["mean_bottom_jaccard"]).all()
        ),
    }
    atomic_write_json(RESULTS_DIR / "score_structure_validation.json", audit)
    print("structure analysis complete")


if __name__ == "__main__":
    main()
