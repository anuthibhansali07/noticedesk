"""Canned parser + drafting responses for offline dev (LLM_PROVIDER_PRIMARY=stub).

The stub OCR provider returns text with no real identifiers, so the parsing
agent has nothing to extract. This module returns plausible canned JSON keyed
off the original filename embedded in the parsing-agent user prompt, so the
parse-and-route pipeline produces real-looking outcomes in offline dev.

The drafting agent (Sprint 5) also routes through here: when the stub provider
is selected, ``_drafting_canned`` assembles a deterministic 15-section reply
draft from the matter context inside the prompt. The output exercises every
downstream code path (citation extraction, verification, paragraph map, Word
export) without requiring an Anthropic key.

Replace with a real LLM provider for parser-quality or draft-quality testing —
identifiers hard-coded here must match the dev DB seed.
"""

from __future__ import annotations

import json
import re
from typing import Any

from app.core.config import get_settings

_CANNED_BY_FILENAME: dict[str, dict[str, Any]] = {
    "drc-01.pdf": {
        "document_type": "DRC-01A",
        "client_name_on_document": "Sunrise Papers Pvt Ltd",
        "pans_extracted": [{"value": "AAQCS3456P", "location": "header"}],
        "gstins_extracted": [
            {"value": "27AAQCS3456P1ZF", "location": "header", "state_code": "27"}
        ],
        "notice_number": "ZA270526123456",
        "din_or_rfn": "DIN-202604-000123",
        "issue_date": "2026-04-15",
        "receipt_date": "2026-04-20",
        "due_date": "2026-05-15",
        "hearing_date": None,
        "financial_year": "2023-24",
        "assessment_year": None,
        "authority": "Assistant Commissioner, CGST Mumbai West",
        "demand_amount": 245000,
        "issues": ["Mismatch between GSTR-3B and GSTR-1 for Q2 FY 2023-24"],
        "documents_required": ["GSTR-3B reconciliation", "Invoice-level supporting"],
        "parse_confidence": 0.92,
        "fields_needing_review": [],
    },
    "GST Notice.pdf": {
        "document_type": "ASMT-10",
        "client_name_on_document": "Acme Industries Private Limited",
        "pans_extracted": [{"value": "AAACA9876B", "location": "header"}],
        "gstins_extracted": [
            {"value": "27AAACA9876B1Z5", "location": "header", "state_code": "27"}
        ],
        "notice_number": "ASMT10-MH-2026-00789",
        "din_or_rfn": "DIN-202604-000789",
        "issue_date": "2026-04-22",
        "receipt_date": "2026-04-25",
        "due_date": "2026-05-22",
        "hearing_date": None,
        "financial_year": "2022-23",
        "assessment_year": None,
        "authority": "Deputy Commissioner of State Tax, Mumbai",
        "demand_amount": 1850000,
        "issues": ["ITC reconciliation discrepancy with GSTR-2A"],
        "documents_required": ["GSTR-2A reconciliation", "Tax invoices for inward supplies"],
        "parse_confidence": 0.90,
        "fields_needing_review": [],
    },

    # ── Mock Notice 1: GST ASMT-10 · Acme Industries · MH · FY 2022-23 ─────────
    "ASMT10_Acme_MH_FY2223.pdf": {
        "document_type": "ASMT-10",
        "client_name_on_document": "Acme Industries Private Limited",
        "pans_extracted": [{"value": "AAACA9876B", "location": "header"}],
        "gstins_extracted": [
            {"value": "27AAACA9876B1Z5", "location": "header", "state_code": "27"}
        ],
        "notice_number": "ASMT10-MH-2026-00789",
        "din_or_rfn": "DIN20260117ASMT10004587",
        "issue_date": "2026-01-17",
        "receipt_date": "2026-01-19",
        "due_date": "2026-02-17",
        "hearing_date": None,
        "financial_year": "2022-23",
        "assessment_year": None,
        "authority": "State Tax Officer, Range 5, Pune-1",
        "demand_amount": 1675600,
        "issues": ["ITC mismatch FY 22-23 -- excess ITC availed vs GSTR-2A: Rs. 14,20,000"],
        "documents_required": [
            "GSTR-3B reconciliation (month-wise)",
            "Tax invoices for disputed ITC",
            "Supplier communication if any",
        ],
        "parse_confidence": 0.93,
        "fields_needing_review": [],
    },
    # ── Mock Notice 2: GST DRC-01 · Sunteck Realty · MH · FY 2021-22 ───────────
    "DRC01_Sunteck_MH_FY2122.pdf": {
        "document_type": "DRC-01",
        "client_name_on_document": "Sunteck Realty Holdings Ltd",
        "pans_extracted": [{"value": "AABCS5678C", "location": "header"}],
        "gstins_extracted": [
            {"value": "27AABCS5678C1Z7", "location": "header", "state_code": "27"}
        ],
        "notice_number": "DRC0122MH0008871",
        "din_or_rfn": "DRC0122MH0008871",
        "issue_date": "2026-02-10",
        "receipt_date": "2026-02-12",
        "due_date": "2026-05-20",
        "hearing_date": None,
        "financial_year": "2021-22",
        "assessment_year": None,
        "authority": "Joint Commissioner of Central Tax, Mumbai West",
        "demand_amount": 4750000,
        "issues": ["GST demand on Transferable Development Rights (TDR) -- FY 2021-22"],
        "documents_required": ["JDA agreements", "TDR transfer documents", "GST computation workings"],
        "parse_confidence": 0.91,
        "fields_needing_review": [],
    },
    # ── Mock Notice 3: IT 142(1) · Acme Industries · AY 2024-25 ────────────────
    "IT_142_1_Acme_AY2425.pdf": {
        "document_type": "IT_142(1)",
        "client_name_on_document": "Acme Industries Private Limited",
        "pans_extracted": [{"value": "AAACA9876B", "location": "header"}],
        "gstins_extracted": [],
        "notice_number": "ITBA/ASS/F/142(1)/2025-26/0001",
        "din_or_rfn": "ITBA/ASS/F/142(1)/2025-26/0001",
        "issue_date": "2026-01-19",
        "receipt_date": "2026-01-21",
        "due_date": "2026-05-19",
        "hearing_date": None,
        "financial_year": None,
        "assessment_year": "2024-25",
        "authority": "ACIT Circle 3(1), Mumbai",
        "demand_amount": None,
        "issues": ["Information requisition -- large cash transactions AY 2024-25"],
        "documents_required": [
            "Cash Flow Statement FY 2023-24",
            "Bank statements (all accounts) for FY 2023-24",
            "Form 26AS / AIS reconciliation",
            "Fixed Asset Register as on 31-Mar-2024",
        ],
        "parse_confidence": 0.94,
        "fields_needing_review": [],
    },
    # ── Mock Notice 4: IT 143(2) Scrutiny · Patil Trading · AY 2023-24 ─────────
    "IT_143_2_Patil_AY2324.pdf": {
        "document_type": "IT_143(2)",
        "client_name_on_document": "Patil Trading Company",
        "pans_extracted": [{"value": "APBPP1234D", "location": "header"}],
        "gstins_extracted": [],
        "notice_number": "ITBA/ASS/F/143(2)/2025-26/0048",
        "din_or_rfn": "ITBA/ASS/F/143(2)/2025-26/0048",
        "issue_date": "2026-01-16",
        "receipt_date": "2026-01-18",
        "due_date": "2026-05-16",
        "hearing_date": None,
        "financial_year": None,
        "assessment_year": "2023-24",
        "authority": "ITO Ward 9(1), Mumbai",
        "demand_amount": None,
        "issues": [
            "Limited scrutiny (CASS) -- sundry creditors verification",
            "Purchases from cancelled GSTIN vendors",
            "Commission and brokerage expense query",
        ],
        "documents_required": [
            "Audited balance sheet and P&L for FY 2022-23",
            "Creditor-wise ledger and confirmation letters",
            "Purchase invoices from flagged suppliers",
            "Form 26AS / AIS reconciliation AY 2023-24",
        ],
        "parse_confidence": 0.92,
        "fields_needing_review": [],
    },
    # ── Mock Notice 5: GST DRC-01A · Dr. Anjali Kapoor · Delhi · FY 2023-24 ────
    "DRC01A_DrKapoor_DL_FY2324.pdf": {
        "document_type": "DRC-01A",
        "client_name_on_document": "Dr. Anjali Kapoor",
        "pans_extracted": [{"value": "AANPK7890F", "location": "header"}],
        "gstins_extracted": [
            {"value": "07AANPK7890F1ZD", "location": "header", "state_code": "07"}
        ],
        "notice_number": "DRC0124DL0014721",
        "din_or_rfn": "DIN20260124DRC01A007891",
        "issue_date": "2026-01-24",
        "receipt_date": "2026-01-26",
        "due_date": "2026-05-24",
        "hearing_date": None,
        "financial_year": "2023-24",
        "assessment_year": None,
        "authority": "State Tax Officer, Delhi North Division",
        "demand_amount": 8000,
        "issues": ["Late GSTR-1 filing fee for all four quarters of FY 2023-24"],
        "documents_required": ["Payment of Rs. 8,000 via DRC-03 on GST portal"],
        "parse_confidence": 0.96,
        "fields_needing_review": [],
    },
    # ── Mock Notice 6: IT 148A Reassessment · Verma Software · AY 2022-23 ──────
    "IT_148_Reassessment_Verma_AY2223.pdf": {
        "document_type": "IT_148_148A",
        "client_name_on_document": "Verma Software Solutions LLP",
        "pans_extracted": [{"value": "AADFV4321E", "location": "header"}],
        "gstins_extracted": [],
        "notice_number": "ITBA/AST/F/148A/2025-26/0019",
        "din_or_rfn": "ITBA/AST/F/148A/2025-26/0019",
        "issue_date": "2026-01-10",
        "receipt_date": "2026-01-13",
        "due_date": "2026-05-10",
        "hearing_date": None,
        "financial_year": None,
        "assessment_year": "2022-23",
        "authority": "DCIT Circle 4(2), Bengaluru",
        "demand_amount": 32500000,
        "issues": [
            "Reassessment u/s 148A(b) -- unexplained share capital Rs. 2.5 Cr (Section 68)",
            "Unexplained foreign remittance Rs. 75 lakh (Section 69A)",
        ],
        "documents_required": [
            "Share subscription agreements and share certificates",
            "Board resolution for share issue",
            "Investor KYC and 3-year IT returns",
            "FIRC and service agreements for foreign remittances",
            "Transfer pricing documentation u/s 92CE if applicable",
        ],
        "parse_confidence": 0.89,
        "fields_needing_review": ["demand_amount"],
    },
}


def lookup_canned_response(user_prompt: str) -> str | None:
    """Return canned JSON string for the agent that issued this prompt.

    Routing priority:
      1. Drafting agent (Sprint 5) — recognised by the "MATTER CONTEXT" header.
      2. Document parsing agent (Sprint 3) — keyed off the original filename.

    Demo fallback: when ``STUB_DEFAULT_CANNED`` is set (e.g. to
    ``acme_mh_asmt10``), any parsing prompt that doesn't match a specific
    filename falls back to the named canned entry. This lets a partner drop
    *any* PDF onto /inbox and watch the whole route+draft flow without
    renaming the file to "GST Notice.pdf" first.

    Returns None if no entry matches and no fallback is configured, in which
    case the caller falls back to the StubLLMProvider default of "{}".
    """
    if "MATTER CONTEXT" in user_prompt:
        return _drafting_canned(user_prompt)
    if "PARSED FIELDS" in user_prompt or "STRUCTURED EXTRACTION" in user_prompt or ("CLIENT" in user_prompt and "REGISTRATION" in user_prompt):
        return _triage_canned(user_prompt)
    for filename, payload in _CANNED_BY_FILENAME.items():
        if filename in user_prompt:
            return json.dumps(payload)

    fallback_key = (get_settings().stub_default_canned or "").strip()
    if fallback_key:
        payload = _FALLBACK_BY_KEY.get(fallback_key)
        if payload is not None:
            return json.dumps(payload)
    return None


# Friendly keys for the STUB_DEFAULT_CANNED env var. Pick whichever matches
# the matter you want every uploaded PDF to route to during a demo.
_FALLBACK_BY_KEY: dict[str, dict[str, Any]] = {
    "acme_mh_asmt10": _CANNED_BY_FILENAME["GST Notice.pdf"],
    "drc01a_anomaly": _CANNED_BY_FILENAME["drc-01.pdf"],
}


# ---- Drafting-agent canned responses --------------------------------------


def _drafting_canned(user_prompt: str) -> str:
    """Build a deterministic 15-section draft from the prompt.

    The stub doesn't ship pre-canned response strings per notice because the
    matter pool grows over time; instead it parses the registration identifier
    + notice type out of the user prompt and assembles a generic-but-plausible
    draft so the workflow end-to-end exercises every code path. Real Anthropic
    output looks materially different and partners should always run with a
    real provider in front of clients.
    """
    notice_type = _extract_after(user_prompt, "Type: ") or "GST notice"
    legal_name = (
        _extract_after(user_prompt, "Client: ").split(" (")[0]
        if "Client: " in user_prompt
        else "the assessee"
    )
    pan = _extract_pan(user_prompt) or "—"
    reg = _extract_after(user_prompt, "Registration: ") or "—"
    fy_or_ay = _extract_after(user_prompt, "Financial year / Assessment year: ") or "—"
    tone = _extract_after(user_prompt, "Tone: ") or "formal"
    authority = _extract_after(user_prompt, "Authority: ") or "the learned officer"
    due_date = (
        _extract_after(user_prompt, "Due date (parsed): ")
        or _extract_after(user_prompt, "Due date: ")
        or "—"
    )
    law = _extract_after(user_prompt, "Law: ") or ""

    is_hearing = "HEARING" in notice_type.upper()
    # Keep the canned draft regime-consistent: Income Tax replies cite
    # direct-tax authority and the faceless-assessment hearing provision;
    # GST replies keep the indirect-tax authorities.
    from app.agents.regime_guidance import hearing_provision, regime_for_law

    is_direct = regime_for_law(law) == "DIRECT"
    hearing_ref = hearing_provision(law, notice_type)

    def p(html: str) -> str:
        return f"<p>{html}</p>"

    def cite(case: str, prop: str) -> str:
        return (
            f'<span class="draft-citation" data-status="VERIFIED">{case}</span>'
            f" for the proposition that {prop}"
        )

    sections = [
        {
            "num": 1,
            "title": "Executive Summary",
            "body_html": p(
                f"This reply is filed on behalf of {legal_name} (PAN {pan}) "
                f"in response to the {notice_type} dated {due_date} issued by "
                f"{authority} for {fy_or_ay}. The notice is replied to in full; "
                "every issue raised is addressed paragraph-wise in Section 05, "
                "supported by the documents listed in Section 10."
            ),
        },
        {
            "num": 2,
            "title": "Notice Understanding",
            "body_html": p(
                f"The Department has issued a {notice_type} alleging the "
                "matters set out in the issue text. The assessee&rsquo;s "
                "position and the legal grounds in support are set out below."
            ),
        },
        {
            "num": 3,
            "title": "Factual Background",
            "body_html": p(
                f"{legal_name} holds {reg}. The relevant financial period is "
                f"{fy_or_ay}. All books of account, returns, and supporting "
                "records pertaining to this registration are maintained at "
                "the registered place of business and are available for "
                "verification. [DOCUMENT REQUESTED — pending from client]"
            ),
        },
        {
            "num": 4,
            "title": "Issue-wise Response",
            "body_html": p(
                "Issue 1 — The assessee respectfully submits that the alleged "
                "discrepancy is reconcilable on the face of the records. A "
                "detailed reconciliation is annexed."
            )
            + p(
                "Issue 2 — Without prejudice, the assessee further submits "
                "that even if any discrepancy is held to subsist, no demand "
                "can be fastened in the absence of a finding of suppression."
            ),
        },
        {
            "num": 5,
            "title": "Para-wise Reply",
            "body_html": p(
                "Para 1 — Admitted to the extent of the identity of the "
                "assessee and the registration; the rest of the para is "
                "denied as alleged."
            )
            + p(
                "Para 2 — The figures referenced in the notice are accepted; "
                "the inference drawn from them is denied for the reasons in "
                "Section 06."
            ),
        },
        {
            "num": 6,
            "title": "Legal Submissions",
            "body_html": (
                p(
                    "It is well settled that an addition under the Income-tax "
                    "Act, 1961 cannot rest on material that has not been put "
                    "to the assessee — "
                    + cite(
                        "Kishinchand Chellaram v. CIT, (1980) 125 ITR 713 (SC)",
                        "material relied upon against the assessee must be "
                        "disclosed and an opportunity given to rebut it",
                    )
                    + ". The assessee's books and the enclosed annexures "
                    "fully explain the items under scrutiny."
                )
                if is_direct
                else p(
                    "It is well settled that a demand under the GST Acts "
                    "cannot rest on material withheld from the noticee — "
                    + cite(
                        "Andaman Timber Industries v. Commissioner of "
                        "Central Excise (SC, 2015)",
                        "material relied upon must be disclosed and its "
                        "makers offered for cross-examination",
                    )
                    + ". The reasoning extends to scrutiny notices: "
                    + cite(
                        "Larsen &amp; Toubro Ltd. v. State of Karnataka, "
                        "(2014) 1 SCC 708, para 18",
                        "reconciliation is sufficient response where the records "
                        "bear out the position",
                    )
                    + "."
                )
            ),
        },
        {
            "num": 7,
            "title": "Procedural Objections",
            "body_html": p(
                "Without prejudice to the merits, the assessee reserves the "
                "right to raise procedural objections in case the notice is "
                "found to have been issued beyond the period of limitation "
                "prescribed under the relevant statute."
            ),
        },
    ]

    if is_hearing:
        sections.append(
            {
                "num": 8,
                "title": "Cross-Examination Request",
                "body_html": p(
                    "Insofar as the notice relies on a third-party statement, "
                    "the assessee invokes the right to cross-examine the "
                    "deponent before any reliance is placed against it. "
                    + cite(
                        "Andaman Timber Industries v. CCE, "
                        "(2015) 324 ELT 641 (SC)",
                        "denial of cross-examination of a witness whose "
                        "statement is relied upon vitiates the proceedings",
                    )
                    + "."
                ),
            }
        )

    sections.extend(
        [
            {
                "num": 10,
                "title": "Documents Enclosed",
                "body_html": p(
                    "1. Copy of the impugned notice.\n"
                    "2. Reconciliation working.\n"
                    "3. Statutory returns for the relevant period."
                ),
            },
            {
                "num": 11,
                "title": "Annexure Index",
                "body_html": p(
                    "Annexure A — Reconciliation working.\n"
                    "Annexure B — Statutory returns."
                ),
            },
            {
                "num": 12,
                "title": "Prayer",
                "body_html": p(
                    "In light of the above, it is most respectfully prayed "
                    "that the proceedings initiated vide the captioned notice "
                    "be dropped and any consequential demand be set aside, and "
                    "that the assessee be granted an opportunity of personal "
                    f"hearing under {hearing_ref} before any adverse order is "
                    "passed."
                ),
            },
            {
                "num": 13,
                "title": "Internal Partner Note",
                "body_html": p(
                    '<span class="draft-internal-note">Officer historically '
                    "open to reconciliation-based closure. Push for personal "
                    "hearing only if reply is rejected on the first round."
                    "</span>"
                ),
            },
            {
                "num": 14,
                "title": "Client Summary",
                "body_html": p(
                    f"{legal_name}: the Department has issued a "
                    f"{notice_type} for {fy_or_ay}. We have prepared a "
                    "complete reply with the necessary reconciliation; "
                    "please review and sign so we can file before "
                    f"{due_date}."
                ),
            },
            {
                "num": 15,
                "title": "Filing Checklist",
                "body_html": p(
                    "&bull; Verify all annexures are paginated and stamped.\n"
                    "&bull; Obtain the partner&rsquo;s signature on the reply.\n"
                    + (
                        "&bull; File via the Income Tax e-filing portal "
                        "(e-Proceedings) and save the acknowledgement.\n"
                        if is_direct
                        else "&bull; File via the GST portal acknowledgement (or "
                        "email to the listed authority address).\n"
                    )
                    + "&bull; Diary the acknowledgement and update lifecycle "
                    "to Reply Submitted."
                ),
            },
        ]
    )

    payload = {
        "sections": sections,
        "internal_partner_note": (
            "Officer historically open to reconciliation-based closure. Push "
            "for personal hearing only if reply is rejected on the first "
            f"round. Tone applied: {tone}."
        ),
        "client_summary": (
            f"{legal_name}: the Department has issued a {notice_type} for "
            f"{fy_or_ay}. We have prepared a complete reply with the "
            "necessary reconciliation; please review and sign so we can "
            f"file before {due_date}."
        ),
    }
    return json.dumps(payload)


def _extract_after(haystack: str, marker: str) -> str:
    idx = haystack.find(marker)
    if idx == -1:
        return ""
    line_end = haystack.find("\n", idx)
    return haystack[idx + len(marker) : line_end if line_end != -1 else None].strip()


def _extract_pan(haystack: str) -> str | None:
    m = re.search(r"PAN ([A-Z]{5}[0-9]{4}[A-Z])", haystack)
    return m.group(1) if m else None


def _triage_canned(user_prompt: str) -> str:
    legal_name = _extract_after(user_prompt, "Legal name        : ") or "the assessee"
    doc_type = _extract_after(user_prompt, "Document type     : ") or "tax notice"
    period = _extract_after(user_prompt, "Period            : ") or "the relevant period"
    demand = _extract_after(user_prompt, "Demand amount     : ") or "unspecified"

    summary = (
        f"The proper officer has issued a {doc_type} alleging discrepancies and statutory non-compliance for {period} in respect of {legal_name}. The demand involved is recorded as {demand}.\n\n"
        f"Procedurally, a detailed reply must be filed along with evidentiary supporting documents before the statutory due date to prevent adverse ex-parte assessment or demand confirmation.\n\n"
        f"Technically, the response requires reconciling the declared figures against statutory returns and books of account, supported by relevant invoices, ledgers, and proof of payment."
    )

    checklist = [
        {
            "label": "Annual / Periodic Return Reconciliation",
            "rationale": "Reconcile outward and inward supplies declared in returns against financial statements.",
            "doc_type": "reconciliation",
            "is_required": True,
            "position": 1,
        },
        {
            "label": "Relevant Tax Invoices & Supply Ledgers",
            "rationale": "Substantiate actual receipt of goods/services and correctness of tax charged/availed.",
            "doc_type": "invoice",
            "is_required": True,
            "position": 2,
        },
        {
            "label": "Bank Statements Showing Payments",
            "rationale": "Evidencing actual consideration paid and tax discharged through official banking channels.",
            "doc_type": "bank_statement",
            "is_required": True,
            "position": 3,
        },
        {
            "label": "Statutory Return Filings (GSTR-3B / ITR)",
            "rationale": "Confirm periodic compliance and timely discharge of tax liability.",
            "doc_type": "gstr_3b",
            "is_required": False,
            "position": 4,
        },
    ]
    return json.dumps({"summary": summary, "checklist": checklist})


