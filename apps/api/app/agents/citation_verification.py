"""Citation Verification Agent — Tier 1 (IndianKanoon).

For each citation embedded in a draft, the verifier:

1. Extracts ``case_name`` and ``citation_string`` from the draft HTML.
2. Looks up the citation in ``citation_cache``; if found and < 30 days old,
   returns the cached verdict.
3. Otherwise queries the configured Tier-1 provider (IndianKanoon free API or
   a stub) and writes the result back to the cache.
4. Returns one of VERIFIED / VERIFIED_PARTIAL / UNVERIFIED. The workflow
   strips UNVERIFIED citations and adds a footnote — no UNVERIFIED citation
   ever reaches the partner's screen.

Tier 2 (Taxmann / SCC paid feeds) and Tier 3 (firm private library) are
Phase 2/3; the interface is structured so adding them is a config flip.
"""

from __future__ import annotations

import abc
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.citation_whitelist import lookup_canonical
from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

CACHE_TTL = timedelta(days=30)


# ---- Extraction ----------------------------------------------------------


_CITATION_SPAN_RE = re.compile(
    r'<span\s+class="draft-citation"[^>]*>(?P<text>.*?)</span>',
    re.IGNORECASE | re.DOTALL,
)
# A loose split: "Case Name v. Other, (2021) 6 SCC 257, para 7"
#   → case_name = "Case Name v. Other", citation_string = "(2021) 6 SCC 257, para 7"
# Robust enough for Tier 1; the verifier doesn't need a parser.
_SPLIT_AT_FIRST_COMMA = re.compile(r"^(?P<case>[^,]+(?:\sv\.?\s[^,]+))(?:,\s*(?P<rest>.*))?$")


@dataclass(frozen=True, slots=True)
class ExtractedCitation:
    """A citation pulled out of a draft section."""

    section_num: int
    raw_text: str
    case_name: str
    citation_string: str | None
    paragraph_referenced: str | None


def extract_citations(sections: list[dict[str, Any]]) -> list[ExtractedCitation]:
    """Pull every <span class="draft-citation"> from every section's body_html.

    Splits on the first comma into (case_name, rest); tries to locate
    "para N" in the rest as paragraph_referenced.
    """
    out: list[ExtractedCitation] = []
    for s in sections:
        body = s.get("body_html", "")
        section_num = int(s.get("num", 0))
        for m in _CITATION_SPAN_RE.finditer(body):
            raw = re.sub(r"\s+", " ", m.group("text")).strip()
            case_name, citation_string, paragraph = _split_citation(raw)
            out.append(
                ExtractedCitation(
                    section_num=section_num,
                    raw_text=raw,
                    case_name=case_name,
                    citation_string=citation_string,
                    paragraph_referenced=paragraph,
                )
            )
    return out


def _split_citation(raw: str) -> tuple[str, str | None, str | None]:
    """Return (case_name, citation_string, paragraph_referenced)."""
    # Pull paragraph reference if present.
    para = None
    para_match = re.search(r",\s*(para\s+\d+[A-Za-z]?)\s*$", raw, re.IGNORECASE)
    rest_after_para = raw
    if para_match:
        para = para_match.group(1).lower()
        rest_after_para = raw[: para_match.start()].rstrip(", ")

    m = _SPLIT_AT_FIRST_COMMA.match(rest_after_para)
    if m:
        case = m.group("case").strip()
        rest = (m.group("rest") or "").strip() or None
        return case, rest, para

    # No "v." pattern found — treat the whole string as case_name.
    return rest_after_para.strip(), None, para


# ---- Verifier interface --------------------------------------------------


@dataclass(frozen=True, slots=True)
class VerificationResult:
    status: str  # VERIFIED | VERIFIED_PARTIAL | UNVERIFIED
    source_url: str | None
    verified_paragraph_text: str | None
    proposition_match_confidence: float | None
    raw_response: dict[str, Any]


class CitationProvider(abc.ABC):
    name: str

    @abc.abstractmethod
    async def verify(
        self,
        case_name: str,
        citation_string: str | None,
        *,
        regime: str | None = None,
    ) -> VerificationResult:
        ...


# ---- Stub provider (offline dev / CI) ------------------------------------


_STUB_VERIFIED: dict[str, str] = {
    # case-name substring → IndianKanoon-style URL (placeholder)
    "larsen": "https://indiankanoon.org/doc/00000002/",
    "andaman timber": "https://indiankanoon.org/doc/00000003/",
}


class StubCitationProvider(CitationProvider):
    name = "stub"

    async def verify(
        self,
        case_name: str,
        citation_string: str | None,
        *,
        regime: str | None = None,
    ) -> VerificationResult:
        needle = case_name.lower()
        for key, url in _STUB_VERIFIED.items():
            if key in needle:
                return VerificationResult(
                    status="VERIFIED",
                    source_url=url,
                    verified_paragraph_text=(
                        f"Stubbed verification of {case_name}; in production "
                        "this is the actual paragraph text retrieved from "
                        "IndianKanoon."
                    ),
                    proposition_match_confidence=0.92,
                    raw_response={"stub": True, "case_name": case_name},
                )
        # Unknown citation: caller will mark UNVERIFIED → STRIPPED.
        return VerificationResult(
            status="UNVERIFIED",
            source_url=None,
            verified_paragraph_text=None,
            proposition_match_confidence=None,
            raw_response={"stub": True, "case_name": case_name, "matched": False},
        )


# ---- IndianKanoon provider (Tier 1) --------------------------------------


class IndianKanoonProvider(CitationProvider):
    """Tier 1 verifier against the IndianKanoon free search API.

    The free search endpoint is unauthenticated and rate-limited; we use the
    "find by case name" path and accept the first hit whose year matches if
    the citation_string includes one. A more sophisticated extractor (and
    paid IK API key) lands in Phase 2.
    """

    name = "indiankanoon"
    BASE_URL = "https://api.indiankanoon.org/search/"
    TIMEOUT = httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0)

    def __init__(self, api_token: str | None = None) -> None:
        self._token = (
            api_token
            if api_token is not None
            else (get_settings().indiankanoon_api_token or "")
        )

    async def verify(
        self,
        case_name: str,
        citation_string: str | None,
        *,
        regime: str | None = None,
    ) -> VerificationResult:
        # Scope the search to the courts / tribunals of the notice's regime
        # so an Income Tax reply isn't "verified" against a CESTAT ruling.
        query = case_name
        if regime == "DIRECT":
            query = f"{case_name} doctypes: supremecourt,highcourts,itat"
        params: dict[str, str] = {"formInput": query}
        headers: dict[str, str] = {"User-Agent": "NoticeDesk/0.1"}
        if self._token:
            headers["Authorization"] = f"Token {self._token}"
        try:
            async with httpx.AsyncClient(timeout=self.TIMEOUT) as client:
                resp = await client.post(self.BASE_URL, data=params, headers=headers)
        except httpx.HTTPError as e:
            logger.warning("indiankanoon transient error: %s", e)
            return VerificationResult(
                status="UNVERIFIED",
                source_url=None,
                verified_paragraph_text=None,
                proposition_match_confidence=None,
                raw_response={"error": str(e)},
            )

        if resp.status_code != 200:
            return VerificationResult(
                status="UNVERIFIED",
                source_url=None,
                verified_paragraph_text=None,
                proposition_match_confidence=None,
                raw_response={
                    "status_code": resp.status_code,
                    "body_preview": resp.text[:280],
                },
            )

        try:
            data = resp.json()
        except ValueError:
            return VerificationResult(
                status="UNVERIFIED",
                source_url=None,
                verified_paragraph_text=None,
                proposition_match_confidence=None,
                raw_response={"body_preview": resp.text[:280]},
            )

        docs = data.get("docs") or []
        if not docs:
            return VerificationResult(
                status="UNVERIFIED",
                source_url=None,
                verified_paragraph_text=None,
                proposition_match_confidence=None,
                raw_response=data,
            )

        # Pick first doc; if citation_string contains a year, require match.
        wanted_year = _year_in(citation_string or "")
        chosen = None
        for doc in docs:
            if wanted_year is None or wanted_year in str(doc.get("title", "")):
                chosen = doc
                break
        if chosen is None:
            return VerificationResult(
                status="VERIFIED_PARTIAL",
                source_url=_doc_url(docs[0]),
                verified_paragraph_text=None,
                proposition_match_confidence=0.5,
                raw_response={"first_doc_only": True, "docs": docs[:3]},
            )

        return VerificationResult(
            status="VERIFIED",
            source_url=_doc_url(chosen),
            verified_paragraph_text=None,  # paragraph fetch is Tier 2 work
            proposition_match_confidence=0.85,
            raw_response={"matched_doc": chosen},
        )


def _year_in(s: str) -> str | None:
    m = re.search(r"\b(19|20)\d{2}\b", s)
    return m.group(0) if m else None


def _doc_url(doc: dict[str, Any]) -> str | None:
    tid = doc.get("tid")
    return f"https://indiankanoon.org/doc/{tid}/" if tid else None


# ---- Cache + orchestration ----------------------------------------------


def _normalise(case_name: str) -> str:
    cleaned = re.sub(r"\s+", " ", case_name).strip().lower()
    cleaned = re.sub(r"[\.\,]+", "", cleaned)
    return cleaned


async def _cache_get(
    session: AsyncSession, *, case_name: str, citation_string: str | None, provider: str
) -> VerificationResult | None:
    row = (
        await session.execute(
            text(
                """
                SELECT status, source_url, verified_paragraph_text, raw_response,
                       verified_at
                FROM citation_cache
                WHERE case_name_normalized = :name
                  AND COALESCE(citation_string, '') = COALESCE(:cs, '')
                  AND provider = :provider
                """
            ),
            {
                "name": _normalise(case_name),
                "cs": citation_string,
                "provider": provider,
            },
        )
    ).mappings().first()
    if row is None:
        return None
    if row["verified_at"] + CACHE_TTL < datetime.now(UTC):
        return None
    return VerificationResult(
        status=row["status"],
        source_url=row["source_url"],
        verified_paragraph_text=row["verified_paragraph_text"],
        proposition_match_confidence=None,
        raw_response=row["raw_response"] or {},
    )


async def _cache_put(
    session: AsyncSession,
    *,
    case_name: str,
    citation_string: str | None,
    provider: str,
    result: VerificationResult,
) -> None:
    # Don't cache UNVERIFIED — case law lookups change as IndianKanoon's
    # corpus grows, and we don't want a transient miss to stick for 30 days.
    if result.status == "UNVERIFIED":
        return
    import json

    await session.execute(
        text(
            """
            INSERT INTO citation_cache (
                case_name_normalized, citation_string, provider,
                status, source_url, verified_paragraph_text, raw_response
            ) VALUES (
                :name, :cs, :provider, :status, :url, :ptext, CAST(:raw AS JSONB)
            )
            ON CONFLICT (case_name_normalized, citation_string, provider)
            DO UPDATE SET
                status = EXCLUDED.status,
                source_url = EXCLUDED.source_url,
                verified_paragraph_text = EXCLUDED.verified_paragraph_text,
                raw_response = EXCLUDED.raw_response,
                verified_at = NOW()
            """
        ),
        {
            "name": _normalise(case_name),
            "cs": citation_string,
            "provider": provider,
            "status": result.status,
            "url": result.source_url,
            "ptext": result.verified_paragraph_text,
            "raw": json.dumps(result.raw_response or {}),
        },
    )



# ---- Gemini LLM provider (Tier 1 AI verifier) -----------------------------


class GeminiCitationProvider(CitationProvider):
    """AI citation verifier using Gemini / OpenAI-compatible endpoint.

    Verifies whether a cited judgment genuinely exists in Indian law,
    extracts its court, deciding year, legal proposition, and canonical URL.
    """

    name = "gemini"

    SYSTEM_PROMPT = """You are an expert Indian tax law citation verifier.
Given a case name and citation string, verify if it is a genuine Indian court judgment.
Return a JSON object with:
- is_genuine (boolean)
- court (string or null)
- year (string or null)
- indiankanoon_url (string URL if known, else null)
- proposition (string summarizing the ruling, or null)
- confidence (float between 0.0 and 1.0)
- regime ("DIRECT" for Income-tax Act / ITAT matters, "INDIRECT" for GST /
  Central Excise / Customs / Service Tax / VAT / CESTAT matters, "ANY" for
  general constitutional / natural-justice rulings)
"""

    async def verify(
        self,
        case_name: str,
        citation_string: str | None,
        *,
        regime: str | None = None,
    ) -> VerificationResult:
        from app.core.config import get_settings
        from app.services.llm.openai import OpenAIProvider

        s = get_settings()
        api_key = s.gemini_api_key or s.openai_api_key
        if not api_key:
            return VerificationResult(
                status="UNVERIFIED",
                source_url=None,
                verified_paragraph_text=None,
                proposition_match_confidence=None,
                raw_response={"error": "no API key configured for gemini citation verifier"},
            )

        provider = OpenAIProvider(
            api_key=api_key,
            model=s.openai_model,
            base_url=s.openai_base_url or None,
        )

        user_prompt = f"Case: {case_name}"
        if citation_string:
            user_prompt += f", {citation_string}"
        if regime:
            user_prompt += f"\nThe citing reply is a {regime} tax matter."

        try:
            resp = await provider.generate_text(
                system=self.SYSTEM_PROMPT,
                user=user_prompt,
                temperature=0.0,
            )
            data = json.loads(resp.content)
        except Exception as e:
            logger.warning("gemini citation verification error: %s", e)
            return VerificationResult(
                status="UNVERIFIED",
                source_url=None,
                verified_paragraph_text=None,
                proposition_match_confidence=None,
                raw_response={"error": str(e)},
            )

        is_genuine = data.get("is_genuine", False)
        confidence = float(data.get("confidence") or 0.0)

        if not is_genuine:
            return VerificationResult(
                status="UNVERIFIED",
                source_url=None,
                verified_paragraph_text=None,
                proposition_match_confidence=0.0,
                raw_response=data,
            )

        status = "VERIFIED" if confidence >= 0.8 else "VERIFIED_PARTIAL"
        proposition = data.get("proposition")
        case_regime = str(data.get("regime") or "").upper()
        if regime and case_regime in ("DIRECT", "INDIRECT") and case_regime != regime:
            status = "VERIFIED_PARTIAL"
            proposition = _cross_regime_note(case_regime, regime) + (
                f" {proposition}" if proposition else ""
            )
        return VerificationResult(
            status=status,
            source_url=data.get("indiankanoon_url"),
            verified_paragraph_text=proposition,
            proposition_match_confidence=confidence,
            raw_response=data,
        )


def get_provider() -> CitationProvider:
    """Pick the configured Tier 1 provider.

    Defaults to the stub so dev / CI runs offline.
    'gemini' or 'indiankanoon' (without token) uses the live Gemini citation verifier.
    'indiankanoon' with INDIANKANOON_API_TOKEN uses the IndianKanoon API.
    """
    s = get_settings()
    name = (s.citation_provider or "stub").lower()
    if name in ("gemini", "openai"):
        return GeminiCitationProvider()
    if name == "indiankanoon":
        token = s.indiankanoon_api_token or ""
        if token:
            return IndianKanoonProvider(api_token=token)
        return GeminiCitationProvider()
    return StubCitationProvider()


@dataclass(frozen=True, slots=True)
class VerifiedCitation:
    extracted: ExtractedCitation
    status: str
    source_url: str | None
    verified_paragraph_text: str | None
    proposition_match_confidence: float | None
    action_taken: str  # 'passed' | 'flagged' | 'stripped'


def _cross_regime_note(case_regime: str, draft_regime: str) -> str:
    label = {
        "DIRECT": "direct-tax (Income-tax)",
        "INDIRECT": "indirect-tax (GST / excise / customs)",
    }
    return (
        f"Cross-regime authority: a {label.get(case_regime, case_regime)} "
        f"decision cited in a {label.get(draft_regime, draft_regime)} reply "
        "— confirm it is genuinely applicable or replace it before filing."
    )


async def verify_citations(
    session: AsyncSession,
    extracted: list[ExtractedCitation],
    provider: CitationProvider | None = None,
    *,
    regime: str | None = None,
) -> list[VerifiedCitation]:
    """Verify each extracted citation.

    Resolution order for each citation:
      1. Canonical whitelist (``citation_whitelist.py``) — covers ~15 leading
         tax-law authorities (Pushpam, Suncraft, D.Y. Beathel, Cosmic Dye,
         etc.) that the IndianKanoon free-tier search consistently ranks
         below noise. Whitelist hits are always VERIFIED with the canonical
         URL; no network call.
      2. citation_cache table (30-day TTL) — repeat lookups across drafts
         skip the network.
      3. Configured provider (stub or IndianKanoon).

    ``regime`` ("DIRECT" / "INDIRECT", from the matter's law) scopes the
    lookup: authorities from the other tax regime are downgraded to
    VERIFIED_PARTIAL (flagged for the partner) instead of passing silently.
    """
    p = provider or get_provider()
    # Regime-scoped lookups can resolve differently, so they cache apart.
    cache_provider = f"{p.name}:{regime}" if regime else p.name
    out: list[VerifiedCitation] = []
    for ec in extracted:
        # 1. Canonical whitelist short-circuit.
        canonical = lookup_canonical(ec.case_name)
        if canonical is not None:
            cross_regime = regime is not None and canonical.regime not in (regime, "ANY")
            result = VerificationResult(
                status="VERIFIED_PARTIAL" if cross_regime else "VERIFIED",
                source_url=canonical.canonical_url,
                verified_paragraph_text=(
                    f"{_cross_regime_note(canonical.regime, regime)} {canonical.proposition}"
                    if cross_regime
                    else canonical.proposition
                ),
                proposition_match_confidence=0.4 if cross_regime else 0.99,
                raw_response={
                    "canonical": True,
                    "name": canonical.name,
                    "regime": canonical.regime,
                    "cross_regime": cross_regime,
                },
            )
        else:
            # 2. Cache.
            cached = await _cache_get(
                session,
                case_name=ec.case_name,
                citation_string=ec.citation_string,
                provider=cache_provider,
            )
            # 3. Provider call.
            result = cached if cached is not None else await p.verify(
                ec.case_name, ec.citation_string, regime=regime
            )
            if cached is None:
                await _cache_put(
                    session,
                    case_name=ec.case_name,
                    citation_string=ec.citation_string,
                    provider=cache_provider,
                    result=result,
                )
        action = {
            "VERIFIED": "passed",
            "VERIFIED_PARTIAL": "flagged",
            "UNVERIFIED": "stripped",
        }[result.status]
        out.append(
            VerifiedCitation(
                extracted=ec,
                status=result.status,
                source_url=result.source_url,
                verified_paragraph_text=result.verified_paragraph_text,
                proposition_match_confidence=result.proposition_match_confidence,
                action_taken=action,
            )
        )
    return out


# ---- Strip + footnote ----------------------------------------------------


_STRIPPED_PLACEHOLDER = (
    '<span class="draft-citation--stripped" '
    'title="Citation removed because verification failed">'
    "[citation removed]"
    "</span>"
)


def apply_stripped_citations(
    sections: list[dict[str, Any]], verified: list[VerifiedCitation]
) -> tuple[list[dict[str, Any]], list[str]]:
    """Replace UNVERIFIED citation spans with the [citation removed] marker
    and return (updated_sections, footnote_lines).
    """
    footnote_lines: list[str] = []
    stripped_raws = {v.extracted.raw_text for v in verified if v.status == "UNVERIFIED"}
    if not stripped_raws:
        # Still update VERIFIED / VERIFIED_PARTIAL status attributes inline so
        # the UI can render the chip accurately.
        return _restamp_statuses(sections, verified), []

    rebuilt: list[dict[str, Any]] = []
    for s in sections:
        body = s.get("body_html", "")
        for m in _CITATION_SPAN_RE.finditer(body):
            raw = re.sub(r"\s+", " ", m.group("text")).strip()
            if raw in stripped_raws:
                body = body.replace(m.group(0), _STRIPPED_PLACEHOLDER, 1)
                footnote_lines.append(f"• {raw} — verification failed; removed from final reply.")
        rebuilt.append({**s, "body_html": body})

    rebuilt = _restamp_statuses(rebuilt, verified)
    return rebuilt, footnote_lines


def _restamp_statuses(
    sections: list[dict[str, Any]], verified: list[VerifiedCitation]
) -> list[dict[str, Any]]:
    """Re-emit data-status="VERIFIED|VERIFIED_PARTIAL" on each kept citation
    so the UI doesn't have to look up the citation table separately.
    """
    by_raw = {v.extracted.raw_text: v.status for v in verified if v.status != "UNVERIFIED"}
    if not by_raw:
        return sections
    out: list[dict[str, Any]] = []
    for s in sections:
        body = s.get("body_html", "")

        def repl(m: re.Match[str]) -> str:
            raw = re.sub(r"\s+", " ", m.group("text")).strip()
            status = by_raw.get(raw)
            if status is None:
                return m.group(0)
            return re.sub(
                r'data-status="[^"]*"',
                f'data-status="{status}"',
                m.group(0),
                count=1,
            ) if 'data-status=' in m.group(0) else m.group(0).replace(
                'class="draft-citation"',
                f'class="draft-citation" data-status="{status}"',
                1,
            )

        out.append({**s, "body_html": _CITATION_SPAN_RE.sub(repl, body)})
    return out


def append_footnote(sections: list[dict[str, Any]], lines: list[str]) -> list[dict[str, Any]]:
    """Add a footnote section at the bottom listing stripped citations."""
    if not lines:
        return sections
    footnote_html = (
        '<div class="draft-footnote"><strong>Citations removed:</strong><br>'
        + "<br>".join(lines)
        + "</div>"
    )
    last_num = max((int(s.get("num", 0)) for s in sections), default=0)
    return [
        *sections,
        {
            "num": last_num + 1,
            "title": "Footnotes",
            "body_html": footnote_html,
        },
    ]
