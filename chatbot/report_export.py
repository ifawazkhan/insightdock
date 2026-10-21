"""In-memory, aggregate-only PDF export. No SQL execution, remote assets or AI.

report_pdf(report) -> bytes. Requires ReportLab; static charts use vl-convert.
Missing/failed chart rendering falls back to a labelled aggregate table.
Output is limited to 10 MiB. SQL and raw person-level rows are never included.
The bundled ReportLab Vera font supports common Latin/Greek/Cyrillic Unicode;
other scripts may need a separately licensed font supplied by the application.
"""

from io import BytesIO
from copy import deepcopy
from datetime import timezone
import inspect
import json
from pathlib import Path
from threading import Lock
from xml.sax.saxutils import escape

from chatbot.report_document import build_document
from chatbot.report_presentation import clean_report_text
from chatbot.reports import BusinessReport, ReportError
from chatbot.visualization import choose_visualization

MAX_PDF_BYTES = 10 * 1024 * 1024
BRANDING = "Created and developed by Fawaz Khan — © 2026 Fawaz Khan. All rights reserved."
_FONT_LOCK = Lock()


class _BoundedBuffer(BytesIO):
    def write(self, data: bytes) -> int:
        if self.tell() + len(data) > MAX_PDF_BYTES:
            raise ReportError("PDF exceeds the 10 MiB export limit.")
        return super().write(data)


def _safe_spec(value: object) -> bool:
    """Only inline chart data; reject URLs, external images and expressions."""
    if isinstance(value, dict):
        return not any(str(key).lower() in {"url", "href", "expr", "signal", "image"} for key in value) and all(
            _safe_spec(child) for child in value.values())
    if isinstance(value, (tuple, list)):
        return all(_safe_spec(child) for child in value)
    return True


def _chart_png(spec: dict) -> bytes:
    if not _safe_spec(spec) or len(json.dumps(spec, default=str)) > 300000:
        raise ReportError("Chart specification is unsafe or oversized.")
    import vl_convert
    import reportlab

    # Browser fonts such as Segoe UI are not available to the static renderer
    # on every OS. Register the bundled PDF font so axes/legends are not blank.
    local_spec = deepcopy(spec)
    local_spec["width"] = 600
    font_directory = Path(reportlab.__file__).parent / "fonts"
    with _FONT_LOCK:
        vl_convert.register_font_directory(str(font_directory))
    config = local_spec.setdefault("config", {})
    config["font"] = "Bitstream Vera Sans"
    mark = local_spec.get("mark")
    if isinstance(mark, dict) and mark.get("type") == "bar" and "color" not in local_spec.get("encoding", {}):
        mark["color"] = "#4F46E5"
    for target in ("axis", "legend"):
        style = config.setdefault(target, {})
        style["labelColor"] = "#1E293B"
        style["titleColor"] = "#1E293B"
    config.setdefault("title", {})["color"] = "#1E293B"
    data = vl_convert.vegalite_to_png(local_spec, scale=1)
    if not isinstance(data, bytes) or len(data) > 2 * 1024 * 1024 or not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ReportError("Static chart image is invalid or oversized.")
    return data


def report_pdf(report: BusinessReport) -> bytes:
    """Render the shared client document in memory, without refreshing evidence."""
    try:
        import reportlab
        from reportlab.lib import colors
        from reportlab.lib.enums import TA_CENTER, TA_LEFT
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.utils import ImageReader
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        from reportlab.platypus import Image, LongTable, Paragraph, SimpleDocTemplate, TableStyle
    except ImportError:
        raise ReportError("PDF export requires the reportlab dependency.") from None
    if len(report.evidence) > 6 or len(report.findings) > 120 or any(
            len(section) > 200 for section in (report.recommendations, report.limitations)):
        raise ReportError("Report exceeds the bounded PDF section limits.")
    # Keep the input ceiling even when the shared document replaces narration.
    if any(len(str(value)) > 16000 for value in
           (report.title, report.summary, *report.findings, *report.recommendations, *report.limitations)):
        raise ReportError("Report text exceeds the bounded PDF text limit.")
    client = build_document(report)
    if len(client.sections) > 6 or len(client.findings) > 120 or any(
            len(values) > 200 for values in (client.recommendations, client.limitations)):
        raise ReportError("Report exceeds the bounded PDF section limits.")
    font = "Helvetica"
    bold_font = "Helvetica-Bold"
    with _FONT_LOCK:
        path = Path(reportlab.__file__).parent / "fonts" / "Vera.ttf"
        if path.is_file():
            if "ReportVera" not in pdfmetrics.getRegisteredFontNames():
                pdfmetrics.registerFont(TTFont("ReportVera", str(path)))
            font = "ReportVera"
            bold_path = path.with_name("VeraBd.ttf")
            if bold_path.is_file():
                if "ReportVeraBold" not in pdfmetrics.getRegisteredFontNames():
                    pdfmetrics.registerFont(TTFont("ReportVeraBold", str(bold_path)))
                bold_font = "ReportVeraBold"
            else:
                bold_font = font
    indigo = colors.HexColor("#4F46E5")
    ink = colors.HexColor("#1E293B")
    muted = colors.HexColor("#64748B")
    page_width, _ = A4
    margin = 42
    content_width = page_width - 2 * margin
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name="ReportBody", fontName=font, fontSize=8.5, leading=11.5,
                              textColor=ink, alignment=TA_LEFT, spaceAfter=4, splitLongWords=True))
    styles.add(ParagraphStyle(name="ReportHeading", fontName=bold_font, fontSize=11, leading=14,
                              textColor=indigo, spaceBefore=9, spaceAfter=5, keepWithNext=True))
    styles.add(ParagraphStyle(name="ReportTitle", fontName=bold_font, fontSize=19, leading=24,
                              textColor=ink, alignment=TA_CENTER, spaceAfter=7, keepWithNext=True))
    styles.add(ParagraphStyle(name="ReportMeta", parent=styles["ReportBody"], fontSize=7.5,
                              leading=10, textColor=muted, spaceAfter=3))
    styles.add(ParagraphStyle(name="ReportCell", parent=styles["ReportBody"], fontSize=7.2,
                              leading=9.5, spaceAfter=0))
    styles.add(ParagraphStyle(name="ReportHeader", parent=styles["ReportCell"], fontName=bold_font,
                              textColor=colors.white))
    styles.add(ParagraphStyle(name="ReportCaption", parent=styles["ReportMeta"], spaceAfter=4))

    def paragraph(value: object, style: str = "ReportBody"):
        text = str(value)
        if len(text) > 16000:
            raise ReportError("Report text exceeds the bounded PDF text limit.")
        # ReportLab paragraphs parse a subset of XML, never trust supplied markup.
        text = "".join(c for c in text if ord(c) >= 32 or c in "\n\t")
        return Paragraph(escape(text).replace("\n", "<br/>"), styles[style])

    def narrative(value: str, style: str = "ReportBody"):
        return paragraph(clean_report_text(value, report), style)

    title = clean_report_text(client.title, report)
    question = getattr(report, "question", "")
    purpose = ("Question: " + question) if question else ("Purpose: " + title)
    generated = report.created_at.astimezone(timezone.utc).strftime("%d %B %Y, %H:%M UTC")
    story = [paragraph(title, "ReportTitle"),
             paragraph(f"Generated: {generated}", "ReportMeta"),
             narrative(purpose, "ReportMeta"),
             paragraph(f"Source: {report.source} | Scope: {report.scope[0]}" +
                       (f".{report.scope[1]}" if report.scope[1] else " (schema)"), "ReportMeta"),
             paragraph("Executive Summary", "ReportHeading"), narrative(client.summary)]

    for section in client.sections:
        story.append(narrative(section.title, "ReportHeading"))
        columns, rows = section.columns[:12], section.rows[:50]
        if columns and rows:
            data = [[paragraph(c, "ReportHeader") for c in columns]] + [
                [paragraph(value, "ReportCell") for value in row[:12]] for row in rows]
            # Allocate a little more width to descriptive labels, without allowing
            # one long label to squeeze all the other columns out of an A4 frame.
            weights = [min(30, max(12, len(c), *(len(row[i]) for row in rows)))
                       for i, c in enumerate(columns)]
            widths = [content_width * weight / sum(weights) for weight in weights]
            table = LongTable(data, colWidths=widths, repeatRows=1, hAlign="LEFT",
                              splitByRow=1, spaceAfter=4)
            table.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), indigo),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#EEF2FF")]),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LINEBELOW", (0, 0), (-1, 0), .5, indigo),
                ("LEFTPADDING", (0, 0), (-1, -1), 5),
                ("RIGHTPADDING", (0, 0), (-1, -1), 5),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]))
            story.append(table)
            if len(section.rows) > 50 or len(section.columns) > 12:
                omitted_rows = max(0, len(section.rows) - 50)
                omitted_columns = max(0, len(section.columns) - 12)
                story.append(paragraph(
                    f"PDF display: first {len(rows)} of {len(section.rows)} aggregate rows and "
                    f"{len(columns)} of {len(section.columns)} columns; "
                    f"{omitted_rows} rows and {omitted_columns} columns omitted. "
                    "Commentary uses the available aggregate evidence, not just displayed rows; "
                    "no totals are extrapolated.", "ReportCaption"))
        else:
            story.append(paragraph("No aggregate rows available; missing values are not zero."))
        story.append(narrative(section.commentary))

    # Charts follow all breakdowns, never interrupting a heading/table/commentary.
    chart_heading_added = False
    charts = [section for section in client.sections
              if section.chart_result is not None and len(section.chart_result.rows) > 1]
    charts.sort(key=lambda section: "Salary Profile" not in section.title)
    for section in charts[:3]:
        try:
            parameters = inspect.signature(choose_visualization).parameters
            accepts_sql = "sql" in parameters or any(p.kind == inspect.Parameter.VAR_KEYWORD for p in parameters.values())
            visualization = (choose_visualization(section.chart_result, section.chart_question, sql=section.chart_sql)
                             if accepts_sql else choose_visualization(section.chart_result, section.chart_question))
            if visualization.spec:
                png = _chart_png(visualization.spec)
                width, height = ImageReader(BytesIO(png)).getSize()
                if width <= 0 or height <= 0 or width * height > 16000000:
                    raise ReportError("Chart dimensions exceed the export limit.")
                ratio = min(content_width / width, 320 / height, 1)
                if not chart_heading_added:
                    story.append(paragraph("Charts", "ReportHeading"))
                    chart_heading_added = True
                story.append(narrative(section.title, "ReportCaption"))
                story[-1].keepWithNext = True
                story.append(Image(BytesIO(png), width=width * ratio, height=height * ratio))
                if visualization.note:
                    story.append(narrative(visualization.note, "ReportCaption"))
        except Exception:
            # Renderer failure must not erase the report or leak library details.
            story.append(narrative(
                f"{section.title} — Chart unavailable: static renderer missing, failed or rejected. "
                "The verified aggregate table above remains available.", "ReportCaption"))

    for heading, values in (("Key Findings", client.findings), ("Recommendations", client.recommendations),
                            ("Data Limitations", client.limitations)):
        if heading == "Recommendations" and not values:
            continue
        story.append(paragraph(heading, "ReportHeading"))
        story.extend(narrative(value) for value in values)
        if not values:
            story.append(paragraph("No aggregate-safe findings are available; missing values are not zero."
                                   if heading == "Key Findings" else "No additional items supplied."))
    if client.follow_up:
        story.extend([paragraph("Suggested Follow-up", "ReportHeading"), narrative(client.follow_up)])
    story.append(paragraph(
        "Method: supplied aggregate evidence only; no database refresh during export. "
        "Displayed numbers use commas and at most two decimals. "
        "Exact values remain in the internal evidence; charts may use display precision. "
        "Original query filters, limits and null handling still apply. Raw individual rows and SQL are not exported. "
        "Separate live reads are not a consistent transactional snapshot.", "ReportCaption"))
    output = _BoundedBuffer()

    def footer(canvas, document):
        canvas.saveState()
        canvas.setFont(font, 6)
        canvas.setFillColor(muted)
        canvas.setStrokeColor(colors.HexColor("#C7D2FE"))
        canvas.line(margin, 34, page_width - margin, 34)
        canvas.drawString(margin, 23, BRANDING)
        canvas.drawRightString(page_width - margin, 23, f"Page {document.page}")
        canvas.restoreState()

    document = SimpleDocTemplate(output, pagesize=A4, rightMargin=margin, leftMargin=margin,
                                 topMargin=36, bottomMargin=45, title=title[:200],
                                 author="Fawaz Khan", pageCompression=1)
    document.build(story, onFirstPage=footer, onLaterPages=footer)
    data = output.getvalue()
    if len(data) > MAX_PDF_BYTES:
        raise ReportError("PDF exceeds the 10 MiB export limit.")
    return data