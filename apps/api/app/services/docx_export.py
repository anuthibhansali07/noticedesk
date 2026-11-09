"""Word export for drafts.

Produces .docx in Vipul's filing-grade preferences:
  - Times New Roman, 12pt body, 14pt section headers
  - A4 page, 1-inch margins
  - No hyperlinks (citations as plain text + paragraph reference)
  - Footer: page number, tenant firm name
  - Cover sheet on Filing version with client/PAN/registration/notice/FY/AY/due

Three modes:
  - filing   → excludes Section 13 (internal note), Section 14 (client summary)
               and Section 15 (filing checklist — the firm's to-do list).
               Also scrubs [ASSUMED] / [DOCUMENT REQUESTED] internal markers and
               drops the "Footnotes — Citations removed" section (those are
               working-paper artefacts that must never reach the department).
  - client   → excludes Sections 13 and 15; includes Section 14. Same scrubbing as
               filing (the client doesn't need to see firm-internal todos).
  - internal → full draft including all sections + internal note. Markers
               and footnotes survive.

Sections are renumbered sequentially in the rendered document regardless of
gaps in the source (Section 08 omitted when no third-party statement, etc.) —
the reader sees 01, 02, 03, … not 01, 02, 03, …, 07, 10, 11.

The HTML inside body_html is parsed minimally: <p>, <br>, <ol>, <ul>, <li>,
<span> are respected; everything else is stripped.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any, Literal

from docx import Document
from docx.enum.section import WD_ORIENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.shared import Cm, Pt

ExportMode = Literal["filing", "client", "internal"]


# Strip-internal markers — written by the drafter as partner-actionable
# todos that must not survive into anything the client or the department
# sees. Matches the literal bracketed forms the prompt emits.
_INTERNAL_MARKER_RE = re.compile(
    r"\[(ASSUMED[^\]]*|DOCUMENT REQUESTED[^\]]*|TODO[^\]]*)\]",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class CoverSheetData:
    firm_name: str
    client_legal_name: str
    client_pan: str
    registration_label: str
    registration_identifier: str
    notice_type: str | None
    fy_or_ay: str
    authority: str | None
    due_date: str | None


# Section 15 (Filing Checklist) is the firm's own to-do list ("verify DIN",
# "obtain DSC") — it must not reach the department or the client.
FILING_CHECKLIST_NUM = 15


def _exclude_sections(mode: ExportMode) -> set[int]:
    if mode == "filing":
        return {13, 14, FILING_CHECKLIST_NUM}
    if mode == "client":
        return {13, FILING_CHECKLIST_NUM}
    return set()


def _is_filing_checklist(s: dict[str, Any]) -> bool:
    # By title too: a reply-format override can renumber the checklist.
    return "filing checklist" in str(s.get("title", "")).strip().lower()


# The marker plus the spacing / punctuation around it. Removing the bare
# marker turned "time. [DOCUMENT REQUESTED — ...]." into "time. ." in the
# filing copy; this form eats the leading space and, when the marker sat
# between two sentence ends, the duplicate full stop after it.
_MARKER_IN_CONTEXT_RE = re.compile(
    r"(?P<pre>[.;:!?])?[ \t]*" + _INTERNAL_MARKER_RE.pattern + r"(?P<post>[.,;:])?",
    re.IGNORECASE,
)


def _drop_marker(m: re.Match[str]) -> str:
    pre = m.group("pre") or ""
    post = m.group("post") or ""
    return pre if pre else post


# "<li>[DOCUMENT REQUESTED] Bank statements" leaves "<li> Bank statements".
_LEADING_SPACE_RE = re.compile(r"(<(?:p|li)>)[ \t]+", re.IGNORECASE)


def _scrub_internal_markers(html: str) -> str:
    """Remove [ASSUMED ...] / [DOCUMENT REQUESTED ...] markers from body html."""
    out = _MARKER_IN_CONTEXT_RE.sub(_drop_marker, html)
    return _LEADING_SPACE_RE.sub(r"\1", out)


def _is_footnote_section(s: dict[str, Any]) -> bool:
    title = str(s.get("title", "")).strip().lower()
    return title == "footnotes" or "citations removed" in title


def render_draft_docx(
    *,
    mode: ExportMode,
    cover: CoverSheetData,
    sections: list[dict[str, Any]],
    internal_partner_note: str | None,
) -> bytes:
    doc = Document()
    _configure_page(doc)
    _configure_default_style(doc)
    _add_footer(doc, cover.firm_name)

    # Cover sheet (Filing version only — the brief calls it out for filing).
    if mode == "filing":
        _add_cover_sheet(doc, cover)
        doc.add_page_break()

    exclude = _exclude_sections(mode)
    include_inline_internal = mode == "internal"
    # Scrub working-paper artefacts in filing + client modes. Internal mode
    # keeps everything so the partner can see what the model flagged.
    scrub_internal_markers = mode != "internal"
    drop_footnote_section = mode != "internal"

    # Renumber sequentially so the reader sees 01, 02, 03 … instead of
    # gaps where conditional sections (08 cross-examination, 09 cross-
    # registration, 13 internal note, 14 client summary) were skipped.
    display_num = 0
    for s in sections:
        num = int(s.get("num", 0))
        if num in exclude:
            continue
        if mode != "internal" and _is_filing_checklist(s):
            continue
        if drop_footnote_section and _is_footnote_section(s):
            continue
        display_num += 1
        title = str(s.get("title", "")).strip()
        body = str(s.get("body_html", ""))
        if scrub_internal_markers:
            body = _scrub_internal_markers(body)
        # Section header line: e.g. "01. Executive Summary"
        h = doc.add_paragraph()
        h.paragraph_format.space_before = Pt(12)
        h.paragraph_format.space_after = Pt(4)
        run = h.add_run(f"{display_num:02d}. {title}")
        run.bold = True
        run.font.size = Pt(14)
        _render_body_html(doc, body, include_inline_internal=include_inline_internal)

    # Internal partner note (Internal mode only).
    if include_inline_internal and internal_partner_note:
        h = doc.add_paragraph()
        h.paragraph_format.space_before = Pt(12)
        run = h.add_run("Internal Partner Note")
        run.bold = True
        run.font.size = Pt(14)
        body = doc.add_paragraph(internal_partner_note)
        body.paragraph_format.left_indent = Cm(0.6)
        for r in body.runs:
            r.italic = True

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _configure_page(doc: Document) -> None:
    for section in doc.sections:
        section.orientation = WD_ORIENT.PORTRAIT
        section.page_height = Cm(29.7)
        section.page_width = Cm(21.0)
        section.left_margin = Cm(2.54)  # 1 inch
        section.right_margin = Cm(2.54)
        section.top_margin = Cm(2.54)
        section.bottom_margin = Cm(2.54)


def _configure_default_style(doc: Document) -> None:
    style = doc.styles["Normal"]
    style.font.name = "Times New Roman"
    # Set the East-Asian font slot too so Word renders consistently on every OS.
    # rPr / rFonts are OOXML element names — keep the original casing.
    r_pr = style.element.get_or_add_rPr()  # type: ignore[attr-defined]
    r_fonts = r_pr.find(qn("w:rFonts"))
    if r_fonts is None:
        from docx.oxml import OxmlElement

        r_fonts = OxmlElement("w:rFonts")
        r_pr.append(r_fonts)
    r_fonts.set(qn("w:ascii"), "Times New Roman")
    r_fonts.set(qn("w:hAnsi"), "Times New Roman")
    r_fonts.set(qn("w:cs"), "Times New Roman")
    r_fonts.set(qn("w:eastAsia"), "Times New Roman")
    style.font.size = Pt(12)


def _add_footer(doc: Document, firm_name: str) -> None:
    for section in doc.sections:
        footer = section.footer
        para = footer.paragraphs[0]
        para.alignment = WD_ALIGN_PARAGRAPH.CENTER
        run = para.add_run(f"{firm_name} · ")
        run.font.size = Pt(9)
        run.font.name = "Times New Roman"
        _add_page_number(para)


def _add_page_number(paragraph) -> None:
    from docx.oxml import OxmlElement

    run = paragraph.add_run()
    fld_begin = OxmlElement("w:fldChar")
    fld_begin.set(qn("w:fldCharType"), "begin")
    run._r.append(fld_begin)  # type: ignore[attr-defined]
    instr = OxmlElement("w:instrText")
    instr.text = "PAGE"
    run._r.append(instr)  # type: ignore[attr-defined]
    fld_end = OxmlElement("w:fldChar")
    fld_end.set(qn("w:fldCharType"), "end")
    run._r.append(fld_end)  # type: ignore[attr-defined]
    run.font.size = Pt(9)
    run.font.name = "Times New Roman"


def _add_cover_sheet(doc: Document, cover: CoverSheetData) -> None:
    title = doc.add_paragraph()
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    title_run = title.add_run("REPLY TO NOTICE")
    title_run.bold = True
    title_run.font.size = Pt(20)
    title.paragraph_format.space_after = Pt(36)

    rows: list[tuple[str, str]] = [
        ("Client", cover.client_legal_name),
        ("PAN", cover.client_pan),
        (cover.registration_label, cover.registration_identifier),
        ("Notice", cover.notice_type or "—"),
        ("Period", cover.fy_or_ay),
        ("Authority", cover.authority or "—"),
        ("Due date", cover.due_date or "—"),
    ]

    table = doc.add_table(rows=len(rows), cols=2)
    table.autofit = False
    for i, (label, value) in enumerate(rows):
        cells = table.rows[i].cells
        cells[0].width = Cm(5.5)
        cells[1].width = Cm(11.5)
        run0 = cells[0].paragraphs[0].add_run(label)
        run0.bold = True
        run0.font.size = Pt(12)
        run0.font.name = "Times New Roman"
        run1 = cells[1].paragraphs[0].add_run(value)
        run1.font.size = Pt(12)
        run1.font.name = "Times New Roman"


# ---- HTML body rendering -------------------------------------------------


class _HtmlToDocx(HTMLParser):
    """Streams <p>, <br>, <ol>, <ul>, <li>, <span> from body_html into a Document.

    Cite spans render as italic plain text (no hyperlink — flat string per
    Vipul's filing preferences). Stripped-citation spans become "[citation
    removed]" with italic styling. Internal-note spans honour the mode flag.

    Lists: <ol><li>foo</li><li>bar</li></ol> each <li> becomes its own paragraph
    using Word's "List Number" / "List Bullet" style. This is what fixes the
    run-on Filing Checklist that previous drafts concatenated.
    """

    def __init__(self, doc: Document, include_inline_internal: bool) -> None:
        super().__init__()
        self._doc = doc
        self._include_inline_internal = include_inline_internal
        self._current_para = None
        self._span_stack: list[str] = []  # class names of currently-open spans
        self._drop_text = False  # True when inside an internal-note span we're dropping
        self._list_stack: list[str] = []  # 'ol' / 'ul' for the current list nesting

    def _ensure_para(self):
        if self._current_para is None:
            self._current_para = self._doc.add_paragraph()
            self._current_para.paragraph_format.space_after = Pt(6)
        return self._current_para

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        if tag == "p":
            self._current_para = self._doc.add_paragraph()
            self._current_para.paragraph_format.space_after = Pt(6)
        elif tag == "br":
            # Treat as paragraph break for clean line spacing in Word.
            self._current_para = None
        elif tag in ("ol", "ul"):
            self._list_stack.append(tag)
            self._current_para = None
        elif tag == "li":
            # Each list item is its own paragraph using the matching Word
            # style — "List Number" gives 1. 2. 3. autonumbering;
            # "List Bullet" gives bullets. Falls back to manual prefix if
            # the style isn't available in this docx template.
            kind = self._list_stack[-1] if self._list_stack else "ul"
            style = "List Number" if kind == "ol" else "List Bullet"
            try:
                self._current_para = self._doc.add_paragraph(style=style)
            except KeyError:
                self._current_para = self._doc.add_paragraph()
                prefix = "• " if kind == "ul" else f"{len(self._doc.paragraphs)}. "
                self._current_para.add_run(prefix)
            self._current_para.paragraph_format.space_after = Pt(3)
        elif tag == "span":
            cls = a.get("class") or ""
            self._span_stack.append(cls)
            if "draft-internal-note" in cls and not self._include_inline_internal:
                self._drop_text = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "p":
            self._current_para = None
        elif tag in ("ol", "ul"):
            if self._list_stack:
                self._list_stack.pop()
            self._current_para = None
        elif tag == "li":
            self._current_para = None
        elif tag == "span" and self._span_stack:
            cls = self._span_stack.pop()
            if "draft-internal-note" in cls and not self._include_inline_internal:
                # Only un-drop if no remaining internal-note span open above us.
                self._drop_text = any(
                    "draft-internal-note" in c for c in self._span_stack
                )

    def handle_data(self, data: str) -> None:
        if self._drop_text:
            return
        text = re.sub(r"\s+", " ", data)
        if not text.strip():
            # Only emit whitespace when we're mid-paragraph; otherwise skip.
            if self._current_para is None:
                return
        para = self._ensure_para()
        cls = self._span_stack[-1] if self._span_stack else ""
        run = para.add_run(text)
        run.font.size = Pt(12)
        run.font.name = "Times New Roman"
        if "draft-citation" in cls:
            run.italic = True
        if "draft-citation--stripped" in cls:
            run.italic = True
            run.font.size = Pt(10)


def _render_body_html(
    doc: Document, html: str, *, include_inline_internal: bool
) -> None:
    parser = _HtmlToDocx(doc, include_inline_internal)
    parser.feed(html)
    parser.close()
