"""Regime-aware drafting guidance + reply-format profiles.

The drafting prompt used to hard-code one canonical-authority list that was
GST / excise-heavy, so Income Tax replies picked up indirect-tax precedents,
and the personal-hearing prayer cited s.142(2) IT (an inquiry power) instead
of the faceless-assessment hearing provision. Both are now derived from the
matter's law and injected into the user prompt as the REGIME GUIDANCE block.

The REPLY FORMAT block lets the partner pick a different reply shape on
(re)generation — full submissions, tabular reconciliation, or an
adjournment petition.
"""

from __future__ import annotations

from typing import Final, Literal

from app.agents.citation_whitelist import Regime, cases_for_regime

FormatProfile = Literal["full_submission", "tabular_reconciliation", "adjournment"]

FORMAT_PROFILES: Final[tuple[str, ...]] = (
    "full_submission",
    "tabular_reconciliation",
    "adjournment",
)


def regime_for_law(law: str | None) -> Regime:
    """Map ``matters.law`` ('IT' / 'GST' / ...) to the whitelist regime."""
    normalised = (law or "").strip().upper()
    return "DIRECT" if normalised in ("IT", "INCOME TAX", "INCOME_TAX") else "INDIRECT"


def hearing_provision(law: str | None, document_type: str | None) -> str:
    """The provision the Prayer should cite when requesting a personal hearing."""
    if regime_for_law(law) == "DIRECT":
        return (
            "Section 144B(6)(vii) read with Section 144B(7) of the Income-tax "
            "Act, 1961 (personal hearing in faceless assessment, conducted "
            "through video conferencing)"
        )
    doc = (document_type or "").upper()
    if "ASMT" in doc:
        # ASMT-10 scrutiny: hearing is sought before proceeding to s.73/74.
        return (
            "Section 61 read with Section 75(4) of the CGST Act, 2017 (and the "
            "corresponding State GST Act)"
        )
    return "Section 75(4) of the CGST Act, 2017 (and the corresponding State GST Act)"


def render_regime_guidance(law: str | None, document_type: str | None) -> str:
    regime = regime_for_law(law)
    lines: list[str] = []
    if regime == "DIRECT":
        lines.append(
            "Regime: DIRECT TAX (Income-tax Act, 1961). Cite only Supreme "
            "Court, High Court (direct-tax benches) and ITAT authorities. Do "
            "NOT cite Central Excise / Customs / Service Tax / GST / CESTAT "
            "decisions."
        )
    else:
        lines.append(
            "Regime: INDIRECT TAX (CGST / SGST / IGST Acts, with Central "
            "Excise / Service Tax precedents where the principle carries "
            "over). Cite Supreme Court, High Court, CESTAT / GSTAT "
            "authorities. Do NOT cite Income-tax Act / ITAT decisions."
        )
    lines.append("")
    lines.append(
        "Personal-hearing provision for the Prayer: "
        f"{hearing_provision(law, document_type)}."
    )
    lines.append("")
    lines.append(
        "Canonical authorities recognised by the verifier for this regime "
        "(use whichever genuinely fit the notice; '[any regime]' marks "
        "natural-justice principles citable in either):"
    )
    for case in cases_for_regime(regime):
        tag = " [any regime]" if case.regime == "ANY" else ""
        lines.append(f"  - {case.name}{tag}: {case.proposition}")
    return "\n".join(lines)


def render_format_profile(profile: str | None) -> str:
    p = profile if profile in FORMAT_PROFILES else "full_submission"
    if p == "tabular_reconciliation":
        return (
            "Profile: TABULAR RECONCILIATION + COVERING NOTE. Suited to simple "
            "mismatch / inquiry notices (e.g. s.142(1) IT, ASMT-10). Emit ONLY "
            "these sections:\n"
            "  01 Covering Letter — 2-4 short paragraphs: reference the notice "
            "(DIN, date), state that the reconciliation is enclosed, and "
            "summarise the outcome.\n"
            "  04 Reconciliation Statement — an HTML <table> with a header row "
            "(<thead>) and one row per discrepancy: Item / As per notice / As per "
            "books or return / Difference / Explanation & supporting document. "
            "Add a totals row.\n"
            "  10 Documents Enclosed — numbered <ol>.\n"
            "  12 Prayer — one paragraph: accept the reconciliation and drop the "
            "proceedings; request a personal hearing only if any item remains "
            "disputed.\n"
            "  13 Internal Partner Note and 14 Client Summary — as usual.\n"
            "  15 Filing Checklist — numbered <ol>.\n"
            "Keep legal argument brief; no separate Legal Submissions section "
            "unless an item cannot be reconciled on facts."
        )
    if p == "adjournment":
        return (
            "Profile: ADJOURNMENT / TIME-EXTENSION PETITION. The client needs "
            "more time; do NOT argue the merits. Emit ONLY these sections:\n"
            "  01 Petition for Adjournment — reference the notice (DIN, date, "
            "due date), state the specific reasons more time is needed (e.g. "
            "documents being collated from the client, the authorised "
            "representative's prior engagements), and request a specific "
            "extension of 15 to 30 days. Use [ASSUMED — please verify] for "
            "any reason not supported by the inputs.\n"
            "  10 Documents Enclosed — numbered <ol> (authorisation letter "
            "etc.; may be short).\n"
            "  12 Prayer — grant the extension to a specific date and keep "
            "proceedings in abeyance till then; reserve the right to file "
            "detailed submissions.\n"
            "  13 Internal Partner Note and 14 Client Summary — as usual "
            "(Client Summary lists what the client must send and by when).\n"
            "  15 Filing Checklist — numbered <ol>.\n"
            "No citations are needed."
        )
    return (
        "Profile: COMPREHENSIVE SUBMISSIONS. Produce the full section list "
        "described in the system prompt (01-07, 10-15, plus 08 / 09 when they "
        "apply)."
    )
