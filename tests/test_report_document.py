from copy import deepcopy
from dataclasses import FrozenInstanceError, fields, replace
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import patch

import pytest
import sqlglot

from chatbot.db import QueryResult, Schema
from chatbot.report_automation import automatic_queries, report_title
from chatbot.report_document import ReportDocument, ReportSection, build_document
from chatbot.reports import ReportEvidence, build_report
from chatbot.visualization import choose_visualization


def evidence(sql, columns, rows, *, id="q1", question="Requested aggregates", truncated=False):
    return ReportEvidence(id, question, sql, QueryResult(columns, rows, 0, truncated, .123),
                          datetime(2026, 10, 15, tzinfo=timezone.utc), "snapshot")


def document(*items, title="Workforce report"):
    return build_document(build_report(title, items, scope=("demo", None), source="snapshot"))


def count_groups(*, alias="employee_count", suffix="", rows=None, truncated=False):
    return evidence(f"SELECT department, COUNT(*) AS {alias} FROM employees GROUP BY department {suffix}",
                    ["department", alias], rows if rows is not None else [["Sales", 6], ["Support", 4]],
                    truncated=truncated)


def salary_groups(*, rows=None, suffix="", count="COUNT(*)", alias="employee_count"):
    return evidence("SELECT department, AVG(salary) AS average_salary, "
                    f"{count} AS {alias} FROM employees GROUP BY department {suffix}",
                    ["department", "average_salary", alias],
                    rows if rows is not None else [["Sales", Decimal("12345.67890123456789"), 6],
                                                  ["Support", Decimal("10000.12345678901234"), 4]])


def text(doc):
    return " ".join([doc.summary, *doc.findings, *doc.recommendations, *doc.limitations, doc.follow_up,
                     *(section.commentary for section in doc.sections)])


def test_public_frozen_api_detached_charts_strings_and_no_side_effects():
    item = salary_groups()
    report = build_report("Salary_report", (item,), scope=("demo", None), source="snapshot")
    before = deepcopy(report)
    with patch("socket.create_connection", side_effect=AssertionError("No network")):
        doc = build_document(report)
    assert isinstance(doc, ReportDocument)
    section = doc.sections[0]
    assert isinstance(section, ReportSection)
    assert [f.name for f in fields(ReportSection)] == [
        "title", "columns", "rows", "commentary", "chart_result", "chart_question", "chart_sql"]
    assert [f.name for f in fields(ReportDocument)] == [
        "title", "summary", "sections", "findings", "recommendations", "limitations", "follow_up"]
    for instance in (section, doc):
        with pytest.raises(FrozenInstanceError):
            instance.title = "Changed"
    assert all(isinstance(v, str) for row in section.rows for v in row)
    assert all(isinstance(v, tuple) for v in (doc.sections, doc.findings, doc.recommendations,
                                              doc.limitations, section.columns, section.rows))
    assert doc.title == "Salary report"
    assert section.chart_result is not item.result
    assert section.chart_result.rows[0][1] == "12345.67890123456789"
    section.chart_result.rows[0][1] = "0"
    section.chart_result.columns[0] = "changed"
    assert report == before
    assert item.result.rows[0][1] == Decimal("12345.67890123456789")


@pytest.mark.parametrize("alias", ["employee_count", "record_count", "headcount", "count", "n", "total_people"])
def test_ast_count_detection_complete_partition_percentages(alias):
    doc = document(count_groups(alias=alias))
    section = doc.sections[0]
    assert section.title == "Department Breakdown"
    assert section.columns[-1] == "Percentage of returned total"
    assert section.rows == (("Sales", "6", "60%"), ("Support", "4", "40%"))
    assert "Sales has the highest returned count (6)" in section.commentary
    assert "returned" in section.commentary and "query filters" not in section.commentary
    assert "query filters" in " ".join(doc.limitations)
    assert section.chart_result.columns == ["department", alias]
    assert section.chart_sql == count_groups(alias=alias).sql


def test_repeating_percentage_is_bounded_in_table_commentary_and_summary():
    doc = document(count_groups(rows=[["Support", 93], ["Other", 802]]))
    assert doc.sections[0].rows[0][-1] == "10.39%"
    assert doc.sections[0].rows[1][-1] == "89.61%"
    assert "Other: 89.61%" in doc.summary
    assert max(len(value) for row in doc.sections[0].rows for value in row) < 20


@pytest.mark.parametrize("suffix,truncated", [
    ("LIMIT 2", False), ("LIMIT 100000", False), ("LIMIT 2 OFFSET 1", False),
    ("HAVING COUNT(*) > 0", False), ("", True), ("WITH ROLLUP", False),
])
def test_no_percentages_for_selection_limits_having_rollup_or_truncation(suffix, truncated):
    doc = document(count_groups(suffix=suffix, truncated=truncated))
    assert "Percentage of returned total" not in doc.sections[0].columns
    assert "60%" not in text(doc)
    assert "returned groups" in doc.sections[0].commentary


@pytest.mark.parametrize("aggregate", ["AVG(salary)", "SUM(salary)", "COUNT(DISTINCT employee_id)",
                                       "COUNT(*) + 0", "MAX(salary)"])
def test_names_do_not_prove_count_or_percentage(aggregate):
    item = evidence(f"SELECT department, {aggregate} AS employee_count FROM employees GROUP BY department",
                    ["department", "employee_count"], [["Sales", 6], ["Support", 4]])
    doc = document(item)
    assert "Percentage of returned total" not in doc.sections[0].columns
    assert "highest returned count" not in doc.sections[0].commentary


@pytest.mark.parametrize("sql", [
    "SELECT e.department, COUNT(*) AS employee_count FROM employees e JOIN departments d ON e.department=d.department GROUP BY e.department",
    "SELECT department, COUNT(*) AS employee_count FROM employees GROUP BY department, city",
    "SELECT DISTINCT department, COUNT(*) AS employee_count FROM employees GROUP BY department",
    "SELECT department, COUNT(*) AS employee_count FROM employees GROUP BY department; SELECT 1",
    "SELECT department, SUM(COUNT(*)) OVER () AS employee_count FROM employees GROUP BY department",
    "SELECT department, COUNT(*) AS employee_count FROM employees GROUP BY department UNION SELECT department, COUNT(*) FROM employees GROUP BY department",
])
def test_complex_or_mismatched_lineage_never_proves_percentages(sql):
    doc = document(evidence(sql, ["department", "employee_count"], [["A", 6], ["B", 4]]))
    assert all("Percentage of returned total" not in s.columns for s in doc.sections)


@pytest.mark.parametrize("rows", [
    [["A", 0], ["B", 0]], [["A", -1], ["B", 2]], [["A", None], ["B", 2]],
    [["A", "1.5"], ["B", 2]], [["A", 1], ["A", 2]],
])
def test_counts_need_nonnegative_integral_values_positive_total_unique_groups(rows):
    doc = document(count_groups(rows=rows))
    assert "Percentage of returned total" not in doc.sections[0].columns


def test_missing_category_in_denominator_not_ranked_and_ties_are_human():
    doc = document(count_groups(rows=[[None, 80], ["Sales", 10], ["Support", 10]]))
    section = doc.sections[0]
    assert section.rows[0] == ("Missing", "80", "80%")
    assert "All named returned groups tie" in section.commentary
    assert "Missing has the highest" not in text(doc)
    assert "Missing categories are shown but excluded from named-group rankings." in doc.limitations
    assert "missing categories" not in section.commentary
    tied = document(count_groups(rows=[["A", 10], ["B", 10], ["C", 5]]))
    assert "A and B tie for the highest returned count (10)" in tied.sections[0].commentary


def test_where_defines_returned_population_never_uses_scalar_denominator():
    grouped = evidence("SELECT department, COUNT(*) AS headcount FROM employees WHERE status='Active' GROUP BY department",
                       ["department", "headcount"], [["A", 2], ["B", 3]])
    scalar = evidence("SELECT COUNT(*) AS employee_count FROM employees", ["employee_count"], [[1000]], id="q2")
    doc = document(grouped, scalar)
    assert doc.sections[0].rows == (("A", "2", "40%"), ("B", "3", "60%"))
    assert "query filters" in " ".join(doc.limitations)
    assert "query filters" not in doc.sections[0].commentary
    assert all("Percentage" not in c for c in doc.sections[1].columns)
    assert "0.2%" not in text(doc)


def test_count_column_excludes_nulls_not_invented_workforce_denominator():
    item = salary_groups(count="COUNT(salary)", alias="headcount")
    doc = document(item, title="Salary profile")
    assert doc.sections[0].columns[2] == "Salary observations"
    assert "returned salary observations total" in " ".join(doc.limitations)
    assert "overall average" not in doc.summary.lower()
    assert "weighted overall salary is inferred" in " ".join(doc.limitations)


def test_multiple_count_definitions_have_no_arbitrary_percentage_denominator():
    item = evidence("SELECT department, COUNT(*) AS headcount, COUNT(salary) AS salary_count FROM employees GROUP BY department",
                    ["department", "headcount", "salary_count"], [["A", 20, 10], ["B", 10, 10]])
    assert "Percentage of returned total" not in document(item).sections[0].columns


def test_salary_profile_decimal_spread_question_first_no_weighted_mean_or_fake_units():
    counts = replace(count_groups(), id="q2")
    item = salary_groups()
    doc = document(counts, item, title="Salary comparison report")
    section = doc.sections[1]
    assert section.title == "Department Salary Profile"
    assert section.columns[:3] == ("Department", "Average salary", "Employee count")
    assert section.rows[0][:3] == ("Sales", "12,345.68", "6")
    assert "Sales has the highest average salary (12,345.68)" in section.commentary
    assert "Support has the lowest (10,000.12)" in section.commentary
    assert "spread between group averages is 2,345.56" in section.commentary
    assert doc.summary.startswith("The available salary evidence shows:")
    assert "average salary" in doc.findings[0]
    assert doc.recommendations == () and doc.follow_up == ""
    assert "returned count" not in section.commentary
    assert not any(unit in text(doc) for unit in ("$", "USD", "annual salary", "monthly salary", "per year"))
    assert "turnover" not in doc.follow_up and "attrition" not in text(doc)
    assert "11,407" not in text(doc)  # Do not construct a weighted mean using COUNT(*).
    assert section.chart_sql == ""
    assert section.chart_result.columns == ["department", "average_salary"]
    assert len(section.chart_result.rows[0]) == 2
    assert choose_visualization(section.chart_result, section.chart_question, sql=section.chart_sql).kind == "bar"


def test_negative_null_decimal_salary_and_high_low_ties():
    rows = [["A", Decimal("-10.1234567890123456789"), 3],
            ["B", Decimal("-10.1234567890123456789"), 2],
            ["C", Decimal("-20.9876543210987654321"), 2],
            ["D", Decimal("-20.9876543210987654321"), 2],
            ["E", None, 1], [None, Decimal("999999"), 1],
            ["Unknown", Decimal("999999"), 1]]
    doc = document(salary_groups(rows=rows), title="Salary report")
    section = doc.sections[0]
    assert "A and B tie for the highest average salary (-10.12)" in section.commentary
    assert "C and D tie for the lowest (-20.99)" in section.commentary
    assert "spread between group averages is 10.86" in section.commentary
    assert section.rows[4][1] == "Missing"
    assert "999,999" not in section.commentary
    assert "missing average salary" in " ".join(doc.limitations)
    assert section.chart_result.rows[4][1] is None


def test_all_salary_ties_single_valid_and_all_missing_groups():
    equal = document(salary_groups(rows=[["A", 10, 1], ["B", 10, 3]]))
    assert "All named returned groups tie at average salary 10 (A and B). The spread is 0." in equal.sections[0].commentary
    single = document(salary_groups(rows=[["A", 10, 1], ["B", None, 3]]))
    assert "Among returned groups, A has average salary 10." in single.sections[0].commentary
    assert "no other named group" not in single.sections[0].commentary
    missing = document(salary_groups(rows=[["Unknown", 100, 1], [None, 200, 3]]))
    assert "No named groups with valid values" in missing.sections[0].commentary
    assert "highest" not in missing.summary


@pytest.mark.parametrize("invalid", ["NaN", "Infinity", True, "not a salary"])
def test_invalid_salary_evidence_fails_closed(invalid):
    doc = document(salary_groups(rows=[["A", invalid, 2], ["B", 20, 2]]), title="Salary report")
    assert doc.sections == ()
    assert "omitted" in text(doc)


def test_real_job_title_breakdown_uses_same_privacy_checks_and_no_mutation():
    item = evidence("SELECT job_title, COUNT(*) AS headcount FROM employees GROUP BY job_title",
                    ["job_title", "headcount"], [["Engineer", 8], ["Manager", 2]])
    before = deepcopy(item)
    doc = document(item, title="Job titles and roles report")
    assert doc.sections[0].title == "Job Title Breakdown"
    assert doc.sections[0].columns == ("Job title", "Headcount", "Percentage of returned total")
    assert doc.sections[0].rows[0] == ("Engineer", "8", "80%")
    assert not any("No job-title" in note or "Nonaggregate or privacy-sensitive" in note for note in doc.limitations)
    assert item == before
    private = replace(item, result=QueryResult(item.result.columns, [["private@example.com", 8]], 0, False, 0))
    assert document(private).sections == ()


def test_job_title_alias_of_department_is_not_real_roles():
    fake = evidence("SELECT department AS job_title, COUNT(*) AS headcount FROM employees GROUP BY department",
                    ["job_title", "headcount"], [["Sales", 8], ["Support", 2]])
    doc = document(fake, title="Job title report")
    assert doc.sections == ()
    assert "No job-title or role evidence" in text(doc)
    doc = document(count_groups(), title="Salary and job roles report")
    assert doc.sections[0].title == "Department Breakdown"
    assert "No job-title or role evidence" in text(doc)
    assert doc.summary.startswith("No aggregate-safe salary evidence")


@pytest.mark.parametrize("dimension", ["city", "status"])
def test_city_status_distribution(dimension):
    item = evidence(f"SELECT {dimension}, COUNT(*) AS record_count FROM employees GROUP BY {dimension}",
                    [dimension, "record_count"], [["A", 20], ["B", 10]])
    assert document(item).sections[0].title == "City/Status Distribution"


def test_hiring_dates_preserved_and_nonhiring_periods_not_invented():
    item = evidence("SELECT DATE_FORMAT(hire_date, '%Y-%m') AS month, COUNT(*) AS hires FROM employees GROUP BY month",
                    ["month", "hires"], [["2026-01", 2], ["2026-03", 3]])
    doc = document(item, title="Hiring report")
    assert doc.sections[0].title == "Hiring Trends"
    assert doc.sections[0].rows[0][0] == "2026-01"
    assert "attrition" not in text(doc)
    assert doc.follow_up == "" and doc.recommendations == ()
    requested = document(item, title="Hiring report with next steps")
    assert "coverage" in requested.follow_up
    assert any("comparable period coverage" in value for value in requested.recommendations)
    ordinary = replace(item, sql=item.sql.replace("hire_date", "order_date"))
    ordinary_doc = document(ordinary)
    assert ordinary_doc.sections[0].title == "Period Breakdown"
    assert "hiring" not in ordinary_doc.follow_up
    assert all("hiring" not in recommendation for recommendation in ordinary_doc.recommendations)


def test_workforce_overview_formats_scalars_and_missing_values():
    item = evidence("SELECT COUNT(*) AS headcount, AVG(salary) AS avg_pay, MIN(salary) AS minimum_salary FROM employees",
                    ["headcount", "avg_pay", "minimum_salary"], [[12345, Decimal("1234.56789"), None]])
    doc = document(item)
    section = doc.sections[0]
    assert section.title == "Workforce Overview"
    assert section.rows == (("12,345", "1,234.57", "Missing"),)
    assert "Headcount: 12,345." in doc.summary
    assert "Average salary: 1,234.57." in doc.summary
    assert "Percentage" not in " ".join(section.columns)
    assert len(doc.findings) <= 6


def test_partial_report_original_limitations_kept_findings_bounded_and_private_rows_hidden():
    items = [replace(count_groups(), id=f"q{i}") for i in range(5)]
    missing = replace(count_groups(), id="qmissing", result=None, error="Source unavailable")
    report = build_report("Workforce report", (*items, missing), scope=("demo", None), source="snapshot")
    report = replace(report, limitations=report.limitations + ("Specific data-quality caveat.",),
                     findings=("Do not trust this invented finding 999999.",),
                     recommendations=("Invented discrimination claim.",))
    doc = build_document(report)
    assert "Partial report: some requested evidence is unavailable." in doc.summary
    assert "Specific data-quality caveat." in doc.limitations
    assert len(doc.findings) <= 6
    assert "invented" not in text(doc).lower()
    private = evidence("SELECT name, salary FROM employees", ["name", "salary"], [["Private Person", 99999]])
    hidden = document(private)
    assert not hidden.sections and not hidden.findings
    assert "Private Person" not in text(hidden)
    assert hidden.follow_up == "" and hidden.recommendations == ()
    requested = document(private, title="Recommend next steps")
    assert "clarify" in requested.follow_up
    assert "aggregate evidence" in requested.recommendations[0]


def test_large_decimal_precision_retained_and_no_float_overflow():
    item = salary_groups(rows=[["A", Decimal("1E+100"), 1], ["B", Decimal("-1E+100"), 1]])
    doc = document(item, title="Salary report")
    section = doc.sections[0]
    assert "spread between group averages is 20" + ",000" * 33 in section.commentary
    assert section.chart_result.rows[0][1] == "1E+100"


def test_six_distinct_findings_maximum_without_changing_evidence_or_authoritative_text():
    items = [replace(salary_groups(rows=[[f"A{i}", i + 10, 1], [f"B{i}", i, 2]]), id=f"q{i}")
             for i in range(6)]
    report = build_report("Salary report", tuple(items), scope=("demo", None), source="snapshot")
    before = deepcopy(report)
    doc = build_document(report)
    assert len(doc.findings) == 6
    assert all("average salary" in finding for finding in doc.findings)
    assert report == before


def test_general_summary_leads_with_counts_even_when_salary_evidence_is_first():
    doc = document(salary_groups(), title="Workforce composition")
    assert "returned count" in doc.findings[0]
    assert doc.summary.index("returned count") < doc.summary.index("average salary")


def test_empty_salary_and_hyphenated_role_requests_explicitly_disclose_missing_evidence():
    item = replace(salary_groups(), result=None, error="Unavailable")
    doc = document(item, title="Salary and job-title report")
    assert doc.summary.startswith("No aggregate-safe salary evidence is available.")
    assert "Partial report" in doc.summary
    assert "No job-title or role evidence" in text(doc)


@pytest.mark.parametrize("role_column", [None, "job_title", "job_role", "role"])
def test_automatic_positive_salary_plan_builds_question_led_document(role_column):
    question = "Which departments and roles have the highest and lowest salaries?"
    columns = ({"name": "department", "type": "VARCHAR(80)"},
               {"name": "salary", "type": "DECIMAL(12,2)"})
    if role_column:
        columns += ({"name": role_column, "type": "VARCHAR(80)"},)
    schema = Schema("demo", "employees", columns, (), (), "8")
    plan = automatic_queries(schema, question)
    results = {
        ("employee_count",): [[10]],
        ("department", "employee_count"): [["Sales", 5], ["Engineering", 3], [None, 2]],
        ("department", "employee_count", "salary_count", "average_salary"): [
            ["Sales", 5, 2, Decimal("200")], ["Engineering", 3, 1, Decimal("300")], [None, 2, 0, None]],
        ("role", "employee_count"): [["Engineer", 6], ["Manager", 4]],
        ("role", "employee_count", "salary_count", "average_salary"): [
            ["Engineer", 6, 2, Decimal("200")], ["Manager", 4, 1, Decimal("300")]],
        ("employee_count", "salary_count", "average_positive_salary", "total_positive_salary",
         "missing_salary_count", "nonpositive_salary_count"): [
            [10, 3, Decimal("233.333333333333333333"), 700, 4, 3]],
    }
    items = []
    for query in plan:
        aliases = tuple(p.alias_or_name for p in sqlglot.parse_one(query.sql, read="mysql").expressions)
        items.append(replace(evidence(query.sql, list(aliases), results[aliases],
                                      id=query.id, question=query.question), source="live"))
    report = replace(build_report(report_title(question), tuple(items), scope=("demo", None), source="live"),
                     question=question)
    before = deepcopy(report)
    with patch("socket.create_connection", side_effect=AssertionError("No network")):
        doc = build_document(report)
    profile_query = next(q for q in plan if "`department`" in q.sql and "AS average_salary" in q.sql)
    assert "AVG(CASE WHEN `salary`>0 THEN `salary` END) AS average_salary" in profile_query.sql
    profile = next(s for s in doc.sections if s.title == "Department Salary Profile")
    assert profile.columns == ("Department", "Employee count", "Positive salary observations",
                               "Average salary", "Percentage of returned total")
    assert profile.rows == (("Sales", "5", "2", "200", "50%"),
                            ("Engineering", "3", "1", "300", "30%"),
                            ("Missing", "2", "0", "Missing", "20%"))
    assert "returned employee count total, including missing categories" in " ".join(doc.limitations)
    assert "excluded" not in profile.commentary and "Headcount includes" not in profile.commentary
    assert "Headcount includes all records" in " ".join(doc.limitations)
    assert doc.recommendations == () and doc.follow_up == ""
    assert "Engineering has the highest average salary (300)" in doc.findings[0]
    assert "Sales has the lowest (200)" in doc.findings[0]
    assert doc.summary.startswith("The available salary evidence shows:")
    assert "Salary averages exclude missing and nonpositive salaries" in text(doc)
    assert "missing average salary values are not zero" in text(doc)
    assert profile.chart_result.columns == ["department", "average_salary"]
    assert profile.chart_result.rows == [["Sales", "200"], ["Engineering", "300"], [None, None]]
    assert profile.chart_sql == ""
    assert profile.chart_question == "Compare average salary by department"
    chart = choose_visualization(profile.chart_result, profile.chart_question, sql=profile.chart_sql)
    assert chart.kind == "bar"
    assert "average salary" in chart.title.lower()
    assert doc.title == "Department Pay Report" and report.question == question
    missing_roles = "No job-title or role evidence is available; department labels are not job titles."
    if role_column:
        role = next(s for s in doc.sections if s.title == "Job Title Salary Profile")
        assert role.columns[0] == "Job title"
        assert role.chart_result.columns == ["role", "average_salary"]
        assert "Manager has the highest average salary (300)" in role.commentary
        assert "Engineer has the lowest (200)" in role.commentary
        assert missing_roles not in doc.limitations
    else:
        # The concise title omits 'roles'; the ORIGINAL question still requests them.
        assert missing_roles in doc.limitations
        assert not any("Job Title" in s.title for s in doc.sections)
    assert report == before


@pytest.mark.parametrize("guidance_question", [
    "Salary report with advice", "Advise on salary comparisons", "Recommend next steps for pay",
    "Salary recommendations", "Salary next-steps", "Salary follow-up", "Suggest a pay review",
    "Salary suggestions", "Salary action plan", "What should we review about salaries?",
])
@pytest.mark.parametrize("field", ["question", "title"])
def test_guidance_only_when_requested_in_original_question_or_title(guidance_question, field):
    original = build_report("Salary report", (salary_groups(),), scope=("demo", None), source="snapshot")
    original = replace(original, **{field: guidance_question})
    before = deepcopy(original)
    doc = build_document(original)
    assert "like-for-like" in doc.recommendations[0]
    assert "If pay differences" in doc.recommendations[0]
    assert "do not establish unfair pay or discrimination" in doc.recommendations[0]
    assert "confirming the salary definition" in doc.follow_up
    assert original == before


@pytest.mark.parametrize("question,title", [
    ("Which departments have the highest salaries?", "Salary report"),
    ("Full salary report", "Salary report"),
    ("Salary report without recommendations", "Salary report"),
    ("Salary report; do not give advice", "Salary recommendations"),
    ("Salary report, no next steps", "Salary report"),
    ("Salary report", "Salary report without recommendations"),
])
def test_plain_full_or_explicitly_no_advice_reports_have_no_optional_sections(question, title):
    item = replace(salary_groups(), question="Recommendations and next steps")
    original = replace(build_report(title, (item,), scope=("demo", None), source="snapshot"),
                       question=question, recommendations=("Unrequested advice",),
                       limitations=("Recommendations are not evidence.",))
    doc = build_document(original)
    assert doc.recommendations == () and doc.follow_up == ""
    assert doc.sections and doc.findings and doc.limitations
    assert "Unrequested advice" not in text(doc)


def test_repeated_salary_and_percentage_methodology_is_only_in_deduplicated_limits():
    item = evidence(
        "SELECT department, COUNT(*) AS headcount, "
        "AVG(CASE WHEN salary > 0 THEN salary END) AS average_salary "
        "FROM employees GROUP BY department",
        ["department", "headcount", "average_salary"], [["A", 5, 200], ["B", 3, 100], [None, 2, None]])
    report = build_report("Salary report", (item, replace(item, id="q2")),
                          scope=("demo", None), source="snapshot")
    before = deepcopy(report)
    doc = build_document(report)
    primary = " ".join([doc.summary, *doc.findings, *(s.commentary for s in doc.sections)])
    assert "A has the highest average salary (200)" in primary
    assert all(word not in primary for word in ("excluded", "Headcount includes", "Percentages use", "query filters"))
    assert sum("Salary averages exclude" in note for note in doc.limitations) == 1
    assert sum("Percentages use" in note for note in doc.limitations) == 1
    assert "missing average salary values are not zero" in " ".join(doc.limitations)
    assert doc.sections[0].rows[-1] == ("Missing", "2", "Missing", "20%")
    assert report == before