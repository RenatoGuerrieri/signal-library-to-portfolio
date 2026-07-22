from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from difflib import SequenceMatcher
from pathlib import Path

import pandas as pd
import requests

from analysis_common import RESULTS_DIR, atomic_write_json, ensure_directories, write_frame


REFERENCES = [
    ("Bailey et al. (2017)", "The Probability of Backtest Overfitting", 2017, "10.21314/JCF.2016.322"),
    ("Benjamini and Hochberg (1995)", "Controlling the False Discovery Rate: A Practical and Powerful Approach to Multiple Testing", 1995, "10.1111/j.2517-6161.1995.tb02031.x"),
    ("Carhart (1997)", "On Persistence in Mutual Fund Performance", 1997, "10.1111/j.1540-6261.1997.tb03808.x"),
    ("Chen and Zimmermann (2022)", "Open Source Cross-Sectional Asset Pricing", 2022, "10.1561/104.00000112"),
    ("DeMiguel, Garlappi and Uppal (2009)", "Optimal Versus Naive Diversification: How Inefficient Is the 1/N Portfolio Strategy?", 2009, "10.1093/rfs/hhm075"),
    ("DeMiguel et al. (2020)", "A Transaction-Cost Perspective on the Multitude of Firm Characteristics", 2020, "10.1093/rfs/hhz085"),
    ("Fama and French (1993)", "Common risk factors in the returns on stocks and bonds", 1993, "10.1016/0304-405X(93)90023-5"),
    ("Fama and French (2015)", "A five-factor asset pricing model", 2015, "10.1016/j.jfineco.2014.10.010"),
    ("Feng, Giglio and Xiu (2020)", "Taming the Factor Zoo: A Test of New Factors", 2020, "10.1111/jofi.12883"),
    ("Freyberger, Neuhierl and Weber (2020)", "Dissecting Characteristics Nonparametrically", 2020, "10.1093/rfs/hhz123"),
    ("Green, Hand and Zhang (2017)", "The Characteristics that Provide Independent Information about Average U.S. Monthly Stock Returns", 2017, "10.1093/rfs/hhx019"),
    ("Grinold (1989)", "The Fundamental Law of Active Management", 1989, "10.3905/jpm.1989.409211"),
    ("Harvey, Liu and Zhu (2016)", "... and the Cross-Section of Expected Returns", 2016, "10.1093/rfs/hhv059"),
    ("Jensen, Kelly and Pedersen (2023)", "Is There a Replication Crisis in Finance?", 2023, "10.1111/jofi.13249"),
    ("Khandani and Lo (2011)", "What Happened to the Quants in August 2007? Evidence from Factors and Transactions Data", 2011, "10.1016/j.finmar.2010.07.005"),
    ("Kozak, Nagel and Santosh (2020)", "Shrinking the Cross-Section", 2020, "10.1016/j.jfineco.2019.06.008"),
    ("Markowitz (1952)", "Portfolio Selection", 1952, "10.1111/j.1540-6261.1952.tb01525.x"),
    ("McLean and Pontiff (2016)", "Does Academic Research Destroy Stock Return Predictability?", 2016, "10.1111/jofi.12365"),
    ("Newey and West (1987)", "A Simple, Positive Semi-Definite, Heteroskedasticity and Autocorrelation Consistent Covariance Matrix", 1987, "10.2307/1913610"),
    ("Novy-Marx and Velikov (2016)", "A Taxonomy of Anomalies and Their Trading Costs", 2016, "10.1093/rfs/hhv063"),
    ("Perold (1988)", "The Implementation Shortfall: Paper Versus Reality", 1988, "10.3905/jpm.1988.409150"),
    ("White (2000)", "A Reality Check for Data Snooping", 2000, "10.1111/1468-0262.00152"),
]


def normalise_title(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()


def validate(reference: tuple[str, str, int, str]) -> dict[str, object]:
    citation, expected_title, expected_year, doi = reference
    url = f"https://api.crossref.org/works/{requests.utils.quote(doi, safe='')}"
    response = requests.get(
        url,
        timeout=30,
        headers={"User-Agent": "GuerrieriCapitalResearchAudit/1.0 (mailto:renato.guerrieri@guerriericapital.com)"},
    )
    response.raise_for_status()
    message = response.json()["message"]
    actual_title = (message.get("title") or [""])[0]
    issued = message.get("published-print") or message.get("published-online") or message.get("issued")
    actual_year = issued["date-parts"][0][0] if issued else None
    authors = "; ".join(
        " ".join(value for value in (author.get("given"), author.get("family")) if value)
        for author in message.get("author", [])
    )
    similarity = SequenceMatcher(
        None, normalise_title(expected_title), normalise_title(actual_title)
    ).ratio()
    validation_note = "Crossref title and publication year match."
    manually_reconciled = False
    year_matches = actual_year == expected_year
    title_matches = similarity >= 0.94
    evidence_url = ""
    if citation == "Bailey et al. (2017)":
        year_matches = actual_year in {2016, 2017}
        manually_reconciled = actual_year == 2016
        validation_note = (
            "Crossref records the 2016 online date; the publisher assigns the article to "
            "Volume 20, Number 4, April 2017."
        )
        evidence_url = "https://www.risk.net/journal-of-computational-finance/volume-20-number-4-april-2017"
    if citation == "Perold (1988)":
        title_matches = normalise_title(expected_title).startswith(normalise_title(actual_title))
        manually_reconciled = True
        validation_note = (
            "Crossref stores the main title only; bibliographic catalogues record "
            "'Paper versus reality' as the subtitle."
        )
        evidence_url = "https://cir.nii.ac.jp/crid/1360011143955136384"
    return {
        "citation": citation,
        "doi": doi,
        "resolved_url": message.get("URL"),
        "expected_title": expected_title,
        "crossref_title": actual_title,
        "title_similarity": similarity,
        "expected_year": expected_year,
        "crossref_year": actual_year,
        "year_matches": year_matches,
        "container_title": (message.get("container-title") or [""])[0],
        "volume": message.get("volume"),
        "issue": message.get("issue"),
        "page": message.get("page"),
        "authors": authors,
        "manually_reconciled": manually_reconciled,
        "validation_note": validation_note,
        "supporting_url": evidence_url,
        "validated": title_matches and year_matches,
    }


def main() -> None:
    ensure_directories()
    rows = []
    errors = []
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(validate, reference): reference for reference in REFERENCES}
        for future in as_completed(futures):
            reference = futures[future]
            try:
                rows.append(future.result())
            except Exception as exc:
                errors.append(
                    {"citation": reference[0], "doi": reference[3], "error": repr(exc)}
                )
    frame = pd.DataFrame(rows).sort_values("citation")
    write_frame(frame, RESULTS_DIR / "reference_doi_validation.csv")
    if errors:
        write_frame(pd.DataFrame(errors), RESULTS_DIR / "reference_doi_errors.csv")
    summary = {
        "references_submitted": len(REFERENCES),
        "references_resolved": len(frame),
        "references_validated": int(frame["validated"].sum()) if len(frame) else 0,
        "errors": errors,
        "all_validated": bool(len(frame) == len(REFERENCES) and frame["validated"].all()),
    }
    atomic_write_json(RESULTS_DIR / "reference_validation.json", summary)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
