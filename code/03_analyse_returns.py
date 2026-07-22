from __future__ import annotations

import json
import math
import multiprocessing as mp
import os
import time
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
from scipy.stats import spearmanr

from analysis_common import (
    GROUP_ORDER,
    HORIZONS,
    INTERMEDIATE_DIR,
    RANDOM_SEED,
    REGISTRY_PARQUET,
    RESULTS_DIR,
    RETURN_LABELS,
    SCORE_PANEL,
    SOURCE_DIR,
    atomic_write_json,
    bh_adjust,
    cross_section_winsorise,
    effective_rank,
    ensure_directories,
    newey_west_mean,
    normalise_path,
    one_way_replacement,
    rank_percentile,
    security_hash,
    variant_identifiers,
    write_frame,
)

LOCAL_JOB_VERSION = "spearman_corrected_20260721"


def population_job_prefix(population: str, variant: str) -> str:
    version = (
        "two_sided_sleeves_20260721"
        if population == "gbp_fixed" and variant == "broad_deduplicated"
        else LOCAL_JOB_VERSION
    )
    return f"{version}__{population}__{variant}"


def information_coefficient(score: pd.Series, forward_return: pd.Series) -> float:
    frame = pd.concat([score.rename("score"), forward_return.rename("return")], axis=1).dropna()
    if len(frame) < 30 or frame["score"].nunique() < 2 or frame["return"].nunique() < 2:
        return math.nan
    return float(
        frame["score"].rank(method="average", pct=True).corr(
            frame["return"].rank(method="average", pct=True)
        )
    )


def quintile_spread(score: pd.Series, forward_return: pd.Series) -> float:
    frame = pd.concat([score.rename("score"), forward_return.rename("return")], axis=1).dropna()
    top = frame.loc[frame["score"] >= 0.8, "return"]
    bottom = frame.loc[frame["score"] <= 0.2, "return"]
    if len(top) < 10 or len(bottom) < 10:
        return math.nan
    return float(top.mean() - bottom.mean())


def quintile_legs(score: pd.Series, forward_return: pd.Series) -> tuple[float, float]:
    frame = pd.concat([score.rename("score"), forward_return.rename("return")], axis=1).dropna()
    top = frame.loc[frame["score"] >= 0.8, "return"]
    bottom = frame.loc[frame["score"] <= 0.2, "return"]
    if len(top) < 10 or len(bottom) < 10:
        return math.nan, math.nan
    return float(top.mean()), float(bottom.mean())


def target_weights(score: pd.Series) -> pd.Series:
    clean = score.dropna()
    top = clean[clean >= 0.8].index
    bottom = clean[clean <= 0.2].index
    weights = pd.Series(0.0, index=clean.index)
    if len(top) < 10 or len(bottom) < 10:
        return weights
    weights.loc[top] = 1.0 / len(top)
    weights.loc[bottom] = -1.0 / len(bottom)
    return weights


def target_turnover(current: pd.Series, previous: pd.Series | None) -> float:
    if previous is None:
        return math.nan
    current_aligned, previous_aligned = current.align(previous, join="outer", fill_value=0.0)
    return float(0.5 * (current_aligned - previous_aligned).abs().sum())


def group_scores(score: pd.DataFrame, groups: dict[str, list[str]]) -> pd.DataFrame:
    output = {}
    for group in GROUP_ORDER:
        identifiers = [value for value in groups.get(group, []) if value in score.columns]
        if identifiers:
            output[group] = rank_percentile(score[identifiers].mean(axis=1, skipna=True))
    return pd.DataFrame(output, index=score.index)


def dedupe_sets(registry: pd.DataFrame) -> dict[str, list[str]]:
    variants = variant_identifiers(registry)
    return {
        "broad": variants["broad"],
        "broad_deduplicated": variants["broad_deduplicated"],
        "native": variants["native"],
    }


def independently_screen_us(
    connection: duckdb.DuckDBPyConnection, registry: pd.DataFrame
) -> pd.DataFrame:
    query = f"""
        WITH by_date AS (
            SELECT s.canonical_signal_id, s.signal_date, COUNT(*) AS cross_section
            FROM read_parquet('{normalise_path(SCORE_PANEL)}') s
            JOIN read_parquet('{normalise_path(RETURN_LABELS)}') r
              ON s.ticker = r.ticker AND s.signal_date = r.signal_date
            WHERE r.clean_us AND r.gbp_return_clean_21d IS NOT NULL
            GROUP BY 1, 2
        ), coverage AS (
            SELECT canonical_signal_id, COUNT(*) AS date_count,
                   MEDIAN(cross_section) AS median_cross_section,
                   MIN(signal_date) AS first_date, MAX(signal_date) AS last_date
            FROM by_date GROUP BY 1
        )
        SELECT * FROM coverage ORDER BY canonical_signal_id
    """
    coverage = connection.execute(query).fetchdf()
    coverage["us_eligible"] = (
        (coverage["date_count"] >= 80) & (coverage["median_cross_section"] >= 300)
    )
    return coverage.merge(
        registry[
            [
                "canonical_signal_id",
                "oap_acronym",
                "cat_data",
                "signal_family",
                "native",
                "dedupe_representative",
            ]
        ],
        on="canonical_signal_id",
        how="left",
        validate="one_to_one",
    )


def sample_group_map(registry: pd.DataFrame, identifiers: list[str]) -> dict[str, list[str]]:
    subset = registry[registry["canonical_signal_id"].isin(identifiers)]
    return {
        group: part["canonical_signal_id"].tolist()
        for group, part in subset.groupby("cat_data")
    }


def analyse_population(
    connection: duckdb.DuckDBPyConnection,
    registry: pd.DataFrame,
    labels: pd.DataFrame,
    population_name: str,
    variant_sets: dict[str, list[str]],
    implementation_variant: str | None = None,
) -> dict[str, pd.DataFrame]:
    all_ids = sorted(set().union(*variant_sets.values()))
    registry_index = registry.set_index("canonical_signal_id")
    score_dates = pd.to_datetime(
        connection.execute(
            f"SELECT DISTINCT signal_date FROM read_parquet('{normalise_path(SCORE_PANEL)}')"
        ).fetchdf()["signal_date"]
    )
    dates = sorted(set(pd.to_datetime(labels["signal_date"])).intersection(score_dates))
    labels_by_date = {date: part.set_index("ticker") for date, part in labels.groupby("signal_date")}

    standalone_rows: list[dict[str, object]] = []
    primary_rows: list[dict[str, object]] = []
    marginal_rows: list[dict[str, object]] = []
    group_rows: list[dict[str, object]] = []
    composite_rows: list[dict[str, object]] = []
    allocation_rows: list[dict[str, object]] = []
    implementation_rows: list[dict[str, object]] = []
    exposure_rows: list[dict[str, object]] = []
    weight_rows: list[dict[str, object]] = []

    previous_targets: dict[str, pd.Series] = {}
    previous_top: dict[str, set[str]] = {}
    previous_bottom: dict[str, set[str]] = {}
    group_corr_history: dict[str, list[pd.DataFrame]] = defaultdict(list)
    group_ic_history: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    group_marginal_history: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    previous_sleeves: pd.DataFrame | None = None
    previous_aggregate_sleeve: pd.Series | None = None
    started = time.time()

    for date_number, signal_date in enumerate(dates, start=1):
        if signal_date not in labels_by_date:
            continue
        label = labels_by_date[signal_date]
        score_long = connection.execute(
            f"""
            SELECT ticker, canonical_signal_id, score_rank_pct,
                   region, country, sector, exchange
            FROM read_parquet('{normalise_path(SCORE_PANEL)}')
            WHERE signal_date = ?
            """,
            [signal_date],
        ).fetchdf()
        score = score_long.pivot(
            index="ticker", columns="canonical_signal_id", values="score_rank_pct"
        ).reindex(columns=all_ids)
        common = score.index.intersection(label.index)
        score = score.loc[common]
        label = label.loc[common]
        y = label["gbp_return_clean_21d"].astype(float)
        minimum_population = 1_000 if population_name == "gbp_fixed" else 300
        valid_population = score.notna().any(axis=1) & y.notna()
        if int(valid_population.sum()) < minimum_population:
            continue
        y_winsor = cross_section_winsorise(y)
        y_rank = y.rank(method="average", pct=True)
        metadata = score_long.drop_duplicates("ticker").set_index("ticker").reindex(common)
        metadata["currency"] = label["currency"]

        for signal_id in all_ids:
            values = score[signal_id]
            standalone_rows.append(
                {
                    "population": population_name,
                    "signal_date": signal_date,
                    "canonical_signal_id": signal_id,
                    "oap_acronym": registry_index.loc[signal_id, "oap_acronym"],
                    "group": registry_index.loc[signal_id, "cat_data"],
                    "native": bool(registry_index.loc[signal_id, "native"]),
                    "dedupe_representative": bool(
                        registry_index.loc[signal_id, "dedupe_representative"]
                    ),
                    "observations": int(pd.concat([values, y], axis=1).dropna().shape[0]),
                    "ic_21d": information_coefficient(values, y),
                    "spread_21d": quintile_spread(values, y),
                    "spread_21d_winsorised": quintile_spread(values, y_winsor),
                }
            )
            horizon = int(registry_index.loc[signal_id, "primary_horizon"])
            primary_y = label[f"gbp_return_clean_{horizon}d"].astype(float)
            primary_rows.append(
                {
                    "population": population_name,
                    "signal_date": signal_date,
                    "canonical_signal_id": signal_id,
                    "oap_acronym": registry_index.loc[signal_id, "oap_acronym"],
                    "group": registry_index.loc[signal_id, "cat_data"],
                    "native": bool(registry_index.loc[signal_id, "native"]),
                    "dedupe_representative": bool(
                        registry_index.loc[signal_id, "dedupe_representative"]
                    ),
                    "primary_horizon": horizon,
                    "observations": int(pd.concat([values, primary_y], axis=1).dropna().shape[0]),
                    "primary_ic": information_coefficient(values, primary_y),
                    "primary_spread": quintile_spread(values, primary_y),
                    "primary_spread_winsorised": quintile_spread(
                        values, cross_section_winsorise(primary_y)
                    ),
                }
            )

        for variant, identifiers in variant_sets.items():
            identifiers = [value for value in identifiers if value in score.columns]
            current = score[identifiers]
            groups = sample_group_map(registry, identifiers)
            group_frame = group_scores(current, groups)
            available_groups = list(group_frame.columns)
            if not available_groups:
                continue

            full_group_score = rank_percentile(group_frame.mean(axis=1, skipna=True))
            full_group_ic = information_coefficient(full_group_score, y)
            current_group_corr = group_frame.corr(min_periods=100)

            for group in available_groups:
                group_score = group_frame[group]
                group_ic = information_coefficient(group_score, y)
                group_spread = quintile_spread(group_score, y)
                without_groups = [value for value in available_groups if value != group]
                without_score = (
                    rank_percentile(group_frame[without_groups].mean(axis=1, skipna=True))
                    if without_groups
                    else pd.Series(np.nan, index=group_frame.index)
                )
                without_ic = information_coefficient(without_score, y)
                marginal_ic = full_group_ic - without_ic if np.isfinite(without_ic) else math.nan
                group_rows.append(
                    {
                        "population": population_name,
                        "variant": variant,
                        "signal_date": signal_date,
                        "group": group,
                        "signals": len(groups[group]),
                        "ic": group_ic,
                        "spread": group_spread,
                        "marginal_ic": marginal_ic,
                    }
                )
                group_ic_history[variant][group].append(group_ic)
                group_marginal_history[variant][group].append(marginal_ic)

            allocation_scores: dict[str, tuple[pd.Series | None, dict[str, float]]] = {}
            allocation_scores["equal_signal"] = (
                rank_percentile(current.mean(axis=1, skipna=True)),
                {group: len(groups[group]) / len(identifiers) for group in available_groups},
            )
            allocation_scores["equal_group"] = (
                full_group_score,
                {group: 1.0 / len(available_groups) for group in available_groups},
            )

            if len(group_corr_history[variant]) >= 36:
                trailing_corr = (
                    pd.concat(group_corr_history[variant][-36:])
                    .groupby(level=0)
                    .mean()
                    .reindex(index=available_groups, columns=available_groups)
                )
                redundancy = {}
                for group in available_groups:
                    others = [value for value in available_groups if value != group]
                    redundancy[group] = float(trailing_corr.loc[group, others].abs().mean())
                raw = {group: 1.0 / (0.05 + redundancy[group]) for group in available_groups}
                total_raw = sum(raw.values())
                inverse_weights = {group: raw[group] / total_raw for group in available_groups}
                inverse_score = rank_percentile(
                    sum(group_frame[group] * inverse_weights[group] for group in available_groups)
                )
                allocation_scores["inverse_redundancy"] = (inverse_score, inverse_weights)
            else:
                allocation_scores["inverse_redundancy"] = (None, {})

            selected_groups = []
            if all(len(group_ic_history[variant][group]) > 36 for group in available_groups):
                for group in available_groups:
                    prior_ic = group_ic_history[variant][group][-37:-1]
                    prior_marginal = group_marginal_history[variant][group][-37:-1]
                    if np.nanmean(prior_ic) > 0 and np.nanmean(prior_marginal) > 0:
                        selected_groups.append(group)
            if selected_groups:
                trailing_weights = {
                    group: (1.0 / len(selected_groups) if group in selected_groups else 0.0)
                    for group in available_groups
                }
                trailing_score = rank_percentile(
                    group_frame[selected_groups].mean(axis=1, skipna=True)
                )
                allocation_scores["trailing_positive_evidence"] = (
                    trailing_score,
                    trailing_weights,
                )
            else:
                allocation_scores["trailing_positive_evidence"] = (None, {})

            for allocation, (portfolio_score, allocation_weights) in allocation_scores.items():
                if portfolio_score is None:
                    continue
                key = f"{population_name}|{variant}|{allocation}"
                target = target_weights(portfolio_score)
                turnover = target_turnover(target, previous_targets.get(key))
                top_names = set(portfolio_score[portfolio_score >= 0.8].dropna().index)
                bottom_names = set(portfolio_score[portfolio_score <= 0.2].dropna().index)
                top_replacement = one_way_replacement(top_names, previous_top.get(key))
                bottom_replacement = one_way_replacement(bottom_names, previous_bottom.get(key))
                spread = quintile_spread(portfolio_score, y)
                spread_winsorised = quintile_spread(portfolio_score, y_winsor)
                top_return, bottom_return = quintile_legs(portfolio_score, y)
                row = {
                    "population": population_name,
                    "variant": variant,
                    "allocation": allocation,
                    "signal_date": signal_date,
                    "signals": len(identifiers),
                    "groups": len(available_groups),
                    "observations": int(pd.concat([portfolio_score, y], axis=1).dropna().shape[0]),
                    "ic": information_coefficient(portfolio_score, y),
                    "spread": spread,
                    "spread_winsorised": spread_winsorised,
                    "top_quintile_return": top_return,
                    "bottom_quintile_return": bottom_return,
                    "short_leg_contribution": -bottom_return if np.isfinite(bottom_return) else math.nan,
                    "turnover_two_legs": turnover,
                    "top_replacement": top_replacement,
                    "bottom_replacement": bottom_replacement,
                }
                for basis_points in (10, 25, 50, 100):
                    row[f"net_spread_{basis_points}bps"] = (
                        spread - turnover * basis_points / 10_000
                        if np.isfinite(spread) and np.isfinite(turnover)
                        else math.nan
                    )
                composite_rows.append(row)
                for group, weight in allocation_weights.items():
                    allocation_rows.append(
                        {
                            "population": population_name,
                            "variant": variant,
                            "allocation": allocation,
                            "signal_date": signal_date,
                            "group": group,
                            "weight": weight,
                        }
                    )

                if population_name == "gbp_fixed" and variant == "broad_deduplicated":
                    for security, weight in target.items():
                        weight_rows.append(
                            {
                                "signal_date": signal_date,
                                "allocation": allocation,
                                "security_id_hash": security_hash(security),
                                "target_weight": weight,
                            }
                        )
                    if allocation in {
                        "equal_signal",
                        "equal_group",
                        "inverse_redundancy",
                        "trailing_positive_evidence",
                    }:
                        exposure_frame = metadata.join(target.rename("weight"), how="inner")
                        for dimension in ("sector", "region", "currency"):
                            for bucket, part in exposure_frame.groupby(dimension, dropna=False):
                                exposure_rows.append(
                                    {
                                        "signal_date": signal_date,
                                        "allocation": allocation,
                                        "dimension": dimension,
                                        "bucket": str(bucket),
                                        "net_weight": part["weight"].sum(),
                                        "gross_weight": part["weight"].abs().sum(),
                                    }
                                )
                previous_targets[key] = target
                previous_top[key] = top_names
                previous_bottom[key] = bottom_names

            group_corr_history[variant].append(current_group_corr)

            if variant == implementation_variant:
                sleeve = pd.DataFrame(0.0, index=current.index, columns=identifiers)
                for signal_id in identifiers:
                    sleeve[signal_id] = target_weights(current[signal_id]).reindex(
                        current.index, fill_value=0.0
                    )
                aggregate = sleeve.mean(axis=1)
                if previous_sleeves is not None:
                    current_aligned, previous_aligned = sleeve.align(
                        previous_sleeves, join="outer", axis=0, fill_value=0.0
                    )
                    individual_turnover = 0.5 * (current_aligned - previous_aligned).abs().sum(axis=0)
                    aggregate_turnover = target_turnover(aggregate, previous_aggregate_sleeve)
                    weighted_individual = float(individual_turnover.mean())
                    cancellation = (
                        1.0 - aggregate_turnover / weighted_individual
                        if weighted_individual > 0
                        else math.nan
                    )
                    aggregate_aligned, previous_aggregate_aligned = aggregate.align(
                        previous_aggregate_sleeve, join="outer", fill_value=0.0
                    )
                    trade = (aggregate_aligned - previous_aggregate_aligned).abs()
                    total_trade = float(trade.sum())
                    top10_share = (
                        float(trade.nlargest(10).sum() / total_trade) if total_trade > 0 else math.nan
                    )
                    adv = label["adv60_gbp"].reindex(trade.index).astype(float)
                    valid_capacity = (trade > 1e-8) & (adv > 0) & np.isfinite(adv)
                    capacity_base = (adv[valid_capacity] / trade[valid_capacity]).replace(
                        [np.inf, -np.inf], np.nan
                    ).dropna()
                    implementation_row = {
                        "population": population_name,
                        "variant": variant,
                        "signal_date": signal_date,
                        "signals": len(identifiers),
                        "weighted_sleeve_turnover": weighted_individual,
                        "aggregate_sleeve_turnover": aggregate_turnover,
                        "trade_cancellation": cancellation,
                        "aggregate_gross_exposure": float(aggregate.abs().sum()),
                        "aggregate_net_exposure": float(aggregate.sum()),
                        "top10_trade_share": top10_share,
                        "capacity_line_items": len(capacity_base),
                    }
                    for participation in (0.01, 0.05, 0.10):
                        implementation_row[f"capacity_p10_gbp_at_{int(participation*100)}pct"] = (
                            float((capacity_base * participation).quantile(0.10))
                            if len(capacity_base)
                            else math.nan
                        )
                        implementation_row[f"capacity_median_gbp_at_{int(participation*100)}pct"] = (
                            float((capacity_base * participation).median())
                            if len(capacity_base)
                            else math.nan
                        )
                    implementation_rows.append(implementation_row)
                previous_sleeves = sleeve
                previous_aggregate_sleeve = aggregate

            if population_name == "gbp_fixed" and variant == "broad_deduplicated":
                full_score = rank_percentile(current.mean(axis=1, skipna=True))
                full_ic = information_coefficient(full_score, y)
                row_sum = current.sum(axis=1, skipna=True)
                row_count = current.notna().sum(axis=1)
                for signal_id in identifiers:
                    available = current[signal_id].notna().astype(int)
                    denominator = row_count - available
                    without = (row_sum - current[signal_id].fillna(0.0)).div(
                        denominator.replace(0, np.nan)
                    )
                    without_score = rank_percentile(without)
                    without_ic = information_coefficient(without_score, y)
                    marginal_rows.append(
                        {
                            "population": population_name,
                            "variant": variant,
                            "signal_date": signal_date,
                            "canonical_signal_id": signal_id,
                            "oap_acronym": registry_index.loc[signal_id, "oap_acronym"],
                            "group": registry_index.loc[signal_id, "cat_data"],
                            "standalone_ic": information_coefficient(current[signal_id], y),
                            "full_composite_ic": full_ic,
                            "without_signal_ic": without_ic,
                            "marginal_ic": full_ic - without_ic,
                        }
                    )

        if date_number % 12 == 0:
            print(
                f"{population_name}: {date_number:,}/{len(dates):,} dates in "
                f"{time.time() - started:,.1f}s",
                flush=True,
            )

    return {
        "standalone_date": pd.DataFrame(standalone_rows),
        "primary_date": pd.DataFrame(primary_rows),
        "marginal_date": pd.DataFrame(marginal_rows),
        "group_date": pd.DataFrame(group_rows),
        "composite_date": pd.DataFrame(composite_rows),
        "allocation_weights": pd.DataFrame(allocation_rows),
        "implementation_date": pd.DataFrame(implementation_rows),
        "exposures": pd.DataFrame(exposure_rows),
        "anonymised_weights": pd.DataFrame(weight_rows),
    }


def summarise_signal_series(
    frame: pd.DataFrame, value: str, output_name: str, lag_column: str | None = None
) -> pd.DataFrame:
    rows = []
    keys = ["population", "canonical_signal_id", "oap_acronym", "group", "native", "dedupe_representative"]
    available_keys = [key for key in keys if key in frame.columns]
    for key_values, part in frame.groupby(available_keys, dropna=False):
        if not isinstance(key_values, tuple):
            key_values = (key_values,)
        row = dict(zip(available_keys, key_values))
        if lag_column:
            horizon = int(part[lag_column].iloc[0])
            maxlags = max(1, int(math.ceil(horizon / 21)))
            row[lag_column] = horizon
        else:
            maxlags = 1
        stats = newey_west_mean(part[value], maxlags=maxlags)
        row.update({f"{output_name}_{key}": value for key, value in stats.items()})
        rows.append(row)
    result = pd.DataFrame(rows)
    result[f"{output_name}_q_bh"] = np.nan
    for _, indices in result.groupby("population").groups.items():
        result.loc[indices, f"{output_name}_q_bh"] = bh_adjust(
            result.loc[indices, f"{output_name}_p"]
        )
    return result


def summarise_group_series(frame: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for keys, part in frame.groupby(["population", "variant", "group"], dropna=False):
        row = dict(zip(["population", "variant", "group"], keys))
        row["signals"] = int(part["signals"].max())
        for column in ("ic", "spread", "marginal_ic"):
            stats = newey_west_mean(part[column], maxlags=1)
            row.update({f"{column}_{key}": value for key, value in stats.items()})
        rows.append(row)
    return pd.DataFrame(rows)


def summarise_composites(frame: pd.DataFrame) -> pd.DataFrame:
    rows = []
    values = [
        "ic",
        "spread",
        "spread_winsorised",
        "top_quintile_return",
        "bottom_quintile_return",
        "short_leg_contribution",
        "net_spread_10bps",
        "net_spread_25bps",
        "net_spread_50bps",
        "net_spread_100bps",
    ]
    for keys, part in frame.groupby(["population", "variant", "allocation"], dropna=False):
        row = dict(zip(["population", "variant", "allocation"], keys))
        row["dates"] = part["signal_date"].nunique()
        row["signals"] = int(part["signals"].max())
        row["groups"] = int(part["groups"].max())
        row["mean_turnover_two_legs"] = part["turnover_two_legs"].mean()
        row["mean_top_replacement"] = part["top_replacement"].mean()
        row["mean_bottom_replacement"] = part["bottom_replacement"].mean()
        for column in values:
            stats = newey_west_mean(part[column], maxlags=1)
            row.update({f"{column}_{key}": value for key, value in stats.items()})
        rows.append(row)
    return pd.DataFrame(rows)


def temporal_composites(frame: pd.DataFrame) -> pd.DataFrame:
    frame = frame.copy()
    frame["year"] = pd.to_datetime(frame["signal_date"]).dt.year
    frame["period"] = pd.cut(
        frame["year"],
        bins=[2015, 2019, 2022, 2026],
        labels=["2016-2019", "2020-2022", "2023-2026"],
    )
    rows = []
    for keys, part in frame.groupby(
        ["population", "variant", "allocation", "period"], observed=True
    ):
        row = dict(zip(["population", "variant", "allocation", "period"], keys))
        row["dates"] = len(part)
        row["mean_ic"] = part["ic"].mean()
        row["mean_spread"] = part["spread"].mean()
        row["positive_ic_fraction"] = (part["ic"] > 0).mean()
        row["positive_spread_fraction"] = (part["spread"] > 0).mean()
        rows.append(row)
    return pd.DataFrame(rows)


def external_data_paths() -> dict[str, Path]:
    root = SOURCE_DIR / "open_asset_pricing_public"
    return {
        "published_long_short": root / "raw" / "PredictorLSretWide.csv",
        "original_portfolios": root / "raw" / "PredictorPortsFull.csv",
        "equal_weighted_deciles": root
        / "extracted"
        / "PredictorAltPorts_Deciles"
        / "PredictorAltPorts_Deciles.csv",
        "value_weighted_deciles": root
        / "extracted"
        / "PredictorAltPorts_DecilesVW"
        / "PredictorAltPorts_DecilesVW.csv",
        "quintiles": root
        / "extracted"
        / "PredictorAltPorts_Quintiles"
        / "PredictorAltPorts_Quintiles.csv",
        "market_cap_above_nyse20": root
        / "extracted"
        / "PredictorAltPorts_LiqScreen_ME_gt_NYSE20pct"
        / "PredictorAltPorts_LiqScreen_ME_gt_NYSE20pct.csv",
        "nyse_only": root
        / "extracted"
        / "PredictorAltPorts_LiqScreen_NYSEonly"
        / "PredictorAltPorts_LiqScreen_NYSEonly.csv",
        "price_above_five": root
        / "extracted"
        / "PredictorAltPorts_LiqScreen_Price_gt_5"
        / "PredictorAltPorts_LiqScreen_Price_gt_5.csv",
    }


def analyse_external(registry: pd.DataFrame) -> dict[str, pd.DataFrame]:
    paths = external_data_paths()
    broad = registry[registry["broad_eligible"]].copy()
    acronyms = broad["oap_acronym"].tolist()
    acronym_group = broad.set_index("oap_acronym")["cat_data"].to_dict()
    dedup_acronyms = broad.loc[broad["dedupe_representative"], "oap_acronym"].tolist()
    native_acronyms = broad.loc[broad["native"], "oap_acronym"].tolist()

    published = pd.read_csv(paths["published_long_short"], na_values="NA")
    published["date"] = pd.to_datetime(published["date"])
    published = published.set_index("date").sort_index()[acronyms] / 100.0
    principal = published.loc["1990-01-01":"2024-12-31"]
    contemporaneous = published.loc["2016-01-01":"2024-12-31"]

    structure_rows = []
    pair_rows = []
    for variant, identifiers in {
        "broad": acronyms,
        "broad_deduplicated": dedup_acronyms,
        "native_mapping": native_acronyms,
    }.items():
        correlation = principal[identifiers].corr(min_periods=240).to_numpy()
        upper = np.triu_indices(len(identifiers), 1)
        values = correlation[upper]
        ranks = effective_rank(correlation)
        structure_rows.append(
            {
                "variant": variant,
                "characteristics": len(identifiers),
                "period_start": principal.index.min(),
                "period_end": principal.index.max(),
                "median_absolute_return_correlation": np.nanmedian(np.abs(values)),
                "p90_absolute_return_correlation": np.nanquantile(np.abs(values), 0.90),
                "p95_absolute_return_correlation": np.nanquantile(np.abs(values), 0.95),
                "pairs_abs_correlation_ge_070": int((np.abs(values) >= 0.70).sum()),
                **ranks,
            }
        )
        if variant == "broad":
            for left in range(len(identifiers)):
                for right in range(left + 1, len(identifiers)):
                    pair_rows.append(
                        {
                            "left_acronym": identifiers[left],
                            "right_acronym": identifiers[right],
                            "left_group": acronym_group[identifiers[left]],
                            "right_group": acronym_group[identifiers[right]],
                            "same_group": acronym_group[identifiers[left]]
                            == acronym_group[identifiers[right]],
                            "return_correlation": correlation[left, right],
                        }
                    )

    standalone_rows = []
    for period_name, frame, minimum in (
        ("1990-2024", principal, 240),
        ("2016-2024", contemporaneous, 96),
    ):
        period_rows = []
        for signal in acronyms:
            series = frame[signal].dropna()
            stats = newey_west_mean(series, maxlags=3)
            period_rows.append(
                {
                    "period": period_name,
                    "oap_acronym": signal,
                    "group": acronym_group[signal],
                    "eligible": len(series) >= minimum,
                    **stats,
                }
            )
        period_frame = pd.DataFrame(period_rows)
        period_frame["q_bh"] = bh_adjust(period_frame["p"])
        standalone_rows.append(period_frame)

    composite_rows = []
    composite_monthly_rows = []
    temporal_rows = []
    for period_name, frame in (("1990-2024", principal), ("2016-2024", contemporaneous)):
        group_frame = pd.DataFrame(
            {
                group: frame[[value for value in acronyms if acronym_group[value] == group]].mean(
                    axis=1, skipna=True
                )
                for group in GROUP_ORDER
            }
        )
        composites = {
            "equal_signal": frame[acronyms].mean(axis=1, skipna=True),
            "equal_signal_deduplicated": frame[dedup_acronyms].mean(axis=1, skipna=True),
            "equal_group": group_frame.mean(axis=1, skipna=True),
        }
        for name, series in composites.items():
            stats = newey_west_mean(series, maxlags=3)
            composite_rows.append(
                {
                    "period": period_name,
                    "composite": name,
                    **stats,
                    "monthly_volatility": series.std(ddof=1),
                    "annualised_sharpe_zero_cash": series.mean() / series.std(ddof=1) * math.sqrt(12),
                    "positive_month_fraction": (series > 0).mean(),
                }
            )
            if period_name == "1990-2024":
                composite_monthly_rows.extend(
                    {
                        "date": date,
                        "composite": name,
                        "return": value,
                    }
                    for date, value in series.dropna().items()
                )
        if period_name == "1990-2024":
            periods = {
                "1990-1999": ("1990-01-01", "1999-12-31"),
                "2000-2009": ("2000-01-01", "2009-12-31"),
                "2010-2019": ("2010-01-01", "2019-12-31"),
                "2020-2024": ("2020-01-01", "2024-12-31"),
            }
            for subperiod, (start, end) in periods.items():
                for name, series in composites.items():
                    subset = series.loc[start:end]
                    temporal_rows.append(
                        {
                            "period": subperiod,
                            "composite": name,
                            "months": subset.notna().sum(),
                            "mean_monthly_return": subset.mean(),
                            "monthly_volatility": subset.std(ddof=1),
                            "positive_month_fraction": (subset > 0).mean(),
                        }
                    )

    connection = duckdb.connect()
    alternative_rows = []
    baseline_composite = principal[dedup_acronyms].mean(axis=1, skipna=True)
    for construction, path in paths.items():
        if construction == "published_long_short":
            continue
        frame = connection.execute(
            f"""
            SELECT signalname, CAST(date AS DATE) AS date, ret / 100.0 AS return
            FROM read_csv_auto('{normalise_path(path)}', all_varchar=false)
            WHERE CAST(port AS VARCHAR) = 'LS'
              AND CAST(date AS DATE) BETWEEN DATE '1990-01-01' AND DATE '2024-12-31'
            """
        ).fetchdf()
        pivot = frame.pivot(index="date", columns="signalname", values="return")
        matched = [value for value in dedup_acronyms if value in pivot.columns]
        composite = pivot[matched].mean(axis=1, skipna=True).sort_index()
        stats = newey_west_mean(composite, maxlags=3)
        common_dates = baseline_composite.index.intersection(pd.to_datetime(composite.index))
        comparison = pd.Series(composite.to_numpy(), index=pd.to_datetime(composite.index))
        correlation = baseline_composite.loc[common_dates].corr(comparison.loc[common_dates])
        signal_means = pivot[matched].mean()
        alternative_rows.append(
            {
                "construction": construction,
                "matched_signals": len(matched),
                "months": composite.notna().sum(),
                "mean_monthly_return": stats["mean"],
                "hac_t": stats["t"],
                "p_value": stats["p"],
                "monthly_volatility": composite.std(ddof=1),
                "annualised_sharpe_zero_cash": composite.mean()
                / composite.std(ddof=1)
                * math.sqrt(12),
                "positive_signal_means": int((signal_means > 0).sum()),
                "median_signal_mean": signal_means.median(),
                "correlation_with_published_composite": correlation,
            }
        )

    return {
        "structure": pd.DataFrame(structure_rows),
        "pairs": pd.DataFrame(pair_rows),
        "standalone": pd.concat(standalone_rows, ignore_index=True),
        "composites": pd.DataFrame(composite_rows),
        "composite_monthly": pd.DataFrame(composite_monthly_rows),
        "temporal": pd.DataFrame(temporal_rows),
        "alternative_constructions": pd.DataFrame(alternative_rows),
    }


def run_population_variant_job(job: dict[str, object]) -> dict[str, object]:
    population = str(job["population"])
    variant = str(job["variant"])
    identifiers = list(job["identifiers"])
    implementation_variant = job.get("implementation_variant")
    job_directory = INTERMEDIATE_DIR / "return_jobs"

    connection = duckdb.connect()
    connection.execute("SET threads TO 2")
    registry = pd.read_parquet(REGISTRY_PARQUET)
    labels = pd.read_parquet(RETURN_LABELS)
    labels["signal_date"] = pd.to_datetime(labels["signal_date"])
    if population == "us_domestic_fixed":
        labels = labels[labels["clean_us"]].copy()

    print(
        f"starting {population}/{variant}: {len(identifiers)} signals, "
        f"{labels['ticker'].nunique()} securities",
        flush=True,
    )
    results = analyse_population(
        connection,
        registry,
        labels,
        population,
        {variant: identifiers},
        implementation_variant=str(implementation_variant) if implementation_variant else None,
    )
    paths: dict[str, str] = {}
    prefix = population_job_prefix(population, variant)
    for name, frame in results.items():
        if frame.empty:
            continue
        path = job_directory / f"{prefix}__{name}.parquet"
        write_frame(frame, path)
        paths[name] = str(path)
    connection.close()
    print(f"completed {population}/{variant}", flush=True)
    return {
        "kind": "population",
        "population": population,
        "variant": variant,
        "paths": paths,
    }


def run_external_job() -> dict[str, object]:
    print("starting external Open Asset Pricing analysis", flush=True)
    os.chdir(Path(__file__).resolve().parents[1])
    registry = pd.read_parquet(REGISTRY_PARQUET)
    results = analyse_external(registry)
    job_directory = INTERMEDIATE_DIR / "return_jobs"
    paths: dict[str, str] = {}
    for name, frame in results.items():
        path = job_directory / f"external__{name}.parquet"
        write_frame(frame, path)
        paths[name] = str(path)
    print("completed external Open Asset Pricing analysis", flush=True)
    return {"kind": "external", "paths": paths}


def main() -> None:
    ensure_directories()
    for variable in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
        os.environ[variable] = "2"

    job_directory = INTERMEDIATE_DIR / "return_jobs"
    job_directory.mkdir(parents=True, exist_ok=True)
    for partial in job_directory.glob("*.part"):
        partial.unlink()

    connection = duckdb.connect()
    connection.execute("SET threads TO 6")
    registry = pd.read_parquet(REGISTRY_PARQUET)
    labels = pd.read_parquet(RETURN_LABELS)
    labels["signal_date"] = pd.to_datetime(labels["signal_date"])

    variants = dedupe_sets(registry)
    us_coverage = independently_screen_us(connection, registry)
    write_frame(us_coverage, RESULTS_DIR / "us_domestic_signal_coverage.csv")
    us_ids = us_coverage.loc[us_coverage["us_eligible"], "canonical_signal_id"].tolist()
    dedup_ids = set(
        registry.loc[
            registry["broad_eligible"] & registry["dedupe_representative"],
            "canonical_signal_id",
        ]
    )
    us_variants = {
        "us_eligible": us_ids,
        "us_deduplicated": [value for value in us_ids if value in dedup_ids],
        "us_native": us_coverage.loc[
            us_coverage["us_eligible"] & us_coverage["native"], "canonical_signal_id"
        ].tolist(),
    }
    us_labels = labels[labels["clean_us"]].copy()
    connection.close()

    jobs: list[dict[str, object]] = []
    for variant, identifiers in variants.items():
        jobs.append(
            {
                "population": "gbp_fixed",
                "variant": variant,
                "identifiers": identifiers,
                "implementation_variant": (
                    "broad_deduplicated" if variant == "broad_deduplicated" else None
                ),
            }
        )
    for variant, identifiers in us_variants.items():
        jobs.append(
            {
                "population": "us_domestic_fixed",
                "variant": variant,
                "identifiers": identifiers,
                "implementation_variant": None,
            }
        )

    result_keys = [
        "standalone_date",
        "primary_date",
        "marginal_date",
        "group_date",
        "composite_date",
        "allocation_weights",
        "implementation_date",
        "exposures",
        "anonymised_weights",
    ]
    standard_job_keys = {
        "standalone_date",
        "primary_date",
        "group_date",
        "composite_date",
        "allocation_weights",
    }
    job_results: list[dict[str, object]] = []
    pending_jobs: list[dict[str, object]] = []
    for job in jobs:
        population = str(job["population"])
        variant = str(job["variant"])
        expected = set(standard_job_keys)
        if population == "gbp_fixed" and variant == "broad_deduplicated":
            expected.update({"marginal_date", "implementation_date", "exposures", "anonymised_weights"})
        prefix = population_job_prefix(population, variant)
        paths = {
            name: str(job_directory / f"{prefix}__{name}.parquet")
            for name in expected
            if (job_directory / f"{prefix}__{name}.parquet").exists()
        }
        if set(paths) == expected:
            print(f"resuming completed {population}/{variant}", flush=True)
            job_results.append(
                {
                    "kind": "population",
                    "population": population,
                    "variant": variant,
                    "paths": paths,
                }
            )
        else:
            pending_jobs.append(job)

    external_names = {
        "structure",
        "pairs",
        "standalone",
        "composites",
        "composite_monthly",
        "temporal",
        "alternative_constructions",
    }
    external_paths = {
        name: str(job_directory / f"external__{name}.parquet")
        for name in external_names
        if (job_directory / f"external__{name}.parquet").exists()
    }
    external_pending = set(external_paths) != external_names
    if not external_pending:
        print("resuming completed external analysis", flush=True)
        job_results.append({"kind": "external", "paths": external_paths})

    pending_count = len(pending_jobs) + int(external_pending)
    worker_count = min(max(1, pending_count), max(2, (os.cpu_count() or 8) // 2))
    print(
        f"launching {pending_count} pending jobs with {worker_count} processes "
        "and two numerical threads per process",
        flush=True,
    )
    failures = []
    if pending_count:
        context = mp.get_context("spawn")
        with ProcessPoolExecutor(max_workers=worker_count, mp_context=context) as executor:
            futures = [executor.submit(run_population_variant_job, job) for job in pending_jobs]
            if external_pending:
                futures.append(executor.submit(run_external_job))
            for future in as_completed(futures):
                try:
                    result = future.result()
                except Exception as exc:
                    failures.append(repr(exc))
                    print(f"job failed: {exc!r}", flush=True)
                    continue
                job_results.append(result)
                if result["kind"] == "population":
                    print(
                        f"collected {result['population']}/{result['variant']}",
                        flush=True,
                    )
                else:
                    print("collected external analysis", flush=True)
    if failures:
        raise RuntimeError("parallel return-analysis failures: " + " | ".join(failures))

    combined = {}
    primary_variants = {
        "gbp_fixed": "broad",
        "us_domestic_fixed": "us_eligible",
    }
    population_results = [result for result in job_results if result["kind"] == "population"]
    for key in result_keys:
        frames = []
        for result in population_results:
            paths = result["paths"]
            if key not in paths:
                continue
            if key in {"standalone_date", "primary_date"} and result["variant"] != primary_variants[
                result["population"]
            ]:
                continue
            frames.append(pd.read_parquet(paths[key]))
        combined[key] = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
        if not combined[key].empty:
            write_frame(combined[key], RESULTS_DIR / f"local_{key}.parquet")
            if len(combined[key]) < 1_000_000:
                write_frame(combined[key], RESULTS_DIR / f"local_{key}.csv")

    standalone_ic = summarise_signal_series(combined["standalone_date"], "ic_21d", "ic_21d")
    standalone_spread = summarise_signal_series(
        combined["standalone_date"], "spread_21d", "spread_21d"
    )
    standalone_winsor = summarise_signal_series(
        combined["standalone_date"], "spread_21d_winsorised", "spread_21d_winsorised"
    )
    standalone_summary = standalone_ic.merge(
        standalone_spread,
        on=[
            "population",
            "canonical_signal_id",
            "oap_acronym",
            "group",
            "native",
            "dedupe_representative",
        ],
        how="outer",
        validate="one_to_one",
    ).merge(
        standalone_winsor,
        on=[
            "population",
            "canonical_signal_id",
            "oap_acronym",
            "group",
            "native",
            "dedupe_representative",
        ],
        how="outer",
        validate="one_to_one",
    )
    write_frame(standalone_summary, RESULTS_DIR / "local_standalone_summary.csv")

    primary_ic = summarise_signal_series(
        combined["primary_date"], "primary_ic", "primary_ic", "primary_horizon"
    )
    primary_spread = summarise_signal_series(
        combined["primary_date"], "primary_spread", "primary_spread", "primary_horizon"
    )
    primary_summary = primary_ic.merge(
        primary_spread,
        on=[
            "population",
            "canonical_signal_id",
            "oap_acronym",
            "group",
            "native",
            "dedupe_representative",
            "primary_horizon",
        ],
        how="outer",
        validate="one_to_one",
    )
    write_frame(primary_summary, RESULTS_DIR / "local_primary_horizon_summary.csv")

    marginal_summary = summarise_signal_series(
        combined["marginal_date"], "marginal_ic", "marginal_ic"
    )
    write_frame(marginal_summary, RESULTS_DIR / "local_marginal_summary.csv")
    write_frame(summarise_group_series(combined["group_date"]), RESULTS_DIR / "local_group_summary.csv")
    composite_summary = summarise_composites(combined["composite_date"])
    write_frame(composite_summary, RESULTS_DIR / "local_composite_summary.csv")
    write_frame(
        temporal_composites(combined["composite_date"]),
        RESULTS_DIR / "local_composite_temporal.csv",
    )

    external_result = next(result for result in job_results if result["kind"] == "external")
    external = {
        name: pd.read_parquet(path) for name, path in external_result["paths"].items()
    }
    for name, frame in external.items():
        write_frame(frame, RESULTS_DIR / f"external_{name}.csv")

    global_standalone = combined["standalone_date"][
        combined["standalone_date"]["population"] == "gbp_fixed"
    ]
    us_standalone = combined["standalone_date"][
        combined["standalone_date"]["population"] == "us_domestic_fixed"
    ]
    validation = {
        "global_population_securities": int(labels["ticker"].nunique()),
        "us_domestic_securities": int(us_labels["ticker"].nunique()),
        "us_eligible_signals": len(us_ids),
        "parallel_processes": worker_count,
        "jobs_completed": len(job_results),
        "global_standalone_rows": len(global_standalone),
        "us_standalone_rows": len(us_standalone),
        "composite_rows": len(combined["composite_date"]),
        "implementation_rows": len(combined["implementation_date"]),
        "external_matched_signals": int(external["standalone"]["oap_acronym"].nunique()),
        "all_global_signal_dates_present": int(
            global_standalone["signal_date"].nunique()
        ),
    }
    atomic_write_json(RESULTS_DIR / "return_analysis_validation.json", validation)
    print("return analysis complete")


if __name__ == "__main__":
    main()
