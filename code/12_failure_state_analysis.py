from __future__ import annotations

import itertools
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import hypergeom

from analysis_common import (
    RANDOM_SEED,
    RESULTS_DIR,
    SOURCE_DIR,
    atomic_write_json,
    ensure_directories,
    write_frame,
)


PERMUTATIONS = 2_000
PERMUTATION_WORKERS = 4


def published_return_path() -> Path:
    return SOURCE_DIR / "open_asset_pricing_public" / "raw" / "PredictorLSretWide.csv"


def pairwise_worst_decile_jaccard(frame: pd.DataFrame) -> float:
    worst_dates: dict[str, set[pd.Timestamp]] = {}
    for column in frame:
        series = frame[column].dropna()
        threshold = series.quantile(0.10)
        worst_dates[column] = set(series.index[series <= threshold])
    values = []
    for left, right in itertools.combinations(frame.columns, 2):
        union = worst_dates[left] | worst_dates[right]
        if union:
            values.append(len(worst_dates[left] & worst_dates[right]) / len(union))
    return float(np.median(values))


def worst_decile_membership(frame: pd.DataFrame) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    valid = frame.notna().to_numpy()
    membership = np.zeros(frame.shape, dtype=np.int16)
    counts = np.zeros(frame.shape[1], dtype=int)
    for column_number, column in enumerate(frame.columns):
        series = frame[column].dropna()
        threshold = series.quantile(0.10)
        selected = frame[column].le(threshold) & frame[column].notna()
        membership[:, column_number] = selected.to_numpy(dtype=np.int16)
        counts[column_number] = int(selected.sum())
    return valid, membership, counts


def median_jaccard_from_membership(
    membership: np.ndarray, counts: np.ndarray
) -> float:
    intersections = membership.T @ membership
    unions = counts[:, None] + counts[None, :] - intersections
    upper = np.triu_indices(len(counts), 1)
    values = intersections[upper] / unions[upper]
    return float(np.median(values))


def permutation_chunk(
    valid: np.ndarray,
    counts: np.ndarray,
    repetitions: int,
    seed: int,
) -> np.ndarray:
    rng = np.random.default_rng(seed)
    rows, columns = valid.shape
    output = np.empty(repetitions, dtype=float)
    for repetition in range(repetitions):
        random_values = rng.random((rows, columns))
        random_values[~valid] = np.inf
        order = np.argsort(random_values, axis=0)
        membership = np.zeros((rows, columns), dtype=np.int16)
        for column, count in enumerate(counts):
            membership[order[:count, column], column] = 1
        output[repetition] = median_jaccard_from_membership(membership, counts)
    return output


def fixed_count_expected_jaccard(observation_count: int, selected_count: int) -> float:
    lower = max(0, 2 * selected_count - observation_count)
    upper = selected_count
    intersections = np.arange(lower, upper + 1)
    probabilities = hypergeom.pmf(
        intersections, observation_count, selected_count, selected_count
    )
    jaccards = intersections / (2 * selected_count - intersections)
    return float(np.sum(probabilities * jaccards))


def overlap_null_benchmark(
    frame: pd.DataFrame,
    *,
    evidence: str,
    measure: str,
    seed_offset: int,
) -> dict[str, object]:
    valid, membership, counts = worst_decile_membership(frame)
    observed = median_jaccard_from_membership(membership, counts)
    chunks = [PERMUTATIONS // PERMUTATION_WORKERS] * PERMUTATION_WORKERS
    for index in range(PERMUTATIONS % PERMUTATION_WORKERS):
        chunks[index] += 1
    seeds = np.random.SeedSequence(RANDOM_SEED + seed_offset).spawn(len(chunks))
    with ProcessPoolExecutor(max_workers=PERMUTATION_WORKERS) as executor:
        futures = [
            executor.submit(
                permutation_chunk,
                valid,
                counts,
                repetitions,
                int(seed.generate_state(1, dtype=np.uint32)[0]),
            )
            for repetitions, seed in zip(chunks, seeds)
        ]
        null = np.concatenate([future.result() for future in futures])

    complete = bool(valid.all())
    equal_counts = bool(np.all(counts == counts[0]))
    finite_expected = (
        fixed_count_expected_jaccard(len(frame), int(counts[0]))
        if complete and equal_counts
        else None
    )
    upper_p = float((1 + np.sum(null >= observed)) / (len(null) + 1))
    lower, upper = np.quantile(null, [0.025, 0.975])
    return {
        "evidence": evidence,
        "measure": measure,
        "observed_median_jaccard": observed,
        "mechanical_independence_decile_jaccard": 0.10 / 1.90,
        "fixed_count_expected_jaccard": finite_expected,
        "permutations": len(null),
        "null_mean_median_jaccard": float(np.mean(null)),
        "null_median_jaccard": float(np.median(null)),
        "null_2_5_percentile": float(lower),
        "null_97_5_percentile": float(upper),
        "one_sided_p_value": upper_p,
        "above_null_97_5_percentile": bool(observed > upper),
    }


def conditional_statistics(
    frame: pd.DataFrame,
    composite: pd.Series,
    *,
    evidence: str,
    component_name: str,
) -> list[dict[str, object]]:
    common = frame.index.intersection(composite.dropna().index)
    current = frame.loc[common]
    current_composite = composite.loc[common]
    threshold = float(current_composite.quantile(0.10))
    worst = current_composite <= threshold
    negative_fraction = (current < 0.0).mean(axis=1)

    overall_corr = current.corr(min_periods=max(8, int(len(current) * 0.20)))
    worst_corr = current.loc[worst].corr(min_periods=max(6, int(worst.sum() * 0.60)))
    upper = np.triu_indices(len(current.columns), 1)
    overall_values = overall_corr.to_numpy()[upper]
    worst_values = worst_corr.to_numpy()[upper]

    return [
        {
            "evidence": evidence,
            "measure": f"mean fraction of {component_name} below zero",
            "all_observations": float(negative_fraction.mean()),
            "worst_decile_observations": float(negative_fraction.loc[worst].mean()),
            "observations": int(len(current)),
            "worst_decile_count": int(worst.sum()),
        },
        {
            "evidence": evidence,
            "measure": f"frequency with a majority of {component_name} below zero",
            "all_observations": float((negative_fraction > 0.5).mean()),
            "worst_decile_observations": float((negative_fraction.loc[worst] > 0.5).mean()),
            "observations": int(len(current)),
            "worst_decile_count": int(worst.sum()),
        },
        {
            "evidence": evidence,
            "measure": f"median pairwise correlation across {component_name}",
            "all_observations": float(np.nanmedian(overall_values)),
            "worst_decile_observations": float(np.nanmedian(worst_values)),
            "observations": int(len(current)),
            "worst_decile_count": int(worst.sum()),
        },
        {
            "evidence": evidence,
            "measure": f"median pairwise overlap of component-specific worst deciles",
            "all_observations": pairwise_worst_decile_jaccard(current),
            "worst_decile_observations": np.nan,
            "observations": int(len(current)),
            "worst_decile_count": int(worst.sum()),
        },
    ]


def local_evidence() -> list[dict[str, object]]:
    groups = pd.read_parquet(RESULTS_DIR / "local_group_date.parquet")
    groups = groups[
        (groups["population"] == "gbp_fixed")
        & (groups["variant"] == "broad_deduplicated")
    ].copy()
    groups["signal_date"] = pd.to_datetime(groups["signal_date"])
    group_frame = groups.pivot(index="signal_date", columns="group", values="ic").sort_index()

    composites = pd.read_parquet(RESULTS_DIR / "local_composite_date.parquet")
    composites = composites[
        (composites["population"] == "gbp_fixed")
        & (composites["variant"] == "broad_deduplicated")
        & (composites["allocation"] == "equal_signal")
    ].copy()
    composites["signal_date"] = pd.to_datetime(composites["signal_date"])
    composite = composites.set_index("signal_date")["ic"].sort_index()
    return conditional_statistics(
        group_frame,
        composite,
        evidence="Local group rank information",
        component_name="groups",
    )


def public_evidence() -> tuple[list[dict[str, object]], pd.DataFrame, pd.DataFrame]:
    registry = pd.read_csv(RESULTS_DIR / "signal_registry.csv")
    acronyms = registry.loc[registry["broad_eligible"], "oap_acronym"].tolist()
    published = pd.read_csv(published_return_path(), na_values="NA")
    published["date"] = pd.to_datetime(published["date"])
    frame = published.set_index("date").sort_index().loc["1990-01-01":"2024-12-31", acronyms]
    frame = frame / 100.0
    composite = frame.mean(axis=1, skipna=True)
    rows = conditional_statistics(
        frame,
        composite,
        evidence="Public predictor returns",
        component_name="predictor portfolios",
    )
    wealth = (1.0 + frame).cumprod()
    drawdowns = wealth.div(wealth.cummax()).sub(1.0)
    rows.append(
        {
            "evidence": "Public predictor returns",
            "measure": "median pairwise overlap of predictor-specific worst drawdown deciles",
            "all_observations": pairwise_worst_decile_jaccard(drawdowns),
            "worst_decile_observations": np.nan,
            "observations": int(len(frame)),
            "worst_decile_count": int((composite <= composite.quantile(0.10)).sum()),
        }
    )
    return rows, frame, drawdowns


def main() -> None:
    ensure_directories()
    public_rows, public_returns, public_drawdowns = public_evidence()
    rows = local_evidence() + public_rows
    output = pd.DataFrame(rows)
    write_frame(output, RESULTS_DIR / "failure_state_evidence.csv")
    null_output = pd.DataFrame(
        [
            overlap_null_benchmark(
                public_returns,
                evidence="Public predictor returns",
                measure="Median pairwise overlap of predictor-specific worst return deciles",
                seed_offset=101,
            ),
            overlap_null_benchmark(
                public_drawdowns,
                evidence="Public predictor returns",
                measure="Median pairwise overlap of predictor-specific worst drawdown deciles",
                seed_offset=202,
            ),
        ]
    )
    write_frame(null_output, RESULTS_DIR / "failure_state_null_benchmark.csv")
    atomic_write_json(
        RESULTS_DIR / "failure_state_evidence.json",
        {
            "rows": len(output),
            "conditions": "Worst decile of the named equal-signal composite within its stated sample",
            "regime_labels_used": False,
            "all_values_finite_where_required": bool(
                output["all_observations"].notna().all()
                and (
                    output["worst_decile_observations"].notna()
                    | output["measure"].str.contains("overlap")
                ).all()
            ),
        },
    )
    atomic_write_json(
        RESULTS_DIR / "failure_state_null_benchmark.json",
        {
            "permutations_per_test": PERMUTATIONS,
            "workers": PERMUTATION_WORKERS,
            "random_seed": RANDOM_SEED,
            "mechanical_independence_decile_jaccard": 0.10 / 1.90,
            "tests": null_output.to_dict(orient="records"),
        },
    )
    print(output.to_string(index=False))
    print(null_output.to_string(index=False))


if __name__ == "__main__":
    main()
