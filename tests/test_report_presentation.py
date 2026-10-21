from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import patch

import pytest

from chatbot import report_export
from chatbot.db import QueryResult
from chatbot.report_presentation import (
    clean_report_text, display_number, evidence_heading, executive_summary, human_label, key_findings,
)
from chatbot.reports import ReportEvidence, build_report


def business_report():
    now = datetime.now(timezone.utc)
    grouped = ReportEvidence(
        "a7b867e6-9336-4cb2-9552-a8a908758684", "Average_salary by city <overview> & context",
        "SELECT city, AVG(salary) AS average_salary FROM employees GROUP BY city",
        QueryResult(["city", "average_salary"],
                    [[f"City {index}", Decimal("12345.6789") + index] for index in range(20)],
                    0, True, .1), now, "snapshot",
    )
    scalar = ReportEvidence(
        "b8c867e6-9336-4cb2-9552-a8a908758685", "Workforce_summary",
        "SELECT COUNT(*) AS record_count, AVG(salary) AS salary_average, "
        "MIN(salary) AS salary_minimum FROM employees",
        QueryResult(["record_count", "salary_average", "salary_minimum"],
                    [[12345, Decimal("98765.43219876"), None]], 0, False, .1), now, "snapshot",
    )
    return build_report("Workforce_report", (grouped, scalar), scope=("demo", None), source="snapshot")


@pytest.mark.parametrize(("value", "expected"), [
    (12345, "12,345"), ("12345.6789", "12,345.68"),
    (Decimal("98765.4300"), "98,765.43"), ("1000.10", "1,000.1"),
    ("1,234.500", "1,234.5"), ("1.234567e6", "1,234,567"),
    ("-1234.5678", "-1,234.57"), ("-0.001", "0"), (0, "0"),
    (".5678", "0.57"), ("2026", "2,026"),
    (None, "Missing"), (True, "True"), ("Sales <team> & HR", "Sales <team> & HR"),
    ("NaN", "NaN"), ("Infinity", "Infinity"), ("2026-10", "2026-10"),
    (Decimal("1E+100"), "10" + ",000" * 33),
])
def test_display_number(value, expected):
    assert display_number(value) == expected


def test_clean_text_exact_references_numbers_labels_and_dates():
    report = business_report()
    first, second = (item.id for item in report.evidence)
    text = (f"[{first}] average_salary: 12345.6789; [{second}] record_count: 12345. "
            "Period 2026-10-15 to 2026-11; EMP12345; [unknown_id]; [12345].")
    cleaned = clean_report_text(text, report)
    assert first not in cleaned and second not in cleaned
    assert "average salary: 12,345.68" in cleaned and "record count: 12,345." in cleaned
    assert "2026-10-15 to 2026-11" in cleaned and "EMP12345" in cleaned
    assert "[unknown id]" in cleaned and "[12345]" in cleaned
    friendly = clean_report_text(text, report, keep_references=True)
    assert friendly.startswith("[1] average salary") and "[2] record count" in friendly
    # Only exact bracketed evidence IDs are citations, not substring matches.
    assert clean_report_text(f"[prefix{first}]", report) == f"[prefix{first}]"
    assert clean_report_text("salary_total: 2026; changed by .5678 from 2024 to 2026.", report) == (
        "salary total: 2,026; changed by 0.57 from 2024 to 2026.")
    assert human_label("salary_average") == "Salary average"


def test_business_summary_prioritizes_comparisons_then_scalar_metrics_without_mutation():
    report = business_report()
    original = repr(report)
    summary = executive_summary(report)
    assert "Evidence report" not in summary and "Findings below" not in summary
    assert "highest average salary" in summary and "12,364.68" in summary
    findings = key_findings(report)
    assert len(findings) <= 6
    assert "highest" in findings[0]
    assert findings[1:4] == ("record count: 12,345.", "salary average: 98,765.43.", "salary minimum: missing.")
    assert all(item.id not in summary + " ".join(findings) for item in report.evidence)
    assert repr(report) == original
    assert report.evidence[1].result.rows[0][1] == Decimal("98765.43219876")
    assert "98765.43219876" in report.findings[-2]


def test_scalar_only_summary_and_duplicate_findings():
    original = business_report()
    report = build_report("Overview", (original.evidence[1],), scope=original.scope, source="snapshot")
    assert executive_summary(report).startswith("This report summarizes the available aggregate results.")
    assert "98,765.43" in executive_summary(report)
    duplicate = replace(report, findings=report.findings * 3)
    assert len(key_findings(duplicate)) == 3


def test_abbreviated_business_labels_do_not_split_observations():
    report = business_report()
    report = replace(report, summary=f"[{report.evidence[0].id}] Among returned groups, Support has the highest count (93); H.R. has the lowest (17).")
    assert key_findings(report)[0] == "Among returned groups, Support has the highest count (93); H.R. has the lowest (17)."


def test_missing_partial_private_and_empty_results_remain_honest():
    report = business_report()
    missing = replace(report.evidence[0], result=None, error="Source unavailable")
    partial = build_report("Partial", (missing, report.evidence[1]), scope=report.scope, source="snapshot")
    assert "some requested evidence is unavailable" in executive_summary(partial)
    assert "missing" in " ".join(key_findings(partial))
    assert evidence_heading(partial, 1) == "Section 1"
    empty = replace(missing, error="", result=QueryResult(["city", "average_salary"], [], 0, False, .1))
    unavailable = build_report("Empty", (empty,), scope=report.scope, source="snapshot")
    assert key_findings(unavailable) == ()
    assert "missing values are not zero" in executive_summary(unavailable)
    assert "No rows returned" in " ".join(unavailable.limitations)
    private = replace(missing, question="Private Person", error="",
                      sql="SELECT name, salary FROM employees",
                      result=QueryResult(["name", "salary"], [["Private Person", 98765]], 0, False, .1))
    hidden = build_report("Private", (private,), scope=report.scope, source="snapshot")
    assert evidence_heading(hidden, 1) == "Section 1"
    assert "Private Person" not in executive_summary(hidden)


def test_pdf_uses_shared_presentation_escapes_and_keeps_limits(monkeypatch):
    pytest.importorskip("reportlab")
    from reportlab.platypus import Paragraph

    report = business_report()
    before = repr(report)
    captured = []
    original = Paragraph.__init__

    def capture(self, text, *args, **kwargs):
        captured.append(text)
        original(self, text, *args, **kwargs)

    monkeypatch.setattr(Paragraph, "__init__", capture)
    with patch.object(report_export, "_chart_png", side_effect=ImportError), \
            patch("socket.create_connection", side_effect=AssertionError("No network")):
        data = report_export.report_pdf(report)
    assert data.startswith(b"%PDF-")
    text = " ".join(captured)
    assert report.id not in text and all(item.id not in text for item in report.evidence)
    assert "Evidence report" not in text and "Findings below" not in text
    assert "City/Status Distribution" in text
    assert "Average salary" in text and "98,765.43" in text and "12,345" in text
    assert "98765.43219876" not in text and "Missing" in text
    assert "truncated" in text and "Currency, salary pay period" in text
    assert "Exact values remain in the internal evidence" in text
    start = captured.index("Key Findings") + 1
    end = captured.index("Data Limitations")
    assert len(captured[start:end]) <= 6
    assert "Recommendations" not in captured and "Suggested Follow-up" not in captured
    assert repr(report) == before