"""Curated whitelist of canonical Indian indirect/direct tax authorities.

Recurring partner feedback (Sprint 5 review on three test drafts):
    "The verifier has a recurring false negative on Pushpam Pharmaceuticals
     across two drafts. The case is real, foundational, and routinely cited
     in s.74 GST adjudications. Same risk exists for Cosmic Dye Chemical,
     Continental Foundation, Anand Nishikawa, Suncraft Energy, D.Y. Beathel
     Enterprises, On Quest Merchandising and other parallel authorities on
     the same point."

So instead of asking IndianKanoon's free-tier search to consistently surface
these (it doesn't — the corpus has hundreds of "Pushpam"-name hits and the
right one ranks below noise), we maintain a small curated map of widely-
cited tax-law authorities keyed by case-name substring. A match here always
returns VERIFIED with the canonical IndianKanoon URL, bypassing the network
call entirely. The verifier consults this list first; if no match, it falls
back to the configured provider (stub or IndianKanoon search).

This is NOT a substitute for real verification of obscure / new cases —
those still go to IndianKanoon. It's a pragmatic safety net for the dozen
or so cases every Indian tax practitioner uses every day.

The list is hand-curated; add new entries as partners flag false negatives.
Keys must be lowercase substrings that uniquely identify the case in the
universe of likely citation text.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
from urllib.parse import quote_plus

# Which tax regime an authority belongs to. "INDIRECT" covers Central
# Excise / Customs / Service Tax / VAT / GST; "DIRECT" is the Income-tax
# Act; "ANY" marks regime-neutral principles (natural justice) that may be
# cited in either kind of reply.
Regime = Literal["INDIRECT", "DIRECT", "ANY"]


@dataclass(frozen=True, slots=True)
class CanonicalCase:
    name: str
    canonical_url: str
    proposition: str  # short note on what the case stands for
    regime: Regime = "INDIRECT"


def _ik_search(name: str) -> str:
    """IndianKanoon search URL for entries whose doc id we haven't pinned."""
    return f"https://indiankanoon.org/search/?formInput={quote_plus(name)}"


# Substring (lowercased) → CanonicalCase. Substring match because the model
# may format the case name with or without years / reporter abbreviations.
CANONICAL_CASES: dict[str, CanonicalCase] = {
    # ---- Excise / GST suppression + extended period -----------------------
    "pushpam pharmaceuticals": CanonicalCase(
        name="Pushpam Pharmaceuticals Co. v. Collector of Central Excise, Bombay",
        canonical_url="https://indiankanoon.org/doc/1690509/",
        proposition=(
            "Suppression of facts under the proviso to s.11A Central Excise "
            "(now mirrored in s.74 CGST) requires deliberate withholding; "
            "mere non-disclosure is insufficient."
        ),
    ),
    "cosmic dye chemical": CanonicalCase(
        name="Cosmic Dye Chemical v. Collector of Central Excise",
        canonical_url="https://indiankanoon.org/doc/1623537/",
        proposition=(
            "Extended period of limitation cannot be invoked without intent "
            "to evade tax; mens rea is essential."
        ),
    ),
    "continental foundation": CanonicalCase(
        name="Continental Foundation Joint Venture v. CCE Chandigarh",
        canonical_url="https://indiankanoon.org/doc/1107015/",
        proposition=(
            "Suppression must be conscious and deliberate; a bona fide "
            "interpretation that is later disagreed with is not suppression."
        ),
    ),
    "anand nishikawa": CanonicalCase(
        name="Anand Nishikawa Co. Ltd. v. Commissioner of Central Excise",
        canonical_url="https://indiankanoon.org/doc/1257203/",
        proposition=(
            "Suppression of facts in the proviso to s.11A is to be construed "
            "strictly; the department must show wilful misstatement."
        ),
    ),
    # ---- Cross-examination / natural justice ------------------------------
    "andaman timber": CanonicalCase(
        name="Andaman Timber Industries v. Commissioner of Central Excise",
        canonical_url="https://indiankanoon.org/doc/24400737/",
        proposition=(
            "Denial of cross-examination of witnesses whose statements are "
            "relied upon vitiates the adjudication order."
        ),
        regime="ANY",
    ),
    # ---- ITC mismatch / supplier default ---------------------------------
    "suncraft energy": CanonicalCase(
        name="Suncraft Energy Pvt Ltd v. Assistant Commissioner, State Tax",
        canonical_url="https://indiankanoon.org/doc/108089236/",
        proposition=(
            "ITC cannot be denied to the recipient merely because the "
            "supplier failed to deposit tax; recovery must first be "
            "attempted from the defaulting supplier (Calcutta HC, 2023)."
        ),
    ),
    "d.y. beathel": CanonicalCase(
        name="D.Y. Beathel Enterprises v. State Tax Officer",
        canonical_url="https://indiankanoon.org/doc/108580056/",
        proposition=(
            "Where supplier has not paid tax, reversal of ITC against the "
            "buyer without investigation of the supplier is unsustainable "
            "(Madras HC, 2021)."
        ),
    ),
    "on quest merchandising": CanonicalCase(
        name="On Quest Merchandising India Pvt Ltd v. Government of NCT of Delhi",
        canonical_url="https://indiankanoon.org/doc/115886907/",
        proposition=(
            "Section 9(2)(g) DVAT (analogous to GST ITC matching rules) "
            "cannot deny ITC to a purchaser for the seller's default "
            "(Delhi HC)."
        ),
    ),
    # ---- ITC entitlement principle ---------------------------------------
    # Bharti Airtel deliberately absent: the entry named it "Commissioner of
    # GST v. Bharti Airtel" for a burden-of-proof proposition. The SC case is
    # Union of India v. Bharti Airtel (2021), decided for the Revenue
    # (GSTR-3B self-assessment, GSTR-2A only a facilitator) — not a case to
    # offer the drafter for the taxpayer. Without an entry it goes to the
    # normal IndianKanoon check like any other citation.
    "eicher motors": CanonicalCase(
        name="Eicher Motors Ltd v. Union of India",
        canonical_url="https://indiankanoon.org/doc/1486002/",
        proposition=(
            "Credit accrued is a vested right; subsequent legislation "
            "cannot retrospectively defeat it."
        ),
    ),
    # ---- Reconciliation defence ------------------------------------------
    "larsen": CanonicalCase(
        name="Larsen & Toubro Ltd v. State of Karnataka",
        canonical_url="https://indiankanoon.org/doc/89018960/",
        proposition=(
            "Reconciliation between returns is a sufficient response where "
            "records bear out the assessee's position (SC, 2014)."
        ),
    ),
    # ---- Show-cause notice formation -------------------------------------
    "amrit foods": CanonicalCase(
        name="Amrit Foods v. Commissioner of Central Excise, UP",
        canonical_url="https://indiankanoon.org/doc/1379149/",
        proposition=(
            "A show cause notice that does not specifically allege which "
            "limb of the proviso (fraud / collusion / wilful misstatement "
            "/ suppression) is invoked is bad in law."
        ),
    ),
    "uniworth textiles": CanonicalCase(
        name="Uniworth Textiles Ltd v. CCE, Raipur",
        canonical_url="https://indiankanoon.org/doc/164527889/",
        proposition=(
            "Mere non-payment of tax is not equivalent to suppression; "
            "extended period requires positive act of suppression."
        ),
    ),
    # ---- Direct tax: 148 reassessment ------------------------------------
    "ganga saran": CanonicalCase(
        name="ITO v. Ganga Saran & Sons (P) Ltd",
        canonical_url="https://indiankanoon.org/doc/1521085/",
        proposition=(
            "The 'reason to believe' for s.148 reopening must have a "
            "rational connection with the material on record; suspicion is "
            "not enough (SC)."
        ),
        regime="DIRECT",
    ),
    "kelvinator": CanonicalCase(
        name="CIT v. Kelvinator of India Ltd",
        canonical_url="https://indiankanoon.org/doc/1192802/",
        proposition=(
            "Reassessment under s.148 cannot be based on a mere change of "
            "opinion; tangible material is required (SC, 2010)."
        ),
        regime="DIRECT",
    ),
    "ashish agarwal": CanonicalCase(
        name="Union of India v. Ashish Agarwal",
        canonical_url="https://indiankanoon.org/doc/183568974/",
        proposition=(
            "Section 148A(b) procedure must be followed for reassessment "
            "notices issued under the new regime (SC, 2022)."
        ),
        regime="DIRECT",
    ),
    # ---- Direct tax: depreciation (s.32) ----------------------------------
    "whittle anderson": CanonicalCase(
        name="CIT v. Whittle Anderson Ltd",
        canonical_url=_ik_search("Whittle Anderson"),
        proposition=(
            "An asset is 'used' for s.32 depreciation once it is ready for "
            "use in the business, even if not actually operated in the "
            "year — (1971) 79 ITR 613 (Bom)."
        ),
        regime="DIRECT",
    ),
    "shaan finance": CanonicalCase(
        name="CIT v. Shaan Finance (P) Ltd",
        canonical_url=_ik_search("Shaan Finance"),
        proposition=(
            "Depreciation is allowable where the asset is used for the "
            "purpose of the assessee's business, including commercial "
            "exploitation by leasing — (1998) 231 ITR 308 (SC)."
        ),
        regime="DIRECT",
    ),
    "mysore minerals": CanonicalCase(
        name="Mysore Minerals Ltd v. CIT",
        canonical_url=_ik_search("Mysore Minerals"),
        proposition=(
            "'Owned' in s.32 includes beneficial ownership; an assessee in "
            "possession and use as owner is entitled to depreciation even "
            "without a registered title — (1999) 239 ITR 775 (SC)."
        ),
        regime="DIRECT",
    ),
    # ---- Direct tax: natural justice -------------------------------------
    "kishinchand chellaram": CanonicalCase(
        name="Kishinchand Chellaram v. CIT",
        canonical_url=_ik_search("Kishinchand Chellaram"),
        proposition=(
            "Material relied upon against the assessee must be disclosed "
            "and the assessee given an opportunity to rebut it — (1980) "
            "125 ITR 713 (SC)."
        ),
        regime="DIRECT",
    ),
}


def cases_for_regime(regime: Regime) -> list[CanonicalCase]:
    """Whitelist entries citable in a reply under ``regime`` (incl. ANY)."""
    return [c for c in CANONICAL_CASES.values() if c.regime in (regime, "ANY")]


def lookup_canonical(case_name: str) -> CanonicalCase | None:
    """Substring-match a citation's case name against the whitelist."""
    needle = case_name.lower()
    for key, entry in CANONICAL_CASES.items():
        if key in needle:
            return entry
    return None
