# From Signal Library to Portfolio: Reproducibility Materials

This repository accompanies *From Signal Library to Portfolio: Redundancy, Marginal Information and Capital Allocation Across Systematic Signals* by Renato Guerrieri.

Permanent repository: https://github.com/RenatoGuerrieri/signal-library-to-portfolio

## Contents

- `specification/`: the final empirical specification, recorded corrections and claim and evidence register;
- `code/`: calculation, challenge, figure and validation scripts;
- `results/`: aggregate results used in the paper;
- `figures/`: the eight publication figures in PNG and SVG format;
- `manuscript/`: the final Markdown manuscript.

## Data boundary

Licensed security-level prices, returns, characteristic scores, target weights and security identifiers are not distributed. Public Open Asset Pricing files and the archived Fama/French factors can be obtained from their official sources. The code accepts source locations through the environment variables defined in `code/analysis_common.py`.

The aggregate files are sufficient to inspect the reported tables, figures and quantitative claims. Re-running the security-level pipeline requires equivalent licensed input data. Nothing in this repository is a live track record, current signal output, holding or investment recommendation.

## Verification

`public_manifest.csv` records the size and SHA-256 hash of every distributed file. The challenge and manuscript validation outputs are included in `results/`.

## Immutable release

The evidence set submitted with the paper is preserved as [version 1.0.0](https://github.com/RenatoGuerrieri/signal-library-to-portfolio/releases/tag/v1.0.0). The release record identifies the exact target commit and provides the submitted PDF and reproducibility archive as release assets.
