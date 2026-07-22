from __future__ import annotations

from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from analysis_common import FIGURES_DIR, RESULTS_DIR, ensure_directories


NAVY = "#102A43"
BLUE = "#2F6FB0"
LIGHT_BLUE = "#82ADD7"
GOLD = "#C99A2E"
BURGUNDY = "#9F3A45"
GREY = "#6B7280"
LIGHT_GREY = "#D7DEE7"
GROUP_COLOURS = {
    "Accounting": NAVY,
    "Event": BURGUNDY,
    "Other": GOLD,
    "Price": BLUE,
    "Trading": "#3E7C6A",
}


def style() -> None:
    plt.rcParams.update(
        {
            "font.family": "DejaVu Sans",
            "font.size": 9,
            "axes.titlesize": 12,
            "axes.titleweight": "bold",
            "axes.labelsize": 9,
            "axes.edgecolor": "#8A97A6",
            "axes.linewidth": 0.8,
            "xtick.color": "#374151",
            "ytick.color": "#374151",
            "text.color": NAVY,
            "figure.facecolor": "white",
            "axes.facecolor": "white",
            "axes.grid": True,
            "grid.color": "#E7EBF0",
            "grid.linewidth": 0.7,
            "grid.alpha": 1.0,
            "legend.frameon": False,
        }
    )


def save(fig: plt.Figure, name: str) -> None:
    fig.savefig(FIGURES_DIR / f"{name}.png", dpi=240, bbox_inches="tight", facecolor="white")
    fig.savefig(FIGURES_DIR / f"{name}.svg", bbox_inches="tight", facecolor="white")
    plt.close(fig)


def compression_figure() -> None:
    local = pd.read_csv(RESULTS_DIR / "score_structure_summary.csv").set_index("variant")
    external = pd.read_csv(RESULTS_DIR / "external_structure.csv").set_index("variant")
    panels = [
        (
            "Local score structure, 2016-2026",
            [
                local.loc["broad", "characteristics"],
                local.loc["broad", "participation_ratio"],
                local.loc["broad", "entropy_rank"],
            ],
        ),
        (
            "Public long-short returns, 1990-2024",
            [
                external.loc["broad", "characteristics"],
                external.loc["broad", "participation_ratio"],
                external.loc["broad", "entropy_rank"],
            ],
        ),
    ]
    labels = ["Catalogue count", "Participation rank", "Entropy rank"]
    fig, axes = plt.subplots(1, 2, figsize=(10.5, 4.6), sharey=True)
    for axis, (title, values) in zip(axes, panels):
        bars = axis.bar(labels, values, color=[NAVY, BLUE, GOLD], width=0.62)
        axis.set_title(title, pad=12)
        axis.set_ylim(0, 122)
        axis.set_ylabel("Number of effective dimensions" if axis is axes[0] else "")
        axis.tick_params(axis="x", rotation=12)
        for bar, value in zip(bars, values):
            axis.text(
                bar.get_x() + bar.get_width() / 2,
                bar.get_height() + 2.5,
                f"{value:.1f}" if value != 112 else "112",
                ha="center",
                va="bottom",
                fontweight="bold",
                color=NAVY,
            )
        axis.spines[["top", "right"]].set_visible(False)
    fig.suptitle("Catalogue size materially exceeds independent breadth", fontsize=14, fontweight="bold")
    fig.tight_layout(rect=[0, 0, 1, 0.94])
    save(fig, "figure_1_library_compression")


def pair_structure_figure() -> None:
    pairs = pd.read_csv(RESULTS_DIR / "score_pair_metrics_private.csv")
    absolute = pairs["mean_score_correlation"].abs().dropna()
    fig, axes = plt.subplots(1, 2, figsize=(10.8, 4.6))
    axes[0].hist(absolute, bins=np.linspace(0, 1, 41), color=BLUE, edgecolor="white")
    for threshold, colour in ((0.7, GOLD), (0.8, BURGUNDY)):
        axes[0].axvline(threshold, color=colour, linewidth=1.5, linestyle="--")
    axes[0].set_title("Absolute pairwise score correlation")
    axes[0].set_xlabel("Time-average absolute Spearman correlation")
    axes[0].set_ylabel("Signal pairs")
    axes[0].spines[["top", "right"]].set_visible(False)

    family_data = [
        pairs.loc[pairs["same_family"], "mean_score_correlation"].abs().dropna(),
        pairs.loc[~pairs["same_family"], "mean_score_correlation"].abs().dropna(),
    ]
    box = axes[1].boxplot(
        family_data,
        tick_labels=["Same family", "Different family"],
        showfliers=False,
        patch_artist=True,
        widths=0.55,
    )
    for patch, colour in zip(box["boxes"], [GOLD, LIGHT_BLUE]):
        patch.set_facecolor(colour)
        patch.set_edgecolor(NAVY)
    for median in box["medians"]:
        median.set_color(NAVY)
        median.set_linewidth(1.5)
    axes[1].set_title("Economic labels identify clusters, not breadth")
    axes[1].set_ylabel("Absolute Spearman correlation")
    axes[1].spines[["top", "right"]].set_visible(False)
    fig.suptitle("Redundancy is concentrated rather than uniform", fontsize=14, fontweight="bold")
    fig.tight_layout(rect=[0, 0, 1, 0.94])
    save(fig, "figure_2_pair_structure")


def marginal_figure() -> None:
    standalone = pd.read_csv(RESULTS_DIR / "local_standalone_summary.csv")
    marginal = pd.read_csv(RESULTS_DIR / "local_marginal_summary.csv")
    frame = marginal.merge(
        standalone[
            ["population", "canonical_signal_id", "ic_21d_mean"]
        ],
        on=["population", "canonical_signal_id"],
        how="left",
        validate="one_to_one",
    )
    fig, axis = plt.subplots(figsize=(8.3, 5.6))
    for group, part in frame.groupby("group"):
        axis.scatter(
            part["ic_21d_mean"],
            part["marginal_ic_mean"],
            s=34,
            alpha=0.78,
            color=GROUP_COLOURS.get(group, GREY),
            label=group,
            edgecolor="white",
            linewidth=0.4,
        )
    axis.axhline(0, color="#374151", linewidth=1)
    axis.axvline(0, color="#374151", linewidth=1)
    count = int(((frame["ic_21d_mean"] > 0) & (frame["marginal_ic_mean"] < 0)).sum())
    axis.text(
        0.98,
        0.05,
        f"{count} signals are positive alone\nbut negative at the margin",
        transform=axis.transAxes,
        ha="right",
        va="bottom",
        color=BURGUNDY,
        fontweight="bold",
    )
    axis.set_title("Standalone evidence does not determine portfolio contribution")
    axis.set_xlabel("Mean standalone 21-session rank IC")
    axis.set_ylabel("Mean leave-one-out marginal rank IC")
    axis.legend(ncol=3, loc="upper left")
    axis.spines[["top", "right"]].set_visible(False)
    fig.tight_layout()
    save(fig, "figure_3_standalone_vs_marginal")


def allocation_figure() -> None:
    bootstrap = pd.read_csv(RESULTS_DIR / "local_composite_common_window_bootstrap.csv")
    selections = {
        "Fixed panel in sterling": ("gbp_fixed", "broad_deduplicated"),
        "Domestic US subset": ("us_domestic_fixed", "us_deduplicated"),
    }
    allocations = [
        "equal_signal",
        "equal_group",
        "inverse_redundancy",
        "trailing_positive_evidence",
    ]
    labels = ["Equal signal", "Equal group", "Inverse redundancy", "Trailing rule"]
    colours = [NAVY, BLUE, GOLD, BURGUNDY]
    fig, axes = plt.subplots(2, 2, figsize=(11.0, 7.2), sharex="col")
    for column, (panel, (population, variant)) in enumerate(selections.items()):
        for row, (metric, title, scale) in enumerate(
            (("ic", "Mean rank IC", 1.0), ("net_spread_25bps", "Mean spread after 25 bp cost", 100.0))
        ):
            axis = axes[row, column]
            estimates = []
            low_errors = []
            high_errors = []
            for allocation in allocations:
                b = bootstrap[
                    (bootstrap["population"] == population)
                    & (bootstrap["variant"] == variant)
                    & (bootstrap["allocation"] == allocation)
                    & (bootstrap["metric"] == metric)
                    & (bootstrap["block_length_months"] == 6)
                ].iloc[0]
                estimates.append(b["estimate"] * scale)
                low_errors.append((b["estimate"] - b["ci_2_5"]) * scale)
                high_errors.append((b["ci_97_5"] - b["estimate"]) * scale)
            x = np.arange(len(allocations))
            axis.bar(x, estimates, color=colours, width=0.65)
            axis.errorbar(
                x,
                estimates,
                yerr=np.array([low_errors, high_errors]),
                fmt="none",
                ecolor="#374151",
                capsize=3,
                linewidth=1,
            )
            axis.axhline(0, color="#374151", linewidth=0.9)
            axis.set_title(f"{panel}: {title}")
            axis.set_xticks(x, labels, rotation=16, ha="right")
            axis.set_ylabel("Rank correlation" if row == 0 else "Per cent per rebalance")
            axis.spines[["top", "right"]].set_visible(False)
    fig.suptitle("Results over the shared period depend on the population and objective", fontsize=14, fontweight="bold")
    fig.tight_layout(rect=[0, 0, 1, 0.95])
    save(fig, "figure_4_allocation_comparison")


def external_figure() -> None:
    temporal = pd.read_csv(RESULTS_DIR / "external_temporal.csv")
    temporal = temporal[temporal["composite"] == "equal_signal"]
    alternatives = pd.read_csv(RESULTS_DIR / "external_alternative_constructions.csv")
    order = [
        "original_portfolios",
        "equal_weighted_deciles",
        "quintiles",
        "value_weighted_deciles",
        "market_cap_above_nyse20",
        "nyse_only",
        "price_above_five",
    ]
    label_map = {
        "original_portfolios": "Published",
        "equal_weighted_deciles": "EW deciles",
        "quintiles": "Quintiles",
        "value_weighted_deciles": "VW deciles",
        "market_cap_above_nyse20": "Above NYSE 20th",
        "nyse_only": "NYSE only",
        "price_above_five": "Price above $5",
    }
    alternatives = alternatives.set_index("construction").loc[order].reset_index()
    fig, axes = plt.subplots(1, 2, figsize=(11.0, 4.8))
    axes[0].bar(
        temporal["period"],
        temporal["mean_monthly_return"] * 100,
        color=[NAVY, BLUE, LIGHT_BLUE, GOLD],
        width=0.65,
    )
    axes[0].set_title("Public gross composite through time")
    axes[0].set_ylabel("Mean monthly long-short return (%)")
    axes[0].tick_params(axis="x", rotation=15)
    axes[0].spines[["top", "right"]].set_visible(False)

    axes[1].barh(
        [label_map[value] for value in alternatives["construction"]],
        alternatives["mean_monthly_return"] * 100,
        color=[NAVY, BLUE, LIGHT_BLUE, "#6F8FB3", GOLD, "#8B9AAA", "#A9B5C2"],
    )
    axes[1].invert_yaxis()
    axes[1].set_title("Portfolio-construction robustness, 1990-2024")
    axes[1].set_xlabel("Mean monthly long-short return (%)")
    axes[1].spines[["top", "right"]].set_visible(False)
    fig.suptitle("Public gross long-short evidence is weaker in the recent sample", fontsize=14, fontweight="bold")
    fig.tight_layout(rect=[0, 0, 1, 0.94])
    save(fig, "figure_5_external_evidence")


def cost_figure() -> None:
    summary = pd.read_csv(RESULTS_DIR / "local_composite_common_window.csv")
    frame = summary[
        (summary["population"] == "gbp_fixed")
        & (summary["variant"] == "broad_deduplicated")
    ]
    allocations = [
        "equal_signal",
        "equal_group",
        "inverse_redundancy",
        "trailing_positive_evidence",
    ]
    label_map = {
        "equal_signal": "Equal signal",
        "equal_group": "Equal group",
        "inverse_redundancy": "Inverse redundancy",
        "trailing_positive_evidence": "Trailing evidence",
    }
    costs = [0, 10, 25, 50, 100]
    fig, axis = plt.subplots(figsize=(8.6, 5.2))
    for allocation, colour in zip(allocations, [NAVY, BLUE, GOLD, BURGUNDY]):
        allocation_frame = frame[frame["allocation"] == allocation].set_index("metric")
        values = [allocation_frame.loc["spread", "mean"]]
        values.extend(allocation_frame.loc[f"net_spread_{cost}bps", "mean"] for cost in costs[1:])
        axis.plot(costs, np.array(values) * 100, marker="o", linewidth=2, color=colour, label=label_map[allocation])
    axis.axhline(0, color="#374151", linewidth=0.9)
    axis.set_title("A stronger ranking statistic need not survive higher implementation cost")
    axis.set_xlabel("Cost per unit of two-leg turnover (basis points)")
    axis.set_ylabel("Mean top-minus-bottom return per rebalance (%)")
    axis.legend(ncol=2)
    axis.spines[["top", "right"]].set_visible(False)
    fig.tight_layout()
    save(fig, "figure_6_cost_sensitivity")


def implementation_figure() -> None:
    frame = pd.read_parquet(RESULTS_DIR / "local_implementation_date.parquet")
    fig, axes = plt.subplots(1, 2, figsize=(10.6, 4.6))
    axes[0].hist(frame["trade_cancellation"], bins=16, color=BLUE, edgecolor="white")
    mean = frame["trade_cancellation"].mean()
    axes[0].axvline(mean, color=GOLD, linewidth=2)
    axes[0].text(
        mean,
        axes[0].get_ylim()[1] * 0.98,
        f"Mean {mean:.1%}",
        ha="center",
        va="top",
        fontweight="bold",
        color=NAVY,
        bbox={"facecolor": "white", "edgecolor": "none", "pad": 1.5},
    )
    axes[0].set_title("Trade cancellation across signal sleeves")
    axes[0].set_xlabel("Fraction of sleeve trading cancelled")
    axes[0].set_ylabel("Rebalances")
    axes[0].spines[["top", "right"]].set_visible(False)

    axes[1].scatter(
        frame["weighted_sleeve_turnover"],
        frame["aggregate_sleeve_turnover"],
        color=NAVY,
        alpha=0.72,
        s=28,
        edgecolor="white",
        linewidth=0.4,
    )
    upper = max(frame["weighted_sleeve_turnover"].max(), frame["aggregate_sleeve_turnover"].max())
    axes[1].plot([0, upper], [0, upper], color=BURGUNDY, linestyle="--", linewidth=1)
    axes[1].set_title("Aggregate trading is below sleeve trading")
    axes[1].set_xlabel("Mean turnover across individual sleeves")
    axes[1].set_ylabel("Turnover after aggregation")
    axes[1].spines[["top", "right"]].set_visible(False)
    fig.suptitle("Implementation must be measured after signals meet", fontsize=14, fontweight="bold")
    fig.tight_layout(rect=[0, 0, 1, 0.94])
    save(fig, "figure_7_trade_cancellation")


def factor_figure() -> None:
    frame = pd.read_csv(RESULTS_DIR / "external_factor_regressions.csv")
    frame = frame[frame["specification"] == "ff5_plus_momentum"].copy()
    composite_order = ["equal_signal", "equal_signal_deduplicated", "equal_group"]
    labels = ["All 112", "Local-map 106", "Equal group"]
    periods = ["1990-2024", "2016-2024"]
    fig, axis = plt.subplots(figsize=(8.7, 5.0))
    x = np.arange(len(composite_order))
    width = 0.34
    for offset, period, colour in ((-width / 2, periods[0], NAVY), (width / 2, periods[1], GOLD)):
        part = frame[frame["period"] == period].set_index("composite").loc[composite_order]
        estimates = part["annualised_intercept"] * 100
        standard_errors = (part["annualised_intercept"] / part["intercept_hac_t"]).abs() * 100
        axis.bar(x + offset, estimates, width=width, color=colour, label=period)
        axis.errorbar(
            x + offset,
            estimates,
            yerr=1.96 * standard_errors,
            fmt="none",
            ecolor="#374151",
            capsize=3,
            linewidth=1,
        )
    axis.axhline(0, color="#374151", linewidth=0.9)
    axis.set_xticks(x, labels)
    axis.set_ylabel("Annualised regression intercept (%)")
    axis.set_title("Public composite after familiar US factors")
    axis.legend(title="Sample")
    axis.spines[["top", "right"]].set_visible(False)
    fig.tight_layout()
    save(fig, "figure_8_factor_diagnostic")


def main() -> None:
    ensure_directories()
    style()
    compression_figure()
    pair_structure_figure()
    marginal_figure()
    allocation_figure()
    external_figure()
    cost_figure()
    implementation_figure()
    factor_figure()
    print("figures complete")


if __name__ == "__main__":
    main()
