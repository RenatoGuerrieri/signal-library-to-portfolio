# Specification Corrections

**Recorded:** 21 July 2026  
**Status:** Applies to all final return, allocation and implementation results generated after this record.

The frozen specification remains unchanged. This file records corrections made after the first outcome calculation and explains why they do not constitute outcome-based model selection.

## 1. Date-level population floor

The first return run revealed 15 signal dates on which the global score panel contained only 36 to 45 securities. These dates were calendar month ends when most relevant exchanges were closed. They are not economically comparable cross-sections and cannot support quintile portfolios.

All local return tests now require at least 1,000 securities with an available score and clean 21-session return on a global signal date. The domestic US subset requires at least 300. These thresholds mirror the pre-specified median cross-section requirements used to admit signals to the respective samples. The rule uses only score and label availability, not realised return magnitude or sign.

## 2. Binary indicator rank correlation

The first implementation required at least three distinct score values before calculating a rank information coefficient. This incorrectly made binary event indicators ineligible even when they had broad cross-sectional coverage. The corrected implementation requires two distinct values. Spearman correlation is defined for a binary rank variable provided both values are present.

Quantile-spread evidence remains unavailable when an indicator does not form both a top and a bottom quintile under the common threshold rule. Such results remain missing rather than being imputed.

## 3. Trailing correlation matrices

The first inverse-redundancy calculation used direct matrix addition over the preceding 36 dates. One unavailable pair cell on any date propagated through the full average and produced missing allocation weights. The corrected implementation averages each pair over its available trailing observations. No missing correlation is replaced with zero.

## 4. Pairwise rank calculation

The independent recalculation identified that the first IC implementation retained score ranks calculated before return rows with missing labels were removed. This differs slightly from exact pairwise Spearman correlation. The corrected implementation re-ranks both score and return on the matched sample.

The same audit was applied to pairwise score correlations. Both characteristics are re-ranked on their common observations and at least 200 common securities are required. Tail-overlap cells use the same 200-security minimum.

## 5. Two-sided implementation sleeves

The implementation audit found that a binary indicator can form a top set without forming a bottom quintile under the common threshold rule. The initial sleeve helper then retained an unintended one-sided position. The corrected implementation requires at least ten securities in both the top and bottom legs. A signal-date sleeve that does not meet both requirements is inactive in the trade-cancellation calculation. This correction does not affect score composites, information coefficients, allocation rules or return spreads.

## 6. Audit treatment

Results from the superseded runs are retained in the execution logs but are not used in the final tables, figures or claims. All local population jobs are rerun after these corrections. The independent verification script checks the population floors, binary-indicator estimates and finite inverse-redundancy weights.

## 7. Common-window allocation comparison

The fixed allocation rules have 103 eligible global dates and 101 domestic US dates. The inverse redundancy and trailing positive-evidence rules require 36 prior observations and therefore begin later. Comparing each rule over all of its available dates would mix rule effects with sample-period effects.

The final allocation comparison therefore also reports a common window. For each population and metric, a date is included only when every reported allocation rule has an observation. This produces 67 global dates for information coefficient and gross spread, and 65 domestic US dates. Cost-adjusted comparisons use the dates on which all four rules have a preceding portfolio, leaving 66 and 64 observations respectively. The restriction is mechanical and does not use the sign or magnitude of any result.

## 8. Effective-rank matrix robustness

The reported rank uses the time average of date-level pairwise Spearman correlations. Pairwise availability means that this aggregate need not be positive semidefinite. The final audit therefore records the count and aggregate magnitude of negative eigenvalues before repair, applies the nearest symmetric positive semidefinite projection followed by diagonal normalisation, repeats the calculation with pairwise time medians, and averages repaired date-level matrices as a separate construction.

These tests were added as a publication robustness check. They do not replace the principal calculation or its block bootstrap. Their purpose is to determine whether the breadth conclusion depends on one matrix repair.

## 9. Public composite hierarchy

The primary external composite uses all 112 qualifying public predictor portfolios. The 106-series set obtained from the local reconstruction map is reported only as a sensitivity. The local map is not evidence that two official public portfolios are duplicates unless the public definitions or realised histories establish that relationship directly.

## 10. Mechanically defined deterioration

Failure-state evidence is descriptive and uses no discretionary regime labels. The local test conditions on the worst decile of the fixed-panel equal signal information coefficient. The public test conditions on the worst decile of the all-112 equal signal return. The audit also reports pairwise overlap in component-specific worst return and drawdown deciles. These diagnostics describe coincidence; they do not identify a causal regime.

## 11. Time-varying signal availability

The broad rule requires 80 of 121 dates and therefore permits the set of available characteristics to change through time. The publication audit reports the minimum, median and maximum number of active characteristics by date and by calendar year. It also repeats the fixed-panel equal signal test using only deduplicated characteristics with at least 1,000 scored securities on every one of the 103 reported return dates.

The constant set is a robustness test of membership variation. It is selected using full-sample coverage and is not presented as a historically point-in-time selection rule.

## 12. Chance benchmark for deterioration overlap

The public worst-decile overlap statistics are compared with the mechanical independence value for two decile sets, 0.10 divided by 1.90. A seeded permutation test independently reassigns each portfolio's worst observations across its valid dates while preserving the number of selected observations and its missing-data pattern. The reported distribution is based on 2,000 permutations. This test assesses coincidence relative to chance; it does not attach an economic regime label.
