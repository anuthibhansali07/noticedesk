"""Regression tests for the Nov 2026 drafting-quality audit fixes.

Covers: 429 retry / fallback in the OpenAI-compatible provider, clean HTTP
429 / 502 translation (no "triage failed: triage failed: openai ..." on the
wire), regime-scoped drafting guidance + citation verification, faceless
hearing provision for Income Tax, and reply-format profiles.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.agents.citation_verification import ExtractedCitation, verify_citations
from app.agents.drafting import DraftingInput, _load_prompt_template, _render_user_prompt
from app.agents.regime_guidance import (
    hearing_provision,
    regime_for_law,
    render_format_profile,
    render_regime_guidance,
)
from app.core.errors import (
    AIProviderError,
    RateLimitedError,
    install_error_handlers,
    llm_error_to_app_error,
)
from app.services.llm.base import (
    JsonSchemaValidationError,
    LLMError,
    LLMRateLimitError,
    is_rate_limit_exception,
    retry_after_from_exception,
)
from app.services.llm.openai import OpenAIProvider

# ---- Fake OpenAI SDK -------------------------------------------------------


class _Fake429Error(Exception):
    status_code = 429

    def __init__(self, retry_after: str | None = None) -> None:
        super().__init__(
            "Error code: 429 - {'error': {'code': 429, 'message': 'Resource has been "
            "exhausted (e.g. check quota).', 'status': 'RESOURCE_EXHAUSTED'}}"
        )
        self.response = SimpleNamespace(
            status_code=429, headers={"retry-after": retry_after} if retry_after else {}
        )


class _FakeCompletions:
    def __init__(self, script: dict[str, list[Any]]) -> None:
        self.script = script
        self.calls: list[str] = []

    async def create(self, *, model: str, **_: Any) -> Any:
        self.calls.append(model)
        outcome = self.script[model].pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return SimpleNamespace(
            id="x",
            choices=[SimpleNamespace(message=SimpleNamespace(content=outcome))],
            usage=None,
        )


def _provider(script: dict[str, list[Any]], **kw: Any) -> tuple[OpenAIProvider, _FakeCompletions]:
    comp = _FakeCompletions(script)
    client = SimpleNamespace(chat=SimpleNamespace(completions=comp))
    return OpenAIProvider(api_key="test", client=client, **kw), comp


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


# ---- 429 detection + retry ------------------------------------------------


def test_rate_limit_detection_and_retry_after_parsing() -> None:
    assert is_rate_limit_exception(_Fake429Error())
    assert retry_after_from_exception(_Fake429Error("12")) == 12.0
    gemini_body = Exception("429 RESOURCE_EXHAUSTED ... 'retryDelay': '23s'")
    assert is_rate_limit_exception(gemini_body)
    assert retry_after_from_exception(gemini_body) == 23.0
    assert not is_rate_limit_exception(Exception("Error code: 400 bad request"))
    assert is_rate_limit_exception(Exception("Error code: 503 - The model is overloaded."))


def test_openai_retries_429_then_succeeds() -> None:
    p, comp = _provider(
        {"m1": [_Fake429Error(), '{"ok": true}']}, model="m1", rate_limit_retry_delays=(0,)
    )
    resp = _run(p.generate_text(system="s", user="u"))
    assert resp.content == '{"ok": true}'
    assert comp.calls == ["m1", "m1"]


def test_openai_falls_back_to_next_model_when_quota_exhausted() -> None:
    p, comp = _provider(
        {"m1": [_Fake429Error(), _Fake429Error()], "m2": ['{"ok": 1}']},
        model="m1",
        rate_limit_retry_delays=(0,),
        fallback_models=("m2",),
    )
    resp = _run(p.generate_text(system="s", user="u"))
    assert resp.model == "m2"
    assert comp.calls == ["m1", "m1", "m2"]


def test_openai_gives_up_without_sleeping_on_long_retry_after() -> None:
    # Retry-After of 10 minutes (daily quota) must not hang the request.
    p, comp = _provider({"m1": [_Fake429Error("600")]}, model="m1", rate_limit_retry_delays=(5, 5))
    with pytest.raises(LLMRateLimitError) as ei:
        _run(p.generate_text(system="s", user="u"))
    assert ei.value.retry_after_seconds == 600.0
    assert comp.calls == ["m1"]


# ---- Error translation -----------------------------------------------------


def test_llm_rate_limit_maps_to_clean_429() -> None:
    inner = LLMRateLimitError(
        "openai rate limited (m1): Error code: 429 ...", retry_after_seconds=12
    )
    try:
        try:
            raise inner
        except LLMRateLimitError as e:
            raise LLMError(f"triage failed: {e}") from e
    except LLMError as wrapped:
        err = llm_error_to_app_error(wrapped, action="triage")
    assert isinstance(err, RateLimitedError)
    assert err.http_status == 429
    assert err.retry_after_seconds == 12
    assert "openai" not in err.message and "triage failed: triage failed" not in err.message


def test_other_llm_errors_map_to_502_without_provider_internals() -> None:
    err = llm_error_to_app_error(
        LLMError("drafting failed: drafter returned non-JSON: ..."), action="draft"
    )
    assert isinstance(err, AIProviderError)
    assert err.http_status == 502
    assert "non-JSON" not in err.message


def test_rate_limited_response_has_retry_after_header() -> None:
    app = FastAPI()
    install_error_handlers(app)

    @app.get("/boom")
    async def boom() -> None:
        raise llm_error_to_app_error(LLMRateLimitError("x"), action="draft")

    r = TestClient(app).get("/boom")
    assert r.status_code == 429
    assert r.headers["retry-after"] == "30"
    body = r.json()["error"]
    assert body["code"] == "rate_limited"
    assert body["details"]["retry_after_seconds"] == 30


def test_triage_agent_prefers_rate_limit_cause(monkeypatch: pytest.MonkeyPatch) -> None:
    """Primary 429 + secondary bad JSON → the surfaced cause is the 429."""
    from app.agents import notice_triage

    class _P:
        def __init__(self, name: str, exc: Exception) -> None:
            self.name, self._exc = name, exc

        async def generate_text(self, **_: Any) -> Any:
            raise self._exc

    monkeypatch.setattr(
        notice_triage, "get_llm_for_agent", lambda _a: _P("p", LLMRateLimitError("429"))
    )
    monkeypatch.setattr(
        notice_triage,
        "get_secondary_llm_for_agent",
        lambda _a: _P("s", JsonSchemaValidationError("bad")),
    )
    monkeypatch.setattr(notice_triage, "_render_user_prompt", lambda _t, _ti: "u")
    with pytest.raises(LLMError) as ei:
        _run(notice_triage.generate_triage(SimpleNamespace()))  # type: ignore[arg-type]
    assert isinstance(llm_error_to_app_error(ei.value, action="triage"), RateLimitedError)


# ---- Regime guidance + hearing provision ---------------------------------


def test_income_tax_guidance_is_direct_tax_only() -> None:
    g = render_regime_guidance("IT", "IT_143(2)")
    assert regime_for_law("IT") == "DIRECT"
    assert "144B(6)(vii)" in g and "144B(7)" in g
    assert "Whittle Anderson" in g and "Shaan Finance" in g and "Mysore Minerals" in g
    assert "Pushpam" not in g and "Cosmic Dye" not in g
    assert "142(2)" not in hearing_provision("IT", "IT_143(2)")


def test_gst_guidance_is_indirect_tax_only() -> None:
    g = render_regime_guidance("GST", "DRC-01")
    assert "75(4)" in g and "Pushpam" in g
    assert "Kelvinator" not in g and "Whittle Anderson" not in g
    # Natural-justice authority is citable in either regime.
    assert "Andaman Timber" in g and "Andaman Timber" in render_regime_guidance("IT", None)


def test_format_profiles_render() -> None:
    adj = render_format_profile("adjournment")
    assert "ADJOURNMENT" in adj and "No citations" in adj
    assert "<table>" in render_format_profile("tabular_reconciliation")
    assert "COMPREHENSIVE" in render_format_profile("bogus")


def _drafting_input(law: str, profile: str = "full_submission") -> DraftingInput:
    return DraftingInput(
        matter_id=uuid4(),
        tone="formal",
        partner_instructions="",
        client_legal_name="Dr. Test Client",
        client_pan="AAAPT1234A",
        client_entity_type="individual",
        registration_type=law,
        registration_identifier="AAAPT1234A",
        registration_state_name=None,
        law=law,
        financial_year=None,
        assessment_year="2023-24",
        notice={"document_type": "IT_143(2)", "due_date": "2026-06-05"},
        raw_extracted_json={},
        format_profile=profile,  # type: ignore[arg-type]
    )


def test_prompt_v4_renders_regime_and_format_blocks_without_placeholders() -> None:
    system, template = _load_prompt_template()
    assert "REGIME GUIDANCE" in system
    assert "Pushpam Pharmaceuticals Co. v. Collector" not in system  # no hard-coded GST list
    user = _render_user_prompt(template, _drafting_input("IT", "tabular_reconciliation"))
    assert "144B(6)(vii)" in user
    assert "TABULAR RECONCILIATION" in user
    assert "{regime_guidance_block}" not in user and "{format_profile_block}" not in user


# ---- Citation verifier: regime scoping ------------------------------------


def _cite(name: str) -> ExtractedCitation:
    return ExtractedCitation(
        section_num=6,
        raw_text=name,
        case_name=name,
        citation_string=None,
        paragraph_referenced=None,
    )


def test_cross_regime_canonical_citation_is_flagged_not_passed() -> None:
    # Whitelist hits never touch the session / network.
    out = _run(
        verify_citations(
            None,  # type: ignore[arg-type]
            [
                _cite("Pushpam Pharmaceuticals Co. v. Collector of Central Excise"),
                _cite("CIT v. Whittle Anderson Ltd"),
                _cite("Andaman Timber Industries v. CCE"),
            ],
            regime="DIRECT",
        )
    )
    by_name = {v.extracted.case_name.split()[0]: v for v in out}
    assert by_name["Pushpam"].status == "VERIFIED_PARTIAL"
    assert by_name["Pushpam"].action_taken == "flagged"
    assert "Cross-regime" in (by_name["Pushpam"].verified_paragraph_text or "")
    assert by_name["CIT"].status == "VERIFIED"
    assert by_name["Andaman"].status == "VERIFIED"


def test_canonical_citation_passes_without_regime() -> None:
    out = _run(verify_citations(None, [_cite("Pushpam Pharmaceuticals v. CCE")]))  # type: ignore[arg-type]
    assert out[0].status == "VERIFIED"


# ---- End-to-end DRC-01 run fixes (Nov 2026) ----------------------------------


def _export_text(mode: str, sections: list[dict[str, Any]]) -> str:
    import io

    from docx import Document

    from app.services.docx_export import CoverSheetData, render_draft_docx

    cover = CoverSheetData(
        firm_name="Firm",
        client_legal_name="Client",
        client_pan="AAAAA0000A",
        registration_label="GSTIN",
        registration_identifier="36AAAAA0000A1Z5",
        notice_type="DRC-01",
        fy_or_ay="FY 2023-24",
        authority="STO",
        due_date="2026-06-12",
    )
    blob = render_draft_docx(
        mode=mode,  # type: ignore[arg-type]
        cover=cover,
        sections=sections,
        internal_partner_note=None,
    )
    return "\n".join(p.text for p in Document(io.BytesIO(blob)).paragraphs)


_SECTIONS = [
    {"num": 4, "title": "Issue-wise Response", "body_html": "<p>Reply.</p>"},
    {"num": 15, "title": "Filing Checklist", "body_html": "<ol><li>Verify DIN</li></ol>"},
]


@pytest.mark.parametrize("mode", ["filing", "client"])
def test_filing_checklist_never_leaves_the_firm(mode: str) -> None:
    out = _export_text(mode, _SECTIONS)
    assert "Filing Checklist" not in out
    assert "Verify DIN" not in out


def test_filing_checklist_kept_in_internal_export() -> None:
    assert "Filing Checklist" in _export_text("internal", _SECTIONS)


def test_filing_checklist_dropped_by_title_when_renumbered() -> None:
    sections = [{"num": 11, "title": "Filing Checklist", "body_html": "<p>Obtain DSC</p>"}]
    assert "Obtain DSC" not in _export_text("filing", sections)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (
            "<p>at the material time. [DOCUMENT REQUESTED — pending from client: Proof].</p>",
            "<p>at the material time.</p>",
        ),
        ("the invoices [ASSUMED — please verify] were genuine.", "the invoices were genuine."),
        ("Rs 4,80,000 [ASSUMED — please verify].", "Rs 4,80,000."),
        ("first [ASSUMED], second", "first, second"),
        ("He said... and then. [TODO x]", "He said... and then."),
        (
            "<li>[DOCUMENT REQUESTED — pending from client] Bank statements.</li>",
            "<li>Bank statements.</li>",
        ),
    ],
)
def test_marker_scrub_leaves_no_stray_punctuation(raw: str, expected: str) -> None:
    from app.services.docx_export import _scrub_internal_markers

    assert _scrub_internal_markers(raw) == expected


def test_gemini_ocr_key_sent_in_header_not_url(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    from app.services.ocr import gemini

    seen: dict[str, Any] = {}

    async def fake_post(self: Any, url: str, **kwargs: Any) -> httpx.Response:
        seen["url"] = url
        seen["headers"] = kwargs.get("headers") or {}
        return httpx.Response(
            200,
            json={"candidates": [{"content": {"parts": [{"text": "=== PAGE 1 ===\nx"}]}}]},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    provider = gemini.GeminiOCRProvider(api_key="secret-key-123", model="m")
    _run(provider.extract(b"%PDF-1.4", "application/pdf"))
    assert "secret-key-123" not in seen["url"]
    assert seen["headers"].get("x-goog-api-key") == "secret-key-123"


def test_bharti_airtel_not_whitelisted_for_taxpayer() -> None:
    from app.agents.citation_whitelist import CANONICAL_CASES

    assert not any("bharti" in k for k in CANONICAL_CASES)


def test_prompt_v4_carries_legal_accuracy_rules() -> None:
    from pathlib import Path

    import app.agents.drafting as drafting

    md = (Path(drafting.__file__).parent / "prompts" / "drafting_v4.md").read_text(encoding="utf-8")
    assert "within 30 days of the order" not in md
    assert "ISSUE OF THE SHOW CAUSE NOTICE" in md
    assert "Section 155" in md
    assert "statutory opportunity" not in md
    assert "(2023) 12 GSTR 45" not in md


# ---- Database unreachable → 503, not "internal server error" -----------------


def _db_down_error() -> OSError:
    # Exactly what asyncpg raises on Windows when nothing listens on 5432.
    return OSError(
        "Multiple exceptions: [Errno 10061] Connect call failed ('::1', 5432, 0, 0), "
        "[Errno 10061] Connect call failed ('127.0.0.1', 5432)"
    )


def test_is_db_unavailable_recognises_connection_failures() -> None:
    from sqlalchemy.exc import OperationalError

    from app.core.errors import is_db_unavailable

    assert is_db_unavailable(_db_down_error())
    assert is_db_unavailable(ConnectionRefusedError(10061, "refused"))
    wrapped = RuntimeError("request failed")
    wrapped.__cause__ = _db_down_error()
    assert is_db_unavailable(wrapped)
    assert is_db_unavailable(
        OperationalError("SELECT 1", {}, Exception("could not connect to server"))
    )
    # Ordinary failures stay ordinary.
    assert not is_db_unavailable(FileNotFoundError("tenants/x/file.pdf"))
    assert not is_db_unavailable(ValueError("bad input"))


def test_db_down_returns_503_with_clear_message() -> None:
    app = FastAPI()
    install_error_handlers(app)

    @app.post("/upload")
    async def _upload() -> None:
        raise _db_down_error()

    res = TestClient(app, raise_server_exceptions=False).post("/upload")
    assert res.status_code == 503
    body = res.json()["error"]
    assert body["code"] == "database_unavailable"
    assert "PostgreSQL" in body["message"]
    assert res.headers["retry-after"] == "30"


# ---- Manual assignment: dates reach asyncpg as datetime.date ----------------


def test_coerce_date_handles_parser_strings() -> None:
    from datetime import date

    from app.agents.notice_routing import _coerce_date

    assert _coerce_date("2026-10-28") == date(2026, 10, 28)
    assert _coerce_date(" 2026-10-28 ") == date(2026, 10, 28)
    assert _coerce_date(date(2026, 1, 2)) == date(2026, 1, 2)
    assert _coerce_date(None) is None
    # Unparseable parser output becomes NULL instead of a 500.
    assert _coerce_date("28/10/2026") is None
    assert _coerce_date("not a date") is None


def test_manual_route_notice_insert_coerces_dates() -> None:
    import inspect

    from app.routes import routing

    src = inspect.getsource(routing._create_notice_override)
    for field in ("issue_date", "receipt_date", "due_date", "hearing_date"):
        assert f'_coerce_date(parsed.get("{field}"))' in src


# ---- OCR: busy provider is retried, falls back, and reports the real reason --


def test_ocr_retries_transient_then_succeeds() -> None:
    from app.services.ocr.base import ExtractedDoc, OCRTransientError
    from app.workflows.document_ocr import _try_with_retries

    calls = {"n": 0}

    class Flaky:
        name = "flaky"

        async def extract(self, file_bytes: bytes, mime_type: str) -> ExtractedDoc:
            calls["n"] += 1
            if calls["n"] < 3:
                raise OCRTransientError("busy (503)")
            return ExtractedDoc(text="ok", layout_json={}, page_count=1, provider_name="flaky")

    doc, err = _run(_try_with_retries(Flaky(), b"x", "application/pdf", delays=(0, 0, 0)))
    assert doc is not None and err is None and calls["n"] == 3


def test_ocr_failure_message_keeps_provider_reason() -> None:
    from app.services.ocr.base import OCRError, OCRTransientError
    from app.workflows.document_ocr import _failure_message

    busy = _failure_message(OCRTransientError("Gemini OCR is busy (503): high demand"))
    assert "high demand" in busy and "Retry OCR" in busy
    assert "Retry OCR" not in _failure_message(OCRError("bad pdf"))


def test_gemini_ocr_falls_back_to_next_model_when_busy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx

    from app.services.ocr import gemini

    tried: list[str] = []

    async def fake_post(self: Any, url: str, **kwargs: Any) -> httpx.Response:
        model = url.rsplit("/models/", 1)[1].split(":")[0]
        tried.append(model)
        req = httpx.Request("POST", url)
        if model == "primary-model":
            return httpx.Response(
                503,
                json={"error": {"message": "This model is currently experiencing high demand."}},
                request=req,
            )
        return httpx.Response(
            200,
            json={"candidates": [{"content": {"parts": [{"text": "=== PAGE 1 ===\nx"}]}}]},
            request=req,
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    provider = gemini.GeminiOCRProvider(
        api_key="k", model="primary-model", fallback_models=["backup-model"]
    )
    out = _run(provider.extract(b"%PDF-1.4", "application/pdf"))
    assert tried == ["primary-model", "backup-model"]
    assert out.layout_json["model"] == "backup-model"


def test_gemini_ocr_busy_error_is_short_and_transient(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    from app.services.ocr import gemini
    from app.services.ocr.base import OCRTransientError

    async def fake_post(self: Any, url: str, **kwargs: Any) -> httpx.Response:
        return httpx.Response(
            503,
            json={"error": {"code": 503, "message": "high demand", "status": "UNAVAILABLE"}},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    provider = gemini.GeminiOCRProvider(api_key="k", model="m")
    with pytest.raises(OCRTransientError) as ei:
        _run(provider.extract(b"%PDF-1.4", "application/pdf"))
    assert "high demand" in str(ei.value) and "UNAVAILABLE" not in str(ei.value)


# ---- Inbox "awaiting review" excludes routed and rejected rows ---------------


def test_inbox_list_reports_awaiting_review_separately() -> None:
    import inspect

    from app.models.inbox import InboxList
    from app.routes import documents

    assert "awaiting_review" in InboxList.model_fields
    src = inspect.getsource(documents.list_inbox)
    assert "NOT IN ('routed', 'rejected')" in src
    assert "awaiting_review=" in src
