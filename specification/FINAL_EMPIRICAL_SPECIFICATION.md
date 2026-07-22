# Final Empirical Specification

**Frozen before the reported calculations:** 21 July 2026  
**Source characteristic build:** `openassetpricing_rebuild_v0_23_6`  
**Source universe build:** `expanded_universe_retest_v0_22_2`

## 1. Research question

The paper asks how a portfolio manager should distinguish catalogue size from independent information and how standalone evidence should be translated into a portfolio allocation decision.

The principal empirical claim concerns redundancy and effective breadth. Return and allocation results are supporting evidence. They are not claims of independent alpha or live performance.

## 2. Fixed source boundary

The source characteristic panel contains 164 reconstructed characteristics, 3,000 securities and 121 signal dates from 30 June 2016 to 12 June 2026. The security population was selected on 4 June 2026 using ten-year data coverage and recent local-price liquidity. This construction creates survivorship and currency-comparability limitations. It must not be described as a historically investable universe.

The datalake is read-only. The project stores hashes, manifests, compact derived data and aggregate results in the evidence pack.

## 3. Characteristic samples

The broad coverage rule is fixed at at least 80 signal dates and a median cross-section of at least 1,000 securities. It retains 112 characteristics.

Signal availability is reported by date because the coverage rule is assessed over the complete panel and does not require constant membership. A membership sensitivity retains only deduplicated characteristics with at least 1,000 scored securities on every date in the 103-date fixed-panel return comparison. This constant set is selected using full-sample coverage and is not treated as a point-in-time portfolio rule.

Four samples will be reported:

1. all 112 coverage-eligible characteristics;
2. the 28 coverage-eligible native implementations;
3. the broad sample after formula duplicates are removed;
4. the native sample after formula duplicates are removed.

Native means that `local_formula_quality` begins with `native_`. All other implementations are treated as proxies.

Duplicate candidates are characteristics sharing the same `local_formula_id`. Exact score equality is also audited across the complete eligible sample. One representative from each formula or exact-score component is selected before return analysis according to this order: native implementation, documented source-code availability, stronger source replication-quality label, greater date coverage, greater median cross-section and canonical identifier in lexical order.

## 4. Populations used for return evidence

### 4.1 Fixed broad research panel

The broad panel is retained for score redundancy, tail overlap and effective rank analysis. Local-currency returns from the archived rebuild are retained only as a reconciliation benchmark for the earlier reconstruction.

### 4.2 Sterling-comparable fixed panel

Return labels are rebuilt from production total-return adjusted prices and sterling exchange rates. For each signal date, entry is the first valid local close after the signal date. Exit is the close after 21 local trading observations. Local prices are translated into sterling at entry and exit using the last available exchange rate on or before the local price date, subject to a maximum five-calendar-day staleness rule.

Currency subunits are normalised before translation. Pence sterling are divided by 100. Israeli agorot and South African cents are divided by 100 before applying the relevant sterling cross. Securities without verified currency, price or exchange-rate coverage are excluded and reported.

This test fixes currency comparability. It does not remove the fixed-panel survivorship limitation.

### 4.3 US-dollar domestic-listing subset

The US test requires country `US`, currency `USD`, an ordinary equity or company security type, no fund or ETF flag, and a recognised US exchange. Foreign-domiciled US listings, ADRs and the sterling record identified in the earlier reconstruction are excluded. This remains a subset of the June 2026 fixed panel and is not a CRSP-style historical universe.

### 4.4 External public replication

Official Open Asset Pricing predictor portfolio returns will be used, where obtainable, as an independent economic robustness source. These returns test whether the main portfolio conclusions are consistent with the public US asset-pricing record. They do not validate the local security-level reconstructions.

The principal external period is January 1990 to December 2024. A characteristic must have at least 240 monthly observations in that period. A contemporaneous comparison uses January 2016 to December 2024 and requires at least 96 observations. The primary external composite uses all 112 qualifying published long-short portfolios. A 106-series set selected by applying the local reconstruction map is a sensitivity only. Value-weighted deciles, quintiles, a price screen above five dollars, an NYSE-only screen and a market capitalisation screen above the twentieth NYSE percentile are further robustness tests.

## 5. Fixed statistical tests

The following tests define the final empirical specification:

- standalone monthly Spearman information coefficient;
- Newey-West inference with lag equal to the forward-return horizon in months, rounded up, and a minimum lag of one;
- Benjamini-Hochberg false-discovery control at 10 per cent within each named test family;
- pairwise Spearman score correlation;
- top- and bottom-quintile Jaccard overlap;
- correlation-matrix effective rank using participation ratio and entropy;
- leave-one-characteristic-out and leave-one-group-out marginal information;
- one-way basket replacement, portfolio turnover and trade cancellation;
- gross and cost-adjusted top-minus-bottom returns;
- temporal stability across fixed subperiods and expanding-window evaluation.
- active characteristic counts by date and a sensitivity using a constant characteristic set;
- overlap of mechanically defined worst return and drawdown deciles relative to chance.

Bootstrap confidence intervals will use signal-date block resampling. The block length is six monthly observations and the default number of replications is 2,000. Sensitivity uses three- and twelve-month blocks.

Production-price returns are not winsorised in the principal test. A sensitivity test winsorises the cross-section at 0.5 and 99.5 per cent on each signal date. Missing labels, non-positive prices, stale exchange rates and extreme observations are counted and reported rather than silently discarded.

The pre-analysis price audit identified vendor unit breaks that survived the production file-level validation. Raw labels are retained, but the principal clean label excludes a holding window when any adjacent daily total-return price ratio is below 0.2 or above 5. The count of excluded windows and affected securities is reported. This gate addresses mechanical scale discontinuities; it is not a return-based security selection rule.

## 6. Allocation rules

Fixed allocation comparisons are equal group weight, equal characteristic weight and inverse-redundancy group weight. At each eligible date, the inverse-redundancy rule uses the preceding 36 observations. Each group's raw weight is the reciprocal of 0.05 plus its mean absolute correlation with the other group composites. Raw weights are normalised across available groups. The current date is excluded.

The trailing positive-evidence rule includes a group when both its standalone and leave-one-group-out information coefficients are positive over the preceding 36 observations, subject to an embargo of one observation. It is retained as a historical illustration and is not promoted as a preferred rule. No allocation rule will be labelled preferred unless it satisfies a pre-stated objective and dominates the fixed alternatives on that objective after implementation costs.

## 7. Implementation evidence

For each composite and rebalance, the analysis will retain target weights, previous weights and required trades. Reported diagnostics include gross exposure, concentration, group contribution, sector and regional exposure, one-way turnover, trade cancellation, common-trade concentration and cost sensitivity.

Trade cancellation is measured in a separate equal-capital sleeve aggregation. Each characteristic forms an equal-weighted long top-quintile and short bottom-quintile sleeve. Cancellation is one minus aggregate portfolio trading divided by the characteristic-weighted sum of sleeve trading. This metric is not attributed to the rank-composite portfolios.

Liquidity capacity is reported only as a mechanical participation envelope using trailing 60-session sterling value traded. It is shown at 1, 5 and 10 per cent participation and does not include impact, borrow availability or financing.

No security name, current rank or current position will appear in the manuscript or public package.

## 8. Cost tests

The earlier reconstruction used a charge of 25 basis points per unit of one-way turnover. The final empirical specification reports 10, 25, 50 and 100 basis point scenarios. These are implementation sensitivities, not estimates of realised cost. Capacity is not claimed without point-in-time executable volume and market impact calibration.

## 9. Temporal evidence

Historical chronological tests are labelled temporal robustness because the full sample has already informed the research. They are not described as untouched holdouts.

A prospective specification record, including hashes of this file and the analysis code, will define the rule for observations after the current evidence date. Future evidence can then be described as genuinely new.

## 10. Publication rule

Every quantitative statement in the final manuscript must map to a generated result file and a row in the claim and evidence register. Where a result is sensitive to proxies, duplicates, currency treatment, sample definition or cost, the qualification must appear with the claim.
