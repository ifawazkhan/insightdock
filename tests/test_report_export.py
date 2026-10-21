from dataclasses import replace
from datetime import datetime, timezone
from io import BytesIO
import re
from unittest.mock import Mock, patch

import pytest

from chatbot.db import QueryResult
from chatbot.report_document import ReportSection, build_document
from chatbot.reports import ReportError, ReportEvidence, build_report
from chatbot import report_export


def report(raw=False):
    sql = "SELECT name,email,salary FROM employees" if raw else "SELECT department,COUNT(*) AS record_count FROM employees GROUP BY department"
    result = (QueryResult(["name", "email", "salary"], [["Private Person", "private@example.com", 90000]], 0, False, .1)
              if raw else QueryResult(["department", "record_count"], [["Sales <script>", 2], ["研发", 3]], 0, False, .1))
    item = ReportEvidence("q1", "Private query question", sql, result, datetime.now(timezone.utc), "snapshot")
    return build_report("Business <b>report</b> & overview", (item,), scope=("demo", None), source="snapshot")


def test_pdf_valid_in_memory_escape_and_branding():
    pytest.importorskip("reportlab")
    with patch.object(report_export, "_chart_png", side_effect=ImportError), patch("socket.create_connection", side_effect=AssertionError("Network forbidden")):
        data = report_export.report_pdf(report())
    assert isinstance(data, bytes) and data.startswith(b"%PDF-") and b"%%EOF" in data
    assert len(data) <= report_export.MAX_PDF_BYTES
    assert "Fawaz Khan" in report_export.BRANDING


def test_pdf_omits_sql_private_rows_and_fallback_has_sections():
    pytest.importorskip("reportlab")
    from reportlab.platypus import Paragraph
    captured = []
    original = Paragraph.__init__

    def capture(self, text, *args, **kwargs):
        if text is not None:
            captured.append(text)
        original(self, text, *args, **kwargs)

    with patch.object(Paragraph, "__init__", capture), patch.object(report_export, "_chart_png", side_effect=RuntimeError("private error")):
        report_export.report_pdf(report())
    text = " ".join(captured)
    assert "&lt;b&gt;report&lt;/b&gt;" in text
    assert "Chart unavailable" in text and "Method:" in text and "Recommendations" not in text
    assert "Data Limitations" in text and "Suggested Follow-up" not in text
    assert "private error" not in text and "SELECT" not in text
    captured.clear()
    with patch.object(Paragraph, "__init__", capture):
        report_export.report_pdf(report(raw=True))
    text = " ".join(captured)
    assert "Private Person" not in text and "private@example.com" not in text and "90000" not in text
    assert "Private query question" not in text


def test_pdf_sql_keyword_passed_to_new_visualization_api():
    pytest.importorskip("reportlab")
    from chatbot.visualization import Visualization
    selector = Mock(return_value=Visualization("table", ""))
    with patch.object(report_export, "choose_visualization", selector):
        report_export.report_pdf(report())
    assert selector.call_args.kwargs["sql"].startswith("SELECT")


def test_pdf_chart_image_embedded():
    pytest.importorskip("reportlab")
    # Pillow is present with ReportLab. Avoid requiring vl-convert for unit tests.
    from PIL import Image
    buffer = BytesIO()
    Image.new("RGB", (100, 60), "blue").save(buffer, format="PNG")
    with patch.object(report_export, "_chart_png", return_value=buffer.getvalue()):
        data = report_export.report_pdf(report())
    assert b"/Subtype /Image" in data


def test_static_chart_uses_bundled_font_without_mutating_browser_spec(monkeypatch):
    from copy import deepcopy
    from PIL import Image
    import vl_convert

    buffer = BytesIO()
    Image.new("RGB", (100, 60), "blue").save(buffer, format="PNG")
    spec = {"mark": "bar", "data": {"values": []}, "config": {"font": "Segoe UI"}}
    before = deepcopy(spec)
    register = Mock()
    renderer = Mock(return_value=buffer.getvalue())
    monkeypatch.setattr(vl_convert, "register_font_directory", register)
    monkeypatch.setattr(vl_convert, "vegalite_to_png", renderer)
    assert report_export._chart_png(spec).startswith(b"\x89PNG")
    register.assert_called_once()
    config = renderer.call_args.args[0]["config"]
    assert config["font"] == "Bitstream Vera Sans"
    assert config["axis"]["labelColor"] == "#1E293B"
    assert spec == before


def test_pdf_size_limit_and_oversized_text():
    pytest.importorskip("reportlab")
    with patch.object(report_export, "MAX_PDF_BYTES", 100):
        with pytest.raises(ReportError, match="limit"):
            report_export.report_pdf(report())
    with pytest.raises(ReportError, match="text"):
        report_export.report_pdf(replace(report(), summary="x" * 16001))


def test_pdf_long_report_pagination():
    pytest.importorskip("reportlab")
    original = report()
    long_document = replace(build_document(original), recommendations=tuple(
        "Verify definitions and available evidence. " * 15 for _ in range(80)))
    with patch.object(report_export, "build_document", return_value=long_document), \
            patch.object(report_export, "_chart_png", side_effect=ImportError):
        data = report_export.report_pdf(original)
    assert len(re.findall(rb"/Type /Page\b", data)) > 2


@pytest.mark.parametrize("spec", [{"data": {"url": "https://example.com/private"}},
                                  {"mark": {"type": "image", "url": "file:///etc/passwd"}},
                                  {"transform": [{"calculate": {"expr": "fetch('x')"}}]}])
def test_renderer_rejects_external_assets_and_expressions(spec):
    with pytest.raises(ReportError, match="unsafe"):
        report_export._chart_png(spec)


def capture_layout(monkeypatch):
    """Inspect real flowables while retaining ReportLab's actual pagination."""
    pytest.importorskip("reportlab")
    from reportlab.platypus import SimpleDocTemplate

    captured = {}
    original = SimpleDocTemplate.build

    def capture(self, flowables, *args, **kwargs):
        captured["story"] = list(flowables)
        captured["document"] = self
        return original(self, flowables, *args, **kwargs)

    monkeypatch.setattr(SimpleDocTemplate, "build", capture)
    return captured


def test_pdf_shared_document_layout_compact_a4_and_footer(monkeypatch):
    from reportlab.lib.enums import TA_CENTER
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen.canvas import Canvas
    from reportlab.platypus import Image, LongTable, Paragraph, Spacer

    captured = capture_layout(monkeypatch)
    footer = []
    original = Canvas.drawRightString

    def capture_footer(self, x, y, text, *args, **kwargs):
        footer.append(text)
        return original(self, x, y, text, *args, **kwargs)

    monkeypatch.setattr(Canvas, "drawRightString", capture_footer)
    original_report = report()
    client = build_document(original_report)
    with patch.object(report_export, "build_document", wraps=build_document) as builder, \
            patch.object(report_export, "_chart_png", side_effect=ImportError):
        data = report_export.report_pdf(original_report)
    builder.assert_called_once_with(original_report)
    story = captured["story"]
    text = [flow.getPlainText() for flow in story if isinstance(flow, Paragraph)]
    assert text[0] == client.title
    assert story[0].style.alignment == TA_CENTER
    assert text[1].startswith("Generated:") and "UTC" in text[1]
    assert text[2] == "Purpose: " + client.title
    headings = ["Executive Summary", client.sections[0].title, "Key Findings", "Data Limitations"]
    assert "Recommendations" not in text and "Suggested Follow-up" not in text
    assert "No additional items supplied." not in text
    assert [text.index(value) for value in headings] == sorted(text.index(value) for value in headings)
    section_index = next(i for i, flow in enumerate(story)
                         if isinstance(flow, Paragraph) and flow.getPlainText() == client.sections[0].title)
    # ReportLab clears the instance flag after grouping; the style is stable.
    assert story[section_index].style.keepWithNext
    table = story[section_index + 1]
    assert isinstance(table, LongTable) and table.repeatRows == 1
    assert story[section_index + 2].getPlainText() == client.sections[0].commentary
    assert not any(isinstance(flow, (Spacer, Image)) for flow in story)
    assert captured["document"].pagesize == A4
    assert len(re.findall(rb"/Type /Page\b", data)) == 1
    assert footer == ["Page 1"]
    assert "2026" in report_export.BRANDING
    assert table._bkgrndcmds[0][0] == "BACKGROUND"
    assert table._bkgrndcmds[1][0] == "ROWBACKGROUNDS"
    header = table._cellvalues[0][0]
    header = header if isinstance(header, Paragraph) else header[0]
    assert header.style.textColor.hexval() == "0xffffff"


def test_pdf_charts_follow_all_breakdowns_and_use_salary_projection(monkeypatch):
    from PIL import Image as PillowImage
    from chatbot.visualization import choose_visualization
    from reportlab.platypus import Image, LongTable, Paragraph

    captured = capture_layout(monkeypatch)
    base = report()
    salary = replace(base.evidence[0], id="salary-section", question="Salary profile",
                     sql="SELECT department, COUNT(*) AS record_count, AVG(salary) AS average_salary "
                         "FROM employees GROUP BY department",
                     result=QueryResult(["department", "record_count", "average_salary"],
                                        [["Sales", 100, "12345.6789"], ["Support", 30, "20000.4321"]],
                                        0, False, .1))
    original = replace(base, title="Department Salary Profile", evidence=(base.evidence[0], salary))
    before = repr(original)
    png = BytesIO()
    PillowImage.new("RGB", (100, 60), "blue").save(png, format="PNG")
    with patch.object(report_export, "choose_visualization", wraps=choose_visualization) as selector, \
            patch.object(report_export, "_chart_png", return_value=png.getvalue()) as renderer:
        data = report_export.report_pdf(original)
    assert b"/Subtype /Image" in data
    projection, question = selector.call_args_list[0].args
    assert projection.columns == ["department", "average_salary"]
    assert projection.rows[0] == ["Sales", "12345.6789"]
    assert "salary" in question and selector.call_args_list[0].kwargs["sql"] == ""
    salary_spec = renderer.call_args_list[0].args[0]
    assert salary_spec["mark"]["type"] == "bar"
    assert salary_spec["encoding"]["x"]["title"] == "Average Salary"
    story = captured["story"]
    first_image = next(i for i, flow in enumerate(story) if isinstance(flow, Image))
    assert all(i < first_image for i, flow in enumerate(story) if isinstance(flow, LongTable))
    for section in build_document(original).sections:
        assert next(i for i, flow in enumerate(story) if isinstance(flow, Paragraph)
                    and flow.getPlainText() == section.commentary) < first_image
    assert repr(original) == before


def test_pdf_table_caps_have_honest_omission_note(monkeypatch):
    from reportlab.platypus import LongTable, Paragraph

    captured = capture_layout(monkeypatch)
    original = report()
    section = ReportSection("Aggregate Breakdown", tuple(f"Measure {i}" for i in range(13)),
                            tuple(tuple(str(i) for _ in range(13)) for i in range(55)),
                            "Comparison covers all retained aggregate groups.", None, "", "")
    client = replace(build_document(original), sections=(section,))
    with patch.object(report_export, "build_document", return_value=client):
        report_export.report_pdf(original)
    table = next(flow for flow in captured["story"] if isinstance(flow, LongTable))
    assert len(table._cellvalues) == 51 and len(table._cellvalues[0]) == 12
    text = " ".join(flow.getPlainText() for flow in captured["story"] if isinstance(flow, Paragraph))
    assert "first 50 of 55 aggregate rows and 12 of 13 columns" in text
    assert "5 rows and 1 columns omitted" in text
    assert "not just displayed rows" in text and "no totals are extrapolated" in text


def test_pdf_heading_stays_with_split_table_and_headers_repeat(monkeypatch):
    from reportlab.platypus import LongTable, Paragraph, SimpleDocTemplate

    capture_layout(monkeypatch)
    pages = []
    original_after = SimpleDocTemplate.afterFlowable

    def after(self, flow):
        if isinstance(flow, (LongTable, Paragraph)):
            pages.append((self.page, flow))
        return original_after(self, flow)

    monkeypatch.setattr(SimpleDocTemplate, "afterFlowable", after)
    original = report()
    section = ReportSection("Department Breakdown", ("Department", "Record count"),
                            tuple((f"Department {i}", "1,000") for i in range(50)),
                            "All returned groups have the same count.", None, "", "")
    client = replace(build_document(original), summary="Scope and definitions. " * 65, sections=(section,))
    with patch.object(report_export, "build_document", return_value=client):
        data = report_export.report_pdf(original)
    tables = [(page, flow) for page, flow in pages if isinstance(flow, LongTable)]
    assert len(tables) >= 2
    heading_page = next(page for page, flow in pages if isinstance(flow, Paragraph)
                        and flow.getPlainText() == section.title)
    assert heading_page == tables[0][0]
    headers = [table._cellvalues[0][0] for _, table in tables]
    assert all((cell if isinstance(cell, Paragraph) else cell[0]).getPlainText() == "Department"
               for cell in headers)
    last_page = max(page for page, _ in pages)
    assert any(page == last_page and isinstance(flow, Paragraph)
               and flow.getPlainText().startswith("Method:") for page, flow in pages)
    assert len(re.findall(rb"/Type /Page\b", data)) == last_page


@pytest.mark.parametrize("rendered", [b"not a PNG", b"\x89PNG\r\n\x1a\n" + b"x" * (2 * 1024 * 1024)])
def test_renderer_rejects_invalid_or_oversized_png(rendered):
    with patch("vl_convert.vegalite_to_png", return_value=rendered):
        with pytest.raises(ReportError, match="invalid or oversized"):
            report_export._chart_png({"data": {"values": []}})


def test_renderer_rejects_oversized_spec_before_rendering():
    with patch("vl_convert.vegalite_to_png") as renderer:
        with pytest.raises(ReportError, match="oversized"):
            report_export._chart_png({"data": {"values": ["x" * 300001]}})
    renderer.assert_not_called()


def test_pdf_shared_text_is_escaped_and_evidence_ids_removed(monkeypatch):
    from reportlab.platypus import Paragraph

    captured = capture_layout(monkeypatch)
    original = report()
    client = replace(build_document(original), follow_up=f"[{original.evidence[0].id}] Review <pay> & context?")
    with patch.object(report_export, "build_document", return_value=client), \
            patch.object(report_export, "_chart_png", side_effect=RuntimeError("private renderer details")):
        report_export.report_pdf(original)
    paragraphs = [flow for flow in captured["story"] if isinstance(flow, Paragraph)]
    assert any("Review &lt;pay&gt; &amp; context?" in flow.text for flow in paragraphs)
    text = " ".join(flow.text for flow in paragraphs)
    assert "[q1]" not in text and "private renderer details" not in text


def test_pdf_optional_original_question_is_used_without_inventing_field(monkeypatch):
    from types import SimpleNamespace
    from reportlab.platypus import Paragraph

    captured = capture_layout(monkeypatch)
    original = report()
    proxy = SimpleNamespace(**{**vars(original), "question": "How do departments compare <today>?"})
    with patch.object(report_export, "_chart_png", side_effect=ImportError):
        report_export.report_pdf(proxy)
    text = [flow.text for flow in captured["story"] if isinstance(flow, Paragraph)]
    assert "Question: How do departments compare &lt;today&gt;?" in text
    assert not any(value.startswith("Purpose:") for value in text)


@pytest.mark.parametrize("recommendations,follow_up", [
    ((), ""), (("Review the category definitions.",), ""),
    ((), "Compare the requested categories?"),
    (("Review the category definitions.",), "Compare the requested categories?"),
])
def test_pdf_renders_each_optional_section_only_when_nonempty(monkeypatch, recommendations, follow_up):
    from reportlab.platypus import Paragraph

    captured = capture_layout(monkeypatch)
    original = report()
    client = replace(build_document(original), recommendations=recommendations, follow_up=follow_up)
    with patch.object(report_export, "build_document", return_value=client), \
            patch.object(report_export, "_chart_png", side_effect=ImportError):
        report_export.report_pdf(original)
    text = [flow.getPlainText() for flow in captured["story"] if isinstance(flow, Paragraph)]
    assert ("Recommendations" in text) == bool(recommendations)
    assert ("Suggested Follow-up" in text) == bool(follow_up)
    assert all(value in text for value in recommendations)
    if follow_up:
        assert follow_up in text
    assert "Data Limitations" in text and any(value.startswith("Method:") for value in text)


def test_pdf_preserves_explicitly_requested_guidance(monkeypatch):
    from reportlab.platypus import Paragraph

    captured = capture_layout(monkeypatch)
    original = replace(report(), question="Department report with recommendations and next steps")
    with patch.object(report_export, "_chart_png", side_effect=ImportError):
        report_export.report_pdf(original)
    text = [flow.getPlainText() for flow in captured["story"] if isinstance(flow, Paragraph)]
    client = build_document(original)
    assert client.recommendations and client.follow_up
    assert "Recommendations" in text and "Suggested Follow-up" in text
    assert all(value in text for value in client.recommendations)
    assert client.follow_up in text