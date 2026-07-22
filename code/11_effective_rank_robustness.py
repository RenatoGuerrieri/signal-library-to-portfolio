from __future__ import annotations

import numpy as np
import pandas as pd

from analysis_common import (
    INTERMEDIATE_DIR,
    RESULTS_DIR,
    atomic_write_json,
    ensure_directories,
    variant_identifiers,
    write_frame,
)


ARRAY_PATH = INTERMEDIATE_DIR / "score_structure_by_date.npz"


def rank_statistics(matrix: np.ndarray) -> dict[str, float | int]:
    current = np.asarray(matrix, dtype=float)
    current = (current + current.T) / 2.0
    eigenvalues = np.linalg.eigvalsh(current)
    negative = eigenvalues[eigenvalues < 0.0]
    clipped = np.clip(eigenvalues, 0.0, None)
    total = float(clipped.sum())
    weights = clipped / total
    positive = weights[weights > 0.0]
    return {
        "negative_eigenvalues": int((eigenvalues < -1e-12).sum()),
        "aggregate_negative_magnitude": float(-negative.sum()),
        "minimum_eigenvalue": float(eigenvalues.min()),
        "participation_ratio": float(total * total / np.square(clipped).sum()),
        "entropy_rank": float(np.exp(-(positive * np.log(positive)).sum())),
        "top_eigen_share": float(clipped.max() / total),
    }


def spectral_psd_correlation(matrix: np.ndarray) -> np.ndarray:
    """Return a correlation matrix after the nearest symmetric PSD projection."""
    current = np.asarray(matrix, dtype=float)
    current = (current + current.T) / 2.0
    np.fill_diagonal(current, 1.0)
    eigenvalues, eigenvectors = np.linalg.eigh(current)
    nearest_psd = (eigenvectors * np.clip(eigenvalues, 0.0, None)) @ eigenvectors.T
    diagonal = np.sqrt(np.maximum(np.diag(nearest_psd), 1e-15))
    correlation = nearest_psd / np.outer(diagonal, diagonal)
    correlation = (correlation + correlation.T) / 2.0
    np.fill_diagonal(correlation, 1.0)
    return correlation


def date_level_psd_average(correlation_dates: np.ndarray) -> tuple[np.ndarray, int]:
    """Average repaired date matrices, then normalise the aggregate diagonal."""
    signal_count = correlation_dates.shape[1]
    aggregate = np.zeros((signal_count, signal_count), dtype=float)
    used_dates = 0
    for date_matrix in correlation_dates:
        active = np.flatnonzero(np.isfinite(np.diag(date_matrix)))
        if len(active) < 2:
            continue
        submatrix = date_matrix[np.ix_(active, active)].astype(float)
        # Undefined relationships on an otherwise usable date carry no covariance
        # before the date matrix is projected to the PSD cone.
        submatrix = np.nan_to_num(submatrix, nan=0.0)
        repaired = spectral_psd_correlation(submatrix)
        aggregate[np.ix_(active, active)] += repaired
        used_dates += 1

    diagonal = np.diag(aggregate)
    scale = np.sqrt(np.outer(diagonal, diagonal))
    averaged = np.divide(
        aggregate,
        scale,
        out=np.zeros_like(aggregate),
        where=scale > 0.0,
    )
    np.fill_diagonal(averaged, 1.0)
    return (averaged + averaged.T) / 2.0, used_dates


def main() -> None:
    ensure_directories()
    arrays = np.load(ARRAY_PATH, allow_pickle=False)
    signal_ids = arrays["signal_ids"].astype(str).tolist()
    correlation_dates = arrays["correlation"].astype(float)
    registry = pd.read_csv(RESULTS_DIR / "signal_registry.csv")
    positions = {signal_id: index for index, signal_id in enumerate(signal_ids)}

    averaged_date_matrix, used_dates = date_level_psd_average(correlation_dates)
    rows: list[dict[str, object]] = []
    for variant in ("broad", "broad_deduplicated", "native"):
        identifiers = variant_identifiers(registry)[variant]
        indices = np.asarray([positions[value] for value in identifiers], dtype=int)
        date_submatrices = correlation_dates[:, indices][:, :, indices]

        constructions = {
            "pairwise_time_mean": np.nanmean(date_submatrices, axis=0),
            "pairwise_time_median": np.nanmedian(date_submatrices, axis=0),
            "date_matrix_psd_average": averaged_date_matrix[np.ix_(indices, indices)],
        }
        for construction, raw_matrix in constructions.items():
            raw_matrix = (raw_matrix + raw_matrix.T) / 2.0
            np.fill_diagonal(raw_matrix, 1.0)
            raw = rank_statistics(raw_matrix)
            repaired_matrix = (
                raw_matrix
                if construction == "date_matrix_psd_average"
                else spectral_psd_correlation(raw_matrix)
            )
            repaired = rank_statistics(repaired_matrix)
            rows.append(
                {
                    "variant": variant,
                    "characteristics": len(indices),
                    "construction": construction,
                    "date_matrices": used_dates if construction == "date_matrix_psd_average" else len(correlation_dates),
                    "negative_eigenvalues_before_repair": raw["negative_eigenvalues"],
                    "aggregate_negative_magnitude_before_repair": raw["aggregate_negative_magnitude"],
                    "minimum_eigenvalue_before_repair": raw["minimum_eigenvalue"],
                    "participation_ratio_after_repair": repaired["participation_ratio"],
                    "entropy_rank_after_repair": repaired["entropy_rank"],
                    "top_eigen_share_after_repair": repaired["top_eigen_share"],
                    "minimum_eigenvalue_after_repair": repaired["minimum_eigenvalue"],
                }
            )

    output = pd.DataFrame(rows)
    write_frame(output, RESULTS_DIR / "effective_rank_robustness.csv")
    primary = output[
        (output["variant"] == "broad")
        & (output["construction"] == "pairwise_time_mean")
    ].iloc[0]
    atomic_write_json(
        RESULTS_DIR / "effective_rank_robustness.json",
        {
            "rows": len(output),
            "date_matrices_available": int(len(correlation_dates)),
            "date_matrices_used_in_psd_average": int(used_dates),
            "primary_negative_eigenvalues": int(primary.negative_eigenvalues_before_repair),
            "primary_aggregate_negative_magnitude": float(
                primary.aggregate_negative_magnitude_before_repair
            ),
            "primary_rank_stable_between_36_and_39": bool(
                output[
                    (output["variant"] == "broad")
                    & output["construction"].isin(
                        [
                            "pairwise_time_mean",
                            "pairwise_time_median",
                            "date_matrix_psd_average",
                        ]
                    )
                ]["participation_ratio_after_repair"].between(36.0, 39.0).all()
            ),
        },
    )
    print(output.to_string(index=False))


if __name__ == "__main__":
    main()
