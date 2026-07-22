from __future__ import annotations

import argparse
import math
import time
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

from analysis_common import (
    ARCHIVED_PRICE_PATH,
    FEASIBILITY_PATH,
    HORIZONS,
    INTERMEDIATE_DIR,
    INVENTORY_PATH,
    PANEL_PATH,
    PRICE_VALIDATION,
    PRICES_PROD,
    REGISTRY_PARQUET,
    RESULTS_DIR,
    RETURN_LABELS,
    ROOT,
    SCORE_PANEL,
    SOURCE_DIR,
    SPEC_DIR,
    UNIVERSE_PATH,
    atomic_write_json,
    ensure_directories,
    environment_record,
    normalise_path,
    sha256_file,
    write_frame,
)


def build_registry(connection: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    panel = normalise_path(PANEL_PATH)
    feasibility = normalise_path(FEASIBILITY_PATH)
    query = f"""
        WITH by_date AS (
            SELECT
                canonical_signal_id,
                signal_date,
                COUNT(*) AS cross_section
            FROM read_parquet('{panel}')
            GROUP BY 1, 2
        ), coverage AS (
            SELECT
                canonical_signal_id,
                COUNT(*) AS date_count,
                MEDIAN(cross_section) AS median_cross_section,
                MIN(signal_date) AS first_signal_date,
                MAX(signal_date) AS last_signal_date
            FROM by_date
            GROUP BY 1
        ), metadata AS (
            SELECT
                canonical_signal_id,
                ANY_VALUE(oap_acronym) AS oap_acronym,
                ANY_VALUE(local_formula_id) AS local_formula_id,
                ANY_VALUE(local_formula_quality) AS local_formula_quality,
                ANY_VALUE(signal_family) AS signal_family,
                ANY_VALUE(cat_data) AS cat_data,
                ANY_VALUE(primary_horizon) AS primary_horizon,
                ANY_VALUE(formula_version) AS formula_version,
                ANY_VALUE(source_sign) AS source_sign
            FROM read_parquet('{panel}')
            GROUP BY 1
        ), feasibility AS (
            SELECT
                canonical_signal_id,
                ANY_VALUE(signal_rep_quality) AS signal_rep_quality,
                BOOL_OR(COALESCE(source_code_file_exists, FALSE)) AS source_code_file_exists,
                ANY_VALUE(authors) AS authors,
                ANY_VALUE(year) AS publication_year,
                ANY_VALUE(stock_weight) AS source_stock_weight,
                ANY_VALUE(portfolio_period) AS source_portfolio_period
            FROM read_parquet('{feasibility}')
            GROUP BY 1
        )
        SELECT m.*, c.*, f.signal_rep_quality, f.source_code_file_exists,
               f.authors, f.publication_year, f.source_stock_weight,
               f.source_portfolio_period
        FROM metadata m
        JOIN coverage c USING (canonical_signal_id)
        LEFT JOIN feasibility f USING (canonical_signal_id)
        ORDER BY m.canonical_signal_id
    """
    registry = connection.execute(query).fetchdf()
    registry["native"] = registry["local_formula_quality"].fillna("").str.startswith("native_")
    registry["broad_eligible"] = (
        (registry["date_count"] >= 80) & (registry["median_cross_section"] >= 1_000)
    )
    registry["quality_order"] = (
        registry["signal_rep_quality"]
        .fillna("99_unknown")
        .str.extract(r"^(\d+)", expand=False)
        .fillna("99")
        .astype(int)
    )
    registry["source_code_file_exists"] = registry["source_code_file_exists"].fillna(False)
    registry["dedupe_representative"] = False

    eligible = registry[registry["broad_eligible"]].copy()
    for _, members in eligible.groupby("local_formula_id", dropna=False):
        ordered = members.sort_values(
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
        registry.loc[ordered.index[0], "dedupe_representative"] = True

    registry["sample_role"] = np.select(
        [
            registry["broad_eligible"] & registry["native"],
            registry["broad_eligible"],
        ],
        ["native_eligible", "proxy_eligible"],
        default="coverage_ineligible",
    )
    return registry.drop(columns=["quality_order"])


def build_score_panel(connection: duckdb.DuckDBPyConnection, registry: pd.DataFrame) -> None:
    connection.register("registry_frame", registry[["canonical_signal_id", "broad_eligible"]])
    output = normalise_path(SCORE_PANEL)
    panel = normalise_path(PANEL_PATH)
    connection.execute(
        f"""
        COPY (
            SELECT
                p.ticker,
                CAST(p.signal_date AS DATE) AS signal_date,
                CAST(p.feature_asof_date AS DATE) AS feature_asof_date,
                p.canonical_signal_id,
                p.oap_acronym,
                p.local_formula_id,
                p.local_formula_quality,
                p.signal_family,
                p.cat_data,
                p.primary_horizon,
                p.region,
                p.country,
                p.sector,
                p.industry,
                p.exchange,
                p.score_rank_pct,
                p.score_z,
                p.close,
                p.volume,
                p.data_quality_flag
            FROM read_parquet('{panel}') p
            JOIN registry_frame r USING (canonical_signal_id)
            WHERE r.broad_eligible
        ) TO '{output}' (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 500000)
        """
    )


def source_manifest() -> pd.DataFrame:
    external_root = SOURCE_DIR / "open_asset_pricing_public"
    files = [
        ("characteristic_panel", PANEL_PATH),
        ("local_feasibility", FEASIBILITY_PATH),
        ("source_inventory", INVENTORY_PATH),
        ("archived_price_panel", ARCHIVED_PRICE_PATH),
        ("source_universe", UNIVERSE_PATH),
        ("price_validation", PRICE_VALIDATION),
        ("empirical_specification", SPEC_DIR / "FINAL_EMPIRICAL_SPECIFICATION.md"),
        ("initial_manuscript_frozen", ROOT / "07_manuscript" / "initial_manuscript_frozen.md"),
    ]
    if external_root.exists():
        for path in sorted(external_root.rglob("*")):
            if path.is_file() and not path.name.endswith(".part"):
                files.append(("open_asset_pricing_public", path))
    rows = []
    for role, path in files:
        if not path.exists():
            rows.append({"role": role, "path": str(path), "exists": False})
            continue
        rows.append(
            {
                "role": role,
                "path": str(path),
                "exists": True,
                "bytes": path.stat().st_size,
                "modified_utc": pd.Timestamp(path.stat().st_mtime, unit="s", tz="UTC"),
                "sha256": sha256_file(path),
            }
        )
    return pd.DataFrame(rows)


class SterlingConverter:
    def __init__(self) -> None:
        self.cache: dict[str, pd.Series | None] = {}

    @staticmethod
    def currency_terms(currency: object) -> tuple[str | None, float]:
        value = str(currency) if pd.notna(currency) else ""
        aliases = {"ILA": "ILS", "ZAc": "ZAR", "KWF": "KWD"}
        multipliers = {"GBp": 0.01, "ILA": 0.01, "ZAc": 0.01}
        if value == "GBP":
            return "GBP", 1.0
        if value == "GBp":
            return "GBP", 0.01
        return aliases.get(value, value or None), multipliers.get(value, 1.0)

    @staticmethod
    def _read_rate(path: Path) -> pd.Series | None:
        if not path.exists():
            return None
        frame = pd.read_parquet(path, columns=["date", "total_return_adj_close"])
        frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
        frame["total_return_adj_close"] = pd.to_numeric(
            frame["total_return_adj_close"], errors="coerce"
        )
        frame = frame.dropna().query("total_return_adj_close > 0").drop_duplicates("date", keep="last")
        if frame.empty:
            return None
        return frame.set_index("date")["total_return_adj_close"].sort_index()

    def rate(self, currency: object) -> tuple[pd.Series | None, float, str]:
        alias, multiplier = self.currency_terms(currency)
        if alias is None:
            return None, multiplier, "missing_currency"
        if alias == "GBP":
            return None, multiplier, "sterling"
        if alias in self.cache:
            return self.cache[alias], multiplier, "direct_or_cross"

        direct = self._read_rate(PRICES_PROD / f"GBP{alias}=X.parquet")
        if direct is not None:
            self.cache[alias] = direct
            return direct, multiplier, "direct_gbp_cross"

        gbp_usd = self._read_rate(PRICES_PROD / "GBPUSD=X.parquet")
        usd_local = self._read_rate(PRICES_PROD / f"USD{alias}=X.parquet")
        if gbp_usd is not None and usd_local is not None:
            combined = pd.concat([gbp_usd.rename("gbp_usd"), usd_local.rename("usd_local")], axis=1)
            combined = combined.sort_index().ffill(limit=5).dropna()
            result = combined["gbp_usd"] * combined["usd_local"]
            self.cache[alias] = result
            return result, multiplier, "usd_cross"
        self.cache[alias] = None
        return None, multiplier, "missing_fx_cross"

    @staticmethod
    def align(series: pd.Series | None, dates: pd.DatetimeIndex) -> tuple[np.ndarray, np.ndarray]:
        if series is None:
            return np.ones(len(dates), dtype=float), np.zeros(len(dates), dtype=float)
        fx_dates = series.index.values.astype("datetime64[ns]")
        target = dates.values.astype("datetime64[ns]")
        locations = np.searchsorted(fx_dates, target, side="right") - 1
        rates = np.full(len(target), np.nan, dtype=float)
        stale = np.full(len(target), np.nan, dtype=float)
        valid = locations >= 0
        rates[valid] = series.to_numpy()[locations[valid]]
        stale[valid] = (target[valid] - fx_dates[locations[valid]]) / np.timedelta64(1, "D")
        rates[(stale > 5) | (rates <= 0)] = np.nan
        return rates, stale


def build_return_labels() -> tuple[pd.DataFrame, pd.DataFrame]:
    universe = pd.read_parquet(UNIVERSE_PATH)
    universe = universe[universe["selected_for_retest"].fillna(False)].copy()
    universe = universe.sort_values("target_rank")
    signal_dates = pd.DatetimeIndex(
        pd.read_parquet(SCORE_PANEL, columns=["signal_date"])["signal_date"].drop_duplicates().sort_values()
    )
    converter = SterlingConverter()
    recognised_us_exchanges = {"NYSE", "NASDAQ", "AMEX", "CBOE"}
    records: list[dict[str, object]] = []
    audit: list[dict[str, object]] = []
    started = time.time()

    for sequence, row in enumerate(universe.itertuples(index=False), start=1):
        ticker = str(row.ticker)
        currency = row.currency
        price_path = PRICES_PROD / f"{ticker}.parquet"
        base_audit = {
            "ticker": ticker,
            "currency": currency,
            "price_path": str(price_path),
            "price_file_exists": price_path.exists(),
        }
        if not price_path.exists():
            base_audit["status"] = "missing_price_file"
            audit.append(base_audit)
            continue
        try:
            price = pd.read_parquet(
                price_path,
                columns=[
                    "date",
                    "total_return_adj_close",
                    "close_split_adj",
                    "volume_split_adj",
                    "validation_status",
                ],
            )
        except Exception as exc:
            base_audit["status"] = "price_read_error"
            base_audit["error"] = str(exc)
            audit.append(base_audit)
            continue
        price["date"] = pd.to_datetime(price["date"], errors="coerce")
        for column in ("total_return_adj_close", "close_split_adj", "volume_split_adj"):
            price[column] = pd.to_numeric(price[column], errors="coerce")
        price = price.dropna(subset=["date", "total_return_adj_close"])
        price = price[price["total_return_adj_close"] > 0]
        price = price.drop_duplicates("date", keep="last").sort_values("date")
        if len(price) < 300:
            base_audit["status"] = "insufficient_price_history"
            base_audit["price_rows"] = len(price)
            audit.append(base_audit)
            continue

        fx, multiplier, fx_method = converter.rate(currency)
        alias, _ = converter.currency_terms(currency)
        if alias != "GBP" and fx is None:
            base_audit["status"] = "missing_fx"
            base_audit["fx_method"] = fx_method
            audit.append(base_audit)
            continue
        dates = pd.DatetimeIndex(price["date"])
        rates, staleness = converter.align(fx, dates)
        local_total_return_price = price["total_return_adj_close"].to_numpy(dtype=float)
        sterling_total_return_price = local_total_return_price * multiplier / rates
        local_notional = (
            price["close_split_adj"].to_numpy(dtype=float)
            * price["volume_split_adj"].to_numpy(dtype=float)
        )
        sterling_notional = local_notional * multiplier / rates
        rolling_adv = pd.Series(sterling_notional).rolling(60, min_periods=20).median().to_numpy()

        local_ratio = np.full(len(price), np.nan, dtype=float)
        sterling_ratio = np.full(len(price), np.nan, dtype=float)
        local_ratio[1:] = local_total_return_price[1:] / local_total_return_price[:-1]
        sterling_ratio[1:] = sterling_total_return_price[1:] / sterling_total_return_price[:-1]
        scale_break = np.zeros(len(price), dtype=int)
        scale_break[1:] = (
            (~np.isfinite(local_ratio[1:]))
            | (~np.isfinite(sterling_ratio[1:]))
            | (local_ratio[1:] < 0.2)
            | (local_ratio[1:] > 5.0)
            | (sterling_ratio[1:] < 0.2)
            | (sterling_ratio[1:] > 5.0)
        ).astype(int)
        scale_break_cumulative = np.cumsum(scale_break)

        date_values = dates.values.astype("datetime64[ns]")
        signal_values = signal_dates.values.astype("datetime64[ns]")
        entry_locations = np.searchsorted(date_values, signal_values, side="right")
        liquidity_locations = entry_locations - 1
        clean_us = bool(
            str(row.country) == "US"
            and str(currency) == "USD"
            and str(row.security_type) == "EQUITY_OR_COMPANY"
            and not bool(row.isEtf)
            and not bool(row.isFund)
            and str(row.exchange) in recognised_us_exchanges
        )

        for signal_index, signal_date in enumerate(signal_dates):
            entry = int(entry_locations[signal_index])
            liquidity_location = int(liquidity_locations[signal_index])
            result: dict[str, object] = {
                "ticker": ticker,
                "signal_date": signal_date,
                "currency": currency,
                "country": row.country,
                "region": row.region,
                "sector": row.sector,
                "exchange": row.exchange,
                "security_type": row.security_type,
                "clean_us": clean_us,
                "fx_method": fx_method,
                "entry_date": pd.NaT,
                "entry_fx_staleness_days": math.nan,
                "adv60_gbp": math.nan,
            }
            if 0 <= liquidity_location < len(rolling_adv):
                result["adv60_gbp"] = rolling_adv[liquidity_location]
            if entry >= len(price):
                for horizon in HORIZONS:
                    result[f"local_return_{horizon}d"] = math.nan
                    result[f"gbp_return_{horizon}d"] = math.nan
                    result[f"local_return_clean_{horizon}d"] = math.nan
                    result[f"gbp_return_clean_{horizon}d"] = math.nan
                    result[f"window_scale_break_count_{horizon}d"] = math.nan
                records.append(result)
                continue
            result["entry_date"] = dates[entry]
            result["entry_fx_staleness_days"] = staleness[entry]
            for horizon in HORIZONS:
                exit_location = entry + horizon
                if exit_location >= len(price):
                    result[f"local_return_{horizon}d"] = math.nan
                    result[f"gbp_return_{horizon}d"] = math.nan
                    result[f"local_return_clean_{horizon}d"] = math.nan
                    result[f"gbp_return_clean_{horizon}d"] = math.nan
                    result[f"window_scale_break_count_{horizon}d"] = math.nan
                    continue
                local_return = (
                    local_total_return_price[exit_location] / local_total_return_price[entry] - 1.0
                )
                result[f"local_return_{horizon}d"] = local_return
                sterling_return = math.nan
                if np.isfinite(sterling_total_return_price[[entry, exit_location]]).all():
                    sterling_return = (
                        sterling_total_return_price[exit_location]
                        / sterling_total_return_price[entry]
                        - 1.0
                    )
                result[f"gbp_return_{horizon}d"] = sterling_return
                break_count = int(
                    scale_break_cumulative[exit_location] - scale_break_cumulative[entry]
                )
                result[f"window_scale_break_count_{horizon}d"] = break_count
                result[f"local_return_clean_{horizon}d"] = (
                    local_return if break_count == 0 else math.nan
                )
                result[f"gbp_return_clean_{horizon}d"] = (
                    sterling_return if break_count == 0 else math.nan
                )
            records.append(result)

        base_audit.update(
            {
                "status": "ok",
                "price_rows": len(price),
                "first_price_date": dates.min(),
                "last_price_date": dates.max(),
                "fx_method": fx_method,
                "currency_alias": alias,
                "currency_multiplier": multiplier,
                "clean_us": clean_us,
            }
        )
        audit.append(base_audit)
        if sequence % 250 == 0:
            elapsed = time.time() - started
            print(f"prepared labels for {sequence:,}/{len(universe):,} securities in {elapsed:,.1f}s")

    return pd.DataFrame(records), pd.DataFrame(audit)


def label_quality(labels: pd.DataFrame, audit: pd.DataFrame) -> dict[str, object]:
    report: dict[str, object] = {
        "rows": len(labels),
        "securities": labels["ticker"].nunique() if len(labels) else 0,
        "signal_dates": labels["signal_date"].nunique() if len(labels) else 0,
        "clean_us_securities": labels.loc[labels["clean_us"], "ticker"].nunique() if len(labels) else 0,
        "audit_status": audit["status"].value_counts(dropna=False).to_dict(),
        "currency_counts": labels.drop_duplicates("ticker")["currency"].value_counts(dropna=False).to_dict()
        if len(labels)
        else {},
    }
    for horizon in HORIZONS:
        for prefix in ("gbp_return", "gbp_return_clean"):
            column = f"{prefix}_{horizon}d"
            values = labels[column]
            report[column] = {
                "non_missing": int(values.notna().sum()),
                "missing": int(values.isna().sum()),
                "minimum": float(values.min()) if values.notna().any() else math.nan,
                "maximum": float(values.max()) if values.notna().any() else math.nan,
                "absolute_above_100pct": int((values.abs() > 1.0).sum()),
                "absolute_above_300pct": int((values.abs() > 3.0).sum()),
            }
        breaks = labels[f"window_scale_break_count_{horizon}d"]
        report[f"scale_break_{horizon}d"] = {
            "excluded_windows": int((breaks > 0).sum()),
            "affected_securities": int(
                labels.loc[breaks > 0, "ticker"].nunique()
            ),
        }
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reuse-labels", action="store_true")
    parser.add_argument("--reuse-scores", action="store_true")
    args = parser.parse_args()
    ensure_directories()
    connection = duckdb.connect()
    connection.execute("SET threads TO 8")
    connection.execute("SET preserve_insertion_order=false")

    registry = build_registry(connection)
    write_frame(registry, REGISTRY_PARQUET)
    write_frame(registry, RESULTS_DIR / "signal_registry.csv")
    duplicate_candidates = registry[
        registry["broad_eligible"]
        & registry.duplicated("local_formula_id", keep=False)
    ].sort_values(["local_formula_id", "canonical_signal_id"])
    write_frame(duplicate_candidates, RESULTS_DIR / "formula_duplicate_candidates.csv")

    if not args.reuse_scores or not SCORE_PANEL.exists():
        print("building compact broad score panel")
        build_score_panel(connection, registry)
    score_audit = connection.execute(
        f"""
        SELECT COUNT(*) row_count, COUNT(DISTINCT canonical_signal_id) signals,
               COUNT(DISTINCT ticker) securities, COUNT(DISTINCT signal_date) signal_dates,
               MIN(signal_date) first_date, MAX(signal_date) last_date,
               SUM((score_rank_pct IS NULL)::INT) missing_scores,
               COUNT(*) - COUNT(DISTINCT (ticker, signal_date, canonical_signal_id)) duplicate_keys
        FROM read_parquet('{normalise_path(SCORE_PANEL)}')
        """
    ).fetchdf()
    write_frame(score_audit, RESULTS_DIR / "score_panel_audit.csv")

    if args.reuse_labels and RETURN_LABELS.exists():
        labels = pd.read_parquet(RETURN_LABELS)
        audit_path = RESULTS_DIR / "price_fx_security_audit.csv"
        audit = pd.read_csv(audit_path) if audit_path.exists() else pd.DataFrame()
    else:
        labels, audit = build_return_labels()
        write_frame(labels, RETURN_LABELS)
        write_frame(audit, RESULTS_DIR / "price_fx_security_audit.csv")
    atomic_write_json(RESULTS_DIR / "return_label_quality.json", label_quality(labels, audit))

    manifest = source_manifest()
    write_frame(manifest, SPEC_DIR / "source_manifest.csv")
    atomic_write_json(SPEC_DIR / "environment.json", environment_record())
    print("preparation complete")


if __name__ == "__main__":
    main()
