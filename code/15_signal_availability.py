from __future__ import annotations

import math

import duckdb
import numpy as np
import pandas as pd

from analysis_common import (
    INTERMEDIATE_DIR,
    REGISTRY_PARQUET,
    RESULTS_DIR,
    RETURN_LABELS,
    SCORE_PANEL,
    atomic_write_json,
    ensure_directories,
    newey_west_mean,
    normalise_path,
    rank_percentile,
    write_frame,
)


MINIMUM_DATE_CROSS_SECTION = 1_000


def information_coefficient(score: pd.Series, forward_return: pd.Series) -> float:
    frame = pd.concat(
        [score.rename("score"), forward_return.rename("return")], axis=1
    ).dropna()
    if len(frame) < 30 or frame["score"].nunique() < 2 or frame["return"].nunique() < 2:
        return math.nan
    return float(
        frame["score"].rank(method="average", pct=True).corr(
            frame["return"].rank(method="average", pct=True)
        )
    )


def quintile_legs(score: pd.Series, forward_return: pd.Series) -> tuple[float, float]:
    frame = pd.concat(
        [score.rename("score"), forward_return.rename("return")], axis=1
    ).dropna()
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
    current_aligned, previous_aligned = current.align(
        previous, join="outer", fill_value=0.0
    )
    return float(0.5 * (current_aligned - previous_aligned).abs().sum())


def availability_matrix(connection: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    counts = connection.execute(
        f"""
        SELECT
            CAST(signal_date AS DATE) AS signal_date,
            canonical_signal_id,
            COUNT(*) AS scored_securities
        FROM read_parquet('{normalise_path(SCORE_PANEL)}')
        GROUP BY 1, 2
        ORDER BY 1, 2
        """
    ).fetchdf()
    counts["signal_date"] = pd.to_datetime(counts["signal_date"])
    return (
        counts.pivot(
            index="signal_date",
            columns="canonical_signal_id",
            values="scored_securities",
        )
        .fillna(0)
        .sort_index()
    )


def activity_outputs(
    matrix: pd.DataFrame, representative_ids: list[str]
) -> tuple[pd.DataFrame, pd.DataFrame]:
    representative_ids = [value for value in representative_ids if value in matrix.columns]
    representatives = matrix[representative_ids]
    by_date = pd.DataFrame(
        {
            "signal_date": matrix.index,
            "active_broad_any_score": (matrix > 0).sum(axis=1).to_numpy(),
            "active_broad_at_least_1000": (
                matrix >= MINIMUM_DATE_CROSS_SECTION
            ).sum(axis=1).to_numpy(),
            "active_representatives_any_score": (representatives > 0)
            .sum(axis=1)
            .to_numpy(),
            "active_representatives_at_least_1000": (
                representatives >= MINIMUM_DATE_CROSS_SECTION
            )
            .sum(axis=1)
            .to_numpy(),
        }
    )
    by_date["year"] = by_date["signal_date"].dt.year

    annual_rows: list[dict[str, object]] = []
    for year, part in by_date.groupby("year", sort=True):
        row: dict[str, object] = {"period": str(year), "dates": len(part)}
        for column in (
            "active_broad_any_score",
            "active_broad_at_least_1000",
            "active_representatives_any_score",
            "active_representatives_at_least_1000",
        ):
            short = column.replace("active_", "")
            row[f"{short}_min"] = int(part[column].min())
            row[f"{short}_median"] = float(part[column].median())
            row[f"{short}_max"] = int(part[column].max())
        annual_rows.append(row)

    overall: dict[str, object] = {"period": "Full 121-date panel", "dates": len(by_date)}
    for column in (
        "active_broad_any_score",
        "active_broad_at_least_1000",
        "active_representatives_any_score",
        "active_representatives_at_least_1000",
    ):
        short = column.replace("active_", "")
        overall[f"{short}_min"] = int(by_date[column].min())
        overall[f"{short}_median"] = float(by_date[column].median())
        overall[f"{short}_max"] = int(by_date[column].max())
    summary = pd.DataFrame([overall, *annual_rows])
    return by_date, summary


def balanced_sensitivity(
    connection: duckdb.DuckDBPyConnection,
    matrix: pd.DataFrame,
    representative_ids: list[str],
) -> tuple[pd.DataFrame, pd.DataFrame, list[str]]:
    reported = pd.read_parquet(RESULTS_DIR / "local_composite_date.parquet")
    reported = reported[
        (reported["population"] == "gbp_fixed")
        & (reported["variant"] == "broad_deduplicated")
        & (reported["allocation"] == "equal_signal")
    ].copy()
    reported["signal_date"] = pd.to_datetime(reported["signal_date"])
    reported = reported.sort_values("signal_date")
    comparison_dates = pd.DatetimeIndex(reported["signal_date"])

    representatives = matrix.reindex(
        index=comparison_dates, columns=representative_ids, fill_value=0
    )
    balanced_ids = representatives.columns[
        (representatives >= MINIMUM_DATE_CROSS_SECTION).all(axis=0)
    ].tolist()

    labels = pd.read_parquet(
        RETURN_LABELS,
        columns=["ticker", "signal_date", "gbp_return_clean_21d"],
    )
    labels["signal_date"] = pd.to_datetime(labels["signal_date"])
    labels_by_date = {
        date: part.set_index("ticker")["gbp_return_clean_21d"].astype(float)
        for date, part in labels.groupby("signal_date")
    }

    identifiers_sql = ", ".join(
        "'" + value.replace("'", "''") + "'" for value in balanced_ids
    )
    balanced_scores = connection.execute(
        f"""
        SELECT
            ticker,
            CAST(signal_date AS DATE) AS signal_date,
            AVG(score_rank_pct) AS mean_score,
            COUNT(*) AS available_characteristics
        FROM read_parquet('{normalise_path(SCORE_PANEL)}')
        WHERE canonical_signal_id IN ({identifiers_sql})
        GROUP BY 1, 2
        ORDER BY 2, 1
        """
    ).fetchdf()
    balanced_scores["signal_date"] = pd.to_datetime(balanced_scores["signal_date"])
    scores_by_date = {
        date: part.set_index("ticker")
        for date, part in balanced_scores.groupby("signal_date")
    }

    rows: list[dict[str, object]] = []
    previous_target: pd.Series | None = None
    for signal_date in comparison_dates:
        score = scores_by_date[signal_date]
        y = labels_by_date[signal_date]
        common = score.index.intersection(y.index)
        score = score.loc[common]
        y = y.loc[common]
        portfolio_score = rank_percentile(score["mean_score"])
        valid = pd.concat([portfolio_score, y], axis=1).dropna()
        if len(valid) < MINIMUM_DATE_CROSS_SECTION:
            raise ValueError(
                f"Balanced set has only {len(valid)} matched securities on {signal_date.date()}"
            )
        top_return, bottom_return = quintile_legs(portfolio_score, y)
        spread = top_return - bottom_return
        target = target_weights(portfolio_score)
        turnover = target_turnover(target, previous_target)
        rows.append(
            {
                "signal_date": signal_date,
                "characteristics": len(balanced_ids),
                "matched_securities": len(valid),
                "ic": information_coefficient(portfolio_score, y),
                "spread": spread,
                "turnover_two_legs": turnover,
                "net_spread_25bps": (
                    spread - turnover * 25 / 10_000 if np.isfinite(turnover) else math.nan
                ),
            }
        )
        previous_target = target

    balanced_date = pd.DataFrame(rows)

    def summary_row(
        label: str, frame: pd.DataFrame, characteristics: int
    ) -> dict[str, object]:
        inference = newey_west_mean(frame["ic"], maxlags=1)
        return {
            "construction": label,
            "characteristics": characteristics,
            "dates": int(frame["ic"].notna().sum()),
            "mean_ic": float(frame["ic"].mean()),
            "ic_hac_t": float(inference["t"]),
            "mean_gross_spread": float(frame["spread"].mean()),
            "cost_dates": int(frame["net_spread_25bps"].notna().sum()),
            "mean_turnover_two_legs": float(frame["turnover_two_legs"].mean()),
            "mean_net_spread_25bps": float(frame["net_spread_25bps"].mean()),
        }

    sensitivity = pd.DataFrame(
        [
            summary_row(
                "Reported composite with characteristics available by date",
                reported,
                106,
            ),
            summary_row("Constant 103-date characteristic set", balanced_date, len(balanced_ids)),
        ]
    )
    return balanced_date, sensitivity, balanced_ids


def main() -> None:
    ensure_directories()
    connection = duckdb.connect()
    registry = pd.read_parquet(REGISTRY_PARQUET)
    representative_ids = registry.loc[
        registry["broad_eligible"] & registry["dedupe_representative"],
        "canonical_signal_id",
    ].tolist()
    matrix = availability_matrix(connection)
    by_date, summary = activity_outputs(matrix, representative_ids)
    balanced_date, sensitivity, balanced_ids = balanced_sensitivity(
        connection, matrix, representative_ids
    )
    comparison_activity = by_date[
        by_date["signal_date"].isin(pd.to_datetime(balanced_date["signal_date"]))
    ]
    comparison_row: dict[str, object] = {
        "period": "Reported 103-date return comparison",
        "dates": len(comparison_activity),
    }
    for column in (
        "active_broad_any_score",
        "active_broad_at_least_1000",
        "active_representatives_any_score",
        "active_representatives_at_least_1000",
    ):
        short = column.replace("active_", "")
        comparison_row[f"{short}_min"] = int(comparison_activity[column].min())
        comparison_row[f"{short}_median"] = float(comparison_activity[column].median())
        comparison_row[f"{short}_max"] = int(comparison_activity[column].max())
    summary = pd.concat(
        [summary.iloc[[0]], pd.DataFrame([comparison_row]), summary.iloc[1:]],
        ignore_index=True,
    )

    write_frame(by_date, RESULTS_DIR / "signal_availability_by_date.csv")
    write_frame(summary, RESULTS_DIR / "signal_availability_summary.csv")
    write_frame(balanced_date, RESULTS_DIR / "signal_availability_balanced_date.csv")
    write_frame(sensitivity, RESULTS_DIR / "signal_availability_sensitivity.csv")
    atomic_write_json(
        RESULTS_DIR / "signal_availability.json",
        {
            "source_dates": len(matrix),
            "source_characteristics": len(matrix.columns),
            "minimum_active_any_score": int((matrix > 0).sum(axis=1).min()),
            "median_active_any_score": float((matrix > 0).sum(axis=1).median()),
            "maximum_active_any_score": int((matrix > 0).sum(axis=1).max()),
            "comparison_dates": int(sensitivity.loc[0, "dates"]),
            "comparison_active_at_least_1000_min": int(
                comparison_activity["active_broad_at_least_1000"].min()
            ),
            "comparison_active_at_least_1000_median": float(
                comparison_activity["active_broad_at_least_1000"].median()
            ),
            "comparison_active_at_least_1000_max": int(
                comparison_activity["active_broad_at_least_1000"].max()
            ),
            "balanced_characteristics": len(balanced_ids),
            "balanced_definition": (
                "Deduplicated characteristics with at least 1,000 scored securities "
                "on every one of the 103 reported fixed-panel return dates"
            ),
            "selection_limitation": (
                "The constant set is selected using full-sample coverage and is a membership "
                "sensitivity, not a point-in-time portfolio rule."
            ),
        },
    )
    print(summary.to_string(index=False))
    print(sensitivity.to_string(index=False))


if __name__ == "__main__":
    main()
