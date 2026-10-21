from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import Mock, patch

import pytest

from chatbot.config import Settings
from chatbot.db import DatabaseError, QueryResult, Schema
from chatbot.report_service import ReportService
from chatbot.reports import (ReportError, ReportEvidence, ReportQuery, aggregate_result, build_report,
                             curated_report_queries, is_report_request, suggest_report_topics)
from chatbot.sql_policy import PolicyError


@pytest.fixture
def schema():
    return Schema("demo", "employees", (), (), (), "8", tables=(
        {"name": "employees", "columns": ({"name": "salary", "type": "DECIMAL(12,2)"},
                                            {"name": "department", "type": "VARCHAR(80)"},
                                            {"name": "hire_date", "type": "DATE"})},
        {"name": "orders", "columns": ({"name": "total_amount", "type": "DECIMAL(12,2)"},)},
    ))


@pytest.fixture
def service(schema):
    settings = Settings("localhost", 3306, "user", "secret", "demo", table="employees",
                        schema_scope=True, allow_mutations=True, max_rows=3)
    database = Mock()
    database.discover_schema.return_value = schema
    database.execute.return_value = QueryResult(["record_count"], [[42]], 0, False, .1)
    return ReportService(settings, database)


def queries():
    return [ReportQuery("q1", "Count records", "SELECT COUNT(*) AS record_count FROM employees")]


def message(service, **changes):
    return {"read_only": True, "query_id": "q1", "question": "Count records",
            "sql": queries()[0].sql, "result": QueryResult(["record_count"], [[42]], 0, False, .1),
            "scope": service.scope, "executed_at": "2026-10-15T10:00:00Z", **changes}


def test_snapshot_zero_calls_and_copies_result(service):
    m = message(service)
    report = service.snapshot("Saved evidence", [m, {"read_only": False, "sql": "DELETE FROM employees"}])
    assert report.scope == ("demo", None)
    assert report.source == "snapshot"
    assert "42" in " ".join(report.findings)
    assert report.evidence[0].executed_at.tzinfo == timezone.utc
    m["result"].rows[0][0] = 99
    assert report.evidence[0].result.rows == [[42]]
    assert not service.database.mock_calls
    assert not hasattr(service, "pending")


@pytest.mark.parametrize("field,value", [("scope", ("other", None)), ("scope", ("demo", "orders")),
                                         ("executed_at", "2026-01-01"), ("query_id", "bad id"),
                                         ("query_id", None), ("question", None)])
def test_snapshot_requires_provenance(service, field, value):
    with pytest.raises(ReportError):
        service.snapshot("Report", [message(service, **{field: value})])
    assert not service.database.mock_calls


def test_snapshot_scope_label_missing_result_and_bounds(service):
    report = service.snapshot("Report", [message(service, scope="demo", result=None)])
    assert "Partial" in report.summary
    assert any("unavailable" in note for note in report.limitations)
    assert not service.database.mock_calls
    large = QueryResult(["record_count"], [[1], [2], [3], [4]], 0, False, 1)
    report = service.snapshot("Report", [message(service, result=large)])
    assert len(report.evidence[0].result.rows) == 3
    assert report.evidence[0].result.truncated


@pytest.mark.parametrize("sql", ["DELETE FROM employees", "UPDATE employees SET salary=1",
                                 "CREATE TABLE employees(x INT)", "DROP TABLE employees",
                                 "SELECT SLEEP(1) FROM employees", "SELECT * FROM other.employees",
                                 "SELECT * FROM employees; SELECT * FROM employees",
                                 "SELECT * INTO OUTFILE '/tmp/a' FROM employees"])
def test_snapshot_readonly_policy(service, sql):
    with pytest.raises((PolicyError, ReportError)):
        service.snapshot("Report", [message(service, sql=sql)])
    assert not service.database.mock_calls


@pytest.mark.parametrize("sql", ["DELETE FROM employees", "SELECT * FROM outside",
                                 "SELECT * FROM other.employees", "SELECT SLEEP(1) FROM employees",
                                 "SELECT * FROM employees; SELECT * FROM orders"])
def test_entire_plan_validated_before_first_execute(service, sql):
    plan = queries() + [ReportQuery("q2", "Invalid", sql)]
    with pytest.raises((PolicyError, ReportError)):
        service.live("Report", plan, "bad-run")
    service.database.execute.assert_not_called()
    with pytest.raises(ReportError, match="consumed"):
        service.live("Report", queries(), "bad-run")


@pytest.mark.parametrize("plan", [[], queries() * 2,
                                 [ReportQuery(f"q{i}", "Count", queries()[0].sql) for i in range(7)],
                                 [ReportQuery("q1", "Count", "")]])
def test_invalid_plan_no_execution(service, plan):
    with pytest.raises(ReportError):
        service.live("Report", plan, "run")
    service.database.execute.assert_not_called()


def test_live_one_use_fingerprints_and_fresh_discovery(service):
    report = service.live("Report", queries(), "run")
    assert report.source == "live"
    assert len(service._runs["run"]) == 1
    assert len(service._runs["run"][0]) == 64
    assert service.database.discover_schema.call_count == 3
    query, schema = service.database.execute.call_args.args
    assert query.read_only and query.sql == report.evidence[0].sql
    assert schema.database == "demo"
    with pytest.raises(ReportError, match="consumed"):
        service.live("Report", queries(), "run")
    assert service.database.execute.call_count == 1


def test_table_scope_rejects_other_table(service):
    service.scope_table = "employees"
    assert service.scoped_schema(service.database.discover_schema()).table_names == ("employees",)
    with pytest.raises(PolicyError):
        service.live("Report", queries() + [ReportQuery("q2", "Orders", "SELECT COUNT(*) FROM orders")], "run")
    service.database.execute.assert_not_called()
    with pytest.raises(PolicyError):
        service.snapshot("Report", [message(service, sql="SELECT COUNT(*) FROM orders")])


def test_fingerprint_tampering_no_execute(service):
    original = service._validate
    calls = 0

    def validate(query, tables):
        nonlocal calls
        calls += 1
        checked = original(query, tables)
        return replace(checked, sql=checked.sql + " LIMIT 1") if calls == 2 else checked

    service._validate = validate
    with pytest.raises(ReportError, match="fingerprint"):
        service.live("Report", queries(), "run")
    service.database.execute.assert_not_called()


def test_runtime_partial_failure_sanitized_and_not_retried(service):
    service.database.execute.side_effect = [DatabaseError("password=secret private@example.com"),
                                            QueryResult(["record_count"], [[5]], 0, False, 1)]
    plan = queries() + [ReportQuery("q2", "Count orders", "SELECT COUNT(*) AS record_count FROM orders")]
    report = service.live("Report", plan, "run")
    assert "Partial" in report.summary
    assert report.evidence[0].result is None and report.evidence[0].error
    assert "[q2]" in report.findings[0]
    assert "secret" not in str(report) and "private@example.com" not in str(report)
    assert service.database.execute.call_count == 2


def test_failed_discovery_consumes_id(service):
    service.database.discover_schema.side_effect = DatabaseError("Cannot inspect")
    with pytest.raises(DatabaseError):
        service.live("Report", queries(), "run")
    with pytest.raises(ReportError, match="consumed"):
        service.live("Report", queries(), "run")
    service.database.execute.assert_not_called()


@pytest.mark.parametrize("point", ["before", "after"])
def test_schema_drift_aborts(service, schema, point):
    changed = replace(schema, tables=schema.tables + ({"name": "new", "columns": ()},))
    service.database.discover_schema.side_effect = [schema, changed] if point == "before" else [schema, schema, changed]
    with pytest.raises(ReportError, match="drift"):
        service.live("Report", queries(), "run")
    assert service.database.execute.call_count == (0 if point == "before" else 1)


def test_scope_change_aborts(service):
    def read(*args):
        service.scope_table = "orders"
        return QueryResult(["record_count"], [[1]], 0, False, 1)
    service.database.execute.side_effect = read
    with pytest.raises(ReportError, match="scope changed"):
        service.live("Report", queries(), "run")


def test_budget_prevents_execution_after_discovery(service):
    clock = iter([0, 61, 61])
    with patch("chatbot.report_service.monotonic", side_effect=lambda: next(clock, 61)):
        report = service.live("Report", queries(), "run")
    assert "budget" in report.evidence[0].error
    service.database.execute.assert_not_called()


def test_budget_after_inflight_call_no_second_read(service):
    clock = [0]
    def read(*args):
        clock[0] = 61
        return QueryResult(["record_count"], [[3]], 0, False, 61)
    service.database.execute.side_effect = read
    with patch("chatbot.report_service.monotonic", side_effect=lambda: clock[0]):
        report = service.live("Report", queries() + [ReportQuery("q2", "Again", queries()[0].sql)], "run")
    assert service.database.execute.call_count == 1
    assert report.evidence[0].result is not None
    assert "budget" in report.evidence[1].error
    assert any("In-flight" in note for note in report.limitations)


def test_id_capacity_does_not_evict(service):
    service._runs = {f"r{i}": () for i in range(64)}
    with pytest.raises(ReportError, match="capacity"):
        service.live("Report", queries(), "new")
    assert len(service._runs) == 64
    with pytest.raises(ReportError, match="consumed"):
        service.live("Report", queries(), "r0")
    assert not service.database.mock_calls


def evidence(sql, columns, rows, truncated=False):
    return ReportEvidence("q1", "Aggregate", sql, QueryResult(columns, rows, 0, truncated, .1),
                          datetime.now(timezone.utc), "snapshot")


def test_decimal_precision_filters_nulls_and_unknown_units():
    item = evidence("SELECT SUM(salary) AS salary_sum, AVG(salary) AS salary_average FROM employees WHERE salary>0 LIMIT 1",
                    ["salary_sum", "salary_average"], [[Decimal("123456789012345.6700"), None]], True)
    report = build_report("Salary", (item,), scope=("demo", None), source="snapshot")
    assert "123456789012345.6700" in report.findings[0]
    assert "missing" in report.findings[1]
    notes = " ".join(report.limitations)
    assert "Currency" in notes and "pay period" in notes and "truncated" in notes and "filters" in notes
    assert "$" not in report.summary


@pytest.mark.parametrize("sql,columns,rows", [
    ("SELECT name, salary FROM employees", ["name", "salary"], [["Private Person", 99]]),
    ("SELECT MIN(email) AS record_count FROM employees", ["record_count"], [["private@example.com"]]),
    ("SELECT name AS department, COUNT(*) AS record_count FROM employees GROUP BY name", ["department", "record_count"], [["Private", 1]]),
    ("SELECT email AS department, COUNT(*) AS record_count FROM employees GROUP BY email", ["department", "record_count"], [["private@example.com", 1]]),
    ("SELECT SUM(salary) + salary AS salary_sum FROM employees", ["salary_sum"], [[99]]),
    ("SELECT SUM(salary) AS salary_sum FROM employees", ["salary_sum"], [["NaN"]]),
    ("SELECT SUM(salary) AS salary_sum FROM employees", ["salary_sum"], [["Infinity"]]),
    ("WITH x AS (SELECT name AS department FROM employees) SELECT department, COUNT(*) AS record_count FROM x GROUP BY department",
     ["department", "record_count"], [["Private Person", 1]]),
])
def test_aggregate_privacy_fail_closed(sql, columns, rows):
    item = evidence(sql, columns, rows)
    assert aggregate_result(item) is None
    report = build_report("Report", (item,), scope=("demo", None), source="snapshot")
    assert not report.findings
    assert "Private" not in report.summary


def test_count_identifier_safe_and_grouped_null_preserved():
    item = evidence("SELECT department, COUNT(emp_id) AS record_count FROM employees GROUP BY department",
                    ["department", "record_count"], [[None, 2], ["Sales", 4]])
    result = aggregate_result(item)
    assert result is not None and result.rows == [[None, "2"], ["Sales", "4"]]


@pytest.mark.parametrize("dimension", ["department_name", "category_name"])
def test_recognized_business_names_are_not_mistaken_for_person_names(dimension):
    item = evidence(f"SELECT {dimension}, COUNT(*) AS records FROM employees GROUP BY {dimension}",
                    [dimension, "records"], [["Sales", 4], ["Support", 2]])
    assert aggregate_result(item) is not None
    disguised = replace(item, sql=f"SELECT name AS {dimension}, COUNT(*) AS records FROM employees GROUP BY name")
    assert aggregate_result(disguised) is None


def test_report_summary_compares_only_returned_groups_with_exact_values():
    item = evidence("SELECT department, SUM(salary) AS salary_sum FROM employees GROUP BY department LIMIT 2",
                    ["department", "salary_sum"], [["Sales", "9007199254740993.01"], ["Support", "9007199254740993.02"]], True)
    report = build_report("Salary comparison", (item,), scope=("demo", None), source="snapshot")
    assert "Among returned groups" in report.summary
    assert "Support has the highest salary sum (9007199254740993.02)" in report.summary
    assert "Sales has the lowest (9007199254740993.01)" in report.summary
    assert any("check group sizes" in action for action in report.recommendations)
    assert "full employer cost" in " ".join(report.limitations)


def test_report_summary_endpoint_change_uses_observed_periods_only():
    item = evidence("SELECT DATE_FORMAT(hire_date, '%Y-%m') AS month, COUNT(*) AS hires FROM employees GROUP BY month",
                    ["month", "hires"], [["2026-03", 8], ["2026-01", 11], ["2026-02", None]])
    report = build_report("Hiring", (item,), scope=("demo", None), source="snapshot")
    assert "changed by -3 from 2026-01 to 2026-03" in report.summary
    assert "observed endpoints only" in report.summary
    assert "missing" in " ".join(report.findings)


def test_report_summary_does_not_invent_comparison_for_equal_or_missing_values():
    item = evidence("SELECT department, COUNT(*) AS records FROM employees GROUP BY department",
                    ["department", "records"], [["Sales", 4], ["Support", 4], [None, None]])
    report = build_report("Counts", (item,), scope=("demo", None), source="snapshot")
    assert "highest" not in report.summary and "changed by" not in report.summary
    assert len(report.recommendations) == 2


def test_curated_templates_and_suggestions(schema, service):
    topics = suggest_report_topics(schema, ["salary summary"])
    assert topics[0] == "salary summary"
    assert "monthly hiring" in topics and "order amount summary" in topics
    for topic in topics:
        plan = curated_report_queries(schema, topic)
        assert len(plan) <= 6
        for query in plan:
            assert service._validate(query, schema.table_names).read_only
    assert not service.database.mock_calls
    assert "revenue" not in curated_report_queries(schema, "order amount summary")[0].sql
    with pytest.raises(ReportError, match="Clarify"):
        curated_report_queries(schema, "profit next quarter")


def test_curated_dirty_types_no_cleaning(schema):
    dirty = replace(schema, tables=({"name": "employees", "columns": (
        {"name": "salary", "type": "TEXT"}, {"name": "hire_date", "type": "VARCHAR(80)"})},))
    assert suggest_report_topics(dirty, []) == ["counts"]
    for topic in ("salary summary", "monthly hiring", "order amount summary"):
        with pytest.raises(ReportError):
            curated_report_queries(dirty, topic)
    generic = replace(schema, tables=({"name": "orders", "columns": ({"name": "amount", "type": "DECIMAL"},)},))
    with pytest.raises(ReportError):
        curated_report_queries(generic, "order amount summary")


@pytest.mark.parametrize("text,expected", [("Create a workforce report", True), ("suggest reports", True),
                                         ("report based on live data", True), ("count employees", False),
                                         ("CREATE TABLE report(x INT)", False),
                                         ("SELECT 'create a report' FROM employees", False)])
def test_explicit_report_intent(text, expected):
    assert is_report_request(text) == expected