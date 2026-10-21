from dataclasses import replace
import sqlite3
from unittest.mock import Mock

import pytest
import sqlglot
from sqlglot import exp

from chatbot.config import Settings
from chatbot.db import QueryResult, Schema
from chatbot.report_automation import automatic_queries, relevant_messages, report_title
from chatbot.report_service import ReportService
from chatbot.reports import ReportError, ReportQuery, aggregate_result, ReportEvidence, evidence_time
from chatbot.sql_policy import PolicyError, validate_sql


SCOPE = ("demo", None)


@pytest.fixture(autouse=True)
def no_model(monkeypatch):
    model = Mock(side_effect=AssertionError("Report automation must work without a model"))
    monkeypatch.setattr("chatbot.llm.ChatModel", model)
    yield
    model.assert_not_called()


@pytest.fixture
def schema():
    return Schema("demo", "employees_messy", (), (), (), "8", tables=(
        {"name": "orders", "columns": ({"name": "amount", "type": "DECIMAL(12,2)"},)},
        {"name": "employees_messy", "columns": (
            {"name": "emp_id", "type": "INT"},
            {"name": "name", "type": "VARCHAR(80)"},
            {"name": "email", "type": "VARCHAR(80)"},
            {"name": "department", "type": "VARCHAR(80)"},
            {"name": "status", "type": "ENUM('Active','Inactive')"},
            {"name": "city", "type": "TEXT"},
            {"name": "salary", "type": "DECIMAL(12,2)"},
            {"name": "join_date_raw", "type": "VARCHAR(80)"},
            {"name": "join_date", "type": "DATE"},
        )},
    ))


def message(identifier="q1", **changes):
    return {"read_only": True, "query_id": identifier, "question": "How many employees?",
            "sql": "SELECT COUNT(*) AS employee_count FROM employees_messy",
            "result": QueryResult(["employee_count"], [[42]], 0, False, .1),
            "scope": SCOPE, "executed_at": "2026-10-15T10:00:00Z", **changes}


def test_workforce_plan_is_schema_grounded_bounded_and_readonly(schema):
    plan = automatic_queries(schema, "Workforce overview")
    assert 1 <= len(plan) <= 6
    assert len(plan) == 6
    assert len({q.id for q in plan}) == len(plan)
    assert all(isinstance(q, ReportQuery) for q in plan)
    assert plan == automatic_queries(schema, "Workforce overview")
    assert "COUNT(*)" in plan[0].sql
    for query in plan:
        checked = validate_sql(query.sql, "demo", "employees_messy", allowed_tables=schema.table_names)
        assert checked.read_only
        ast = sqlglot.parse_one(query.sql, read="mysql")
        assert {t.name for t in ast.find_all(exp.Table)} == {"employees_messy"}
        assert not list(ast.find_all(exp.Join))
        columns = {c.name for c in ast.find_all(exp.Column)}
        assert not columns & {"name", "email", "emp_id", "join_date_raw"}
    hiring = next(q for q in plan if "DATE_FORMAT" in q.sql)
    assert "`join_date`" in hiring.sql and "join_date_raw" not in hiring.sql
    assert "ORDER BY month" in hiring.sql
    salary = next(q for q in plan if "average_positive_salary" in q.sql)
    assert "missing_salary_count" in salary.sql
    assert "nonpositive_salary_count" in salary.sql
    assert "currency and pay period unspecified" in salary.question.lower()


def test_whole_schema_prefers_a_workforce_table_over_unrelated_default(schema):
    other_default = replace(schema, table="orders")
    assert all("`employees_messy`" in q.sql for q in automatic_queries(other_default))


def test_selected_table_and_legacy_schema_do_not_invent_other_tables(schema):
    selected = replace(schema, table="orders", tables=(schema.tables[0],))
    plan = automatic_queries(selected)
    assert len(plan) == 1 and "FROM `orders`" in plan[0].sql
    legacy = replace(schema, columns=schema.tables[1]["columns"], tables=None)
    assert automatic_queries(legacy) == automatic_queries(schema)


def test_text_salaries_dates_and_nontext_dimensions_are_not_silently_cleaned(schema):
    dirty = replace(schema, tables=({"name": "employees_messy", "columns": (
        {"name": "salary", "type": "TEXT"},
        {"name": "join_date", "type": "VARCHAR(80)"},
        {"name": "join_date_raw", "type": "VARCHAR(80)"},
        {"name": "department", "type": "INT"},
    )},))
    plan = automatic_queries(dirty)
    assert len(plan) == 1
    assert plan[0].sql == "SELECT COUNT(*) AS employee_count FROM `employees_messy`"


@pytest.mark.parametrize("unsafe_sql", [
    "DELETE FROM employees_messy",
    "UPDATE employees_messy SET salary = 1",
    "SELECT COUNT(*) FROM outside",
    "SELECT COUNT(*) FROM other.employees_messy",
    "SELECT SLEEP(1) FROM employees_messy",
    "SELECT COUNT(*) FROM employees_messy; DELETE FROM employees_messy",
    "SELECT * INTO OUTFILE '/tmp/report.csv' FROM employees_messy",
])
def test_automatic_whole_plan_validated_before_any_read(schema, unsafe_sql):
    settings = Settings("localhost", 3306, "user", "secret", "demo", schema_scope=True,
                        allow_mutations=True)
    database = Mock()
    database.discover_schema.return_value = schema
    runner = ReportService(settings, database)
    plan = automatic_queries(schema)
    plan[-1] = replace(plan[-1], sql=unsafe_sql)
    with pytest.raises((PolicyError, ReportError)):
        runner.live("Workforce overview", plan, "unsafe-plan")
    database.execute.assert_not_called()
    with pytest.raises(ReportError, match="consumed"):
        runner.live("Workforce overview", automatic_queries(schema), "unsafe-plan")
    database.execute.assert_not_called()


@pytest.mark.parametrize("changes", [
    {"scope": ("other", None)},
    {"scope": ("demo", "employees_messy")},
    {"scope": ("demo", "orders")},
    {"scope": None},
    {"read_only": False},
    {"read_only": None},
    {"read_only": 1},
    {"error": "Database read failed"},
    {"query_id": None},
    {"executed_at": "not a timestamp"},
    {"executed_at": "2026-10-15T10:00:00"},
    {"result": None},
    {"result": QueryResult(["employee_count"], [], 0, False, .1)},
])
def test_relevant_messages_reject_wrong_scope_errors_and_unusable_evidence(changes):
    good = message("good")
    assert relevant_messages([good, message("bad", **changes)], SCOPE, "Workforce overview") == [good]


@pytest.mark.parametrize("missing", ["question", "sql", "executed_at", "scope", "query_id", "read_only"])
def test_relevant_messages_omit_missing_provenance(missing):
    incomplete = message()
    incomplete.pop(missing)
    assert relevant_messages([incomplete], SCOPE) == []


@pytest.mark.parametrize("sql,columns,rows", [
    ("SELECT name, email, salary FROM employees_messy", ["name", "email", "salary"], [["Private Person", "private@example.com", 100]]),
    ("SELECT name AS department, COUNT(*) AS records FROM employees_messy GROUP BY name", ["department", "records"], [["Private Person", 1]]),
    ("SELECT MIN(email) AS records FROM employees_messy", ["records"], [["private@example.com"]]),
    ("SELECT department, COUNT(*) AS records FROM employees_messy GROUP BY department", ["department", "records"], [["private@example.com", 1]]),
])
def test_relevant_messages_omit_raw_and_privacy_sensitive_results(sql, columns, rows):
    private = message("private", sql=sql, result=QueryResult(columns, rows, 0, False, .1))
    assert relevant_messages([private], SCOPE, "Workforce overview") == []
    good = message("good")
    assert relevant_messages([private, good], SCOPE) == [good]


@pytest.mark.parametrize("question,column", [
    ("What is the salary summary?", "amount"),
    ("What is the average?", "average_salary"),
])
def test_relevant_messages_match_keywords_in_questions_or_safe_columns(question, column):
    salary = message("salary", question=question,
                     sql=f"SELECT AVG(salary) AS {column} FROM employees_messy",
                     result=QueryResult([column], [[100]], 0, False, .1))
    count = message("count")
    assert relevant_messages([salary, count], SCOPE, "Please build a salary report based on my results") == [salary]
    assert relevant_messages([salary, count], SCOPE, "department report") == []


def test_relevant_messages_do_not_match_keywords_only_in_sql():
    metric = message(question="Metric", sql="SELECT AVG(salary) AS amount FROM employees_messy",
                     result=QueryResult(["amount"], [[100]], 0, False, .1))
    assert relevant_messages([metric], SCOPE, "salary report") == []


@pytest.mark.parametrize("topic", ["", "Please build a report", "Workforce overview", "Business overview"])
def test_generic_focus_accepts_all_safe_aggregates(topic):
    count = message("count")
    salary = message("salary", question="Salary summary",
                     sql="SELECT AVG(salary) AS average_salary FROM employees_messy",
                     result=QueryResult(["average_salary"], [[100]], 0, False, .1))
    assert relevant_messages([count, salary], SCOPE, topic) == [count, salary]


def test_relevant_messages_deduplicate_sql_using_latest_usable_result():
    old = message("old", result=QueryResult(["employee_count"], [[10]], 0, False, .1))
    latest = message("latest")
    failed = message("failed", error="Unavailable")
    assert relevant_messages([old, latest, failed], SCOPE) == [latest]
    assert latest["result"].rows == [[42]]


def test_relevant_messages_keep_latest_six_unique_queries_in_chronological_order():
    messages = [message(f"q{i}", sql=f"SELECT COUNT(*) AS employee_count FROM employees_messy WHERE salary > {i}")
                for i in range(10)]
    assert relevant_messages(messages, SCOPE) == messages[-6:]
    assert len(messages) == 10  # Selection does not consume or mutate history.


def test_selected_table_scope_requires_exact_match_and_keeps_safe_dimensions():
    table_scope = ("demo", "employees_messy")
    grouped = message("grouped", scope=table_scope, question="Departments",
                      sql="SELECT department, COUNT(*) AS records FROM employees_messy GROUP BY department",
                      result=QueryResult(["department", "records"], [[None, 2], ["Sales", 4]], 0, False, .1))
    assert relevant_messages([message(), grouped], table_scope, "department report") == [grouped]
    evidence = ReportEvidence(grouped["query_id"], grouped["question"], grouped["sql"],
                              grouped["result"], evidence_time(grouped["executed_at"]), "snapshot")
    safe = aggregate_result(evidence)
    assert safe is not None
    assert safe.rows == [[None, "2"], ["Sales", "4"]]


@pytest.mark.parametrize("topic,title", [
    ("Which departments and roles have the highest and lowest salaries?", "Department Pay Report"),
    ("Please prepare a workforce compensation overview by dept", "Department Pay Report"),
    ("Job title wages report", "Role Pay Report"),
    ("Pay summary", "Pay Report"),
    ("Show recruitment trends in a report", "Hiring Trends Report"),
    ("Monthly hiring", "Hiring Trends Report"),
    ("Workforce trends", "Hiring Trends Report"),
    ("Salary trends by department", "Department Pay Report"),
    ("Workforce status", "Workforce Status Report"),
    ("Business overview by location", "Workforce Geography Report"),
    ("Department composition", "Department Composition Report"),
    ("Roles report", "Role Composition Report"),
    ("Workforce overview", "Workforce Overview"),
    ("", "Workforce Overview"),
    ("Please build a report " * 30, "Workforce Overview"),
])
def test_concise_report_titles_keep_original_questions_separate(topic, title):
    original = topic
    assert report_title(topic) == title
    assert len(report_title(topic)) < 60
    assert topic == original


@pytest.mark.parametrize("topic", [
    "Department pay report", "Workforce salary overview by department",
    "Business compensation report by dept", "Which departments pay the highest and lowest wages?",
    "Department salary trends report",
])
def test_pay_plan_prioritizes_comparisons_and_quality_not_hiring_or_geography(schema, topic):
    plan = automatic_queries(schema, topic)
    assert len(plan) == 4
    assert all("DATE_FORMAT" not in q.sql and "`city`" not in q.sql and "`status`" not in q.sql for q in plan)
    profile = next(q for q in plan if "AS average_salary" in q.sql)
    assert "`department` AS department" in profile.sql
    assert "COUNT(*) AS employee_count" in profile.sql
    assert "COUNT(CASE WHEN `salary`>0 THEN `salary` END) AS salary_count" in profile.sql
    assert "AVG(CASE WHEN `salary`>0 THEN `salary` END) AS average_salary" in profile.sql
    assert "GROUP BY `department`" in profile.sql
    assert "WHERE" not in profile.sql  # Headcount includes invalid/missing salaries.
    assert not any("role" in q.sql for q in plan)
    assert any("missing_salary_count" in q.sql and "nonpositive_salary_count" in q.sql for q in plan)
    for q in plan:
        assert validate_sql(q.sql, schema.database, schema.table, allowed_tables=schema.table_names).read_only
        assert not re_currency(q.question + " " + q.sql)


def re_currency(text):
    return any(unit in text.lower() for unit in ("usd", "dollars", "eur", "gbp", "annual", "monthly salary"))


@pytest.mark.parametrize("role_name,datatype", [("job_title", "VARCHAR(80)"), ("role", "ENUM('Engineer','Manager')")])
def test_department_and_role_pay_plan_includes_typed_role_comparison_within_six(schema, role_name, datatype):
    employee_table = schema.tables[1]
    role_schema = replace(schema, tables=({**employee_table, "columns": employee_table["columns"] + (
        {"name": role_name, "type": datatype},)},))
    plan = automatic_queries(role_schema, "Which departments and roles have the highest/lowest salaries?")
    assert len(plan) == 6
    assert [q.id for q in plan] == [f"q{i}" for i in range(1, 7)]
    role_queries = [q for q in plan if f"`{role_name}`" in q.sql]
    assert len(role_queries) == 2  # Composition plus pay profile, not invented roles.
    assert any("AS average_salary" in q.sql and "AS salary_count" in q.sql for q in role_queries)
    assert "missing_salary_count" in plan[-1].sql
    assert not any("`status`" in q.sql or "`city`" in q.sql or "DATE_FORMAT" in q.sql for q in plan)
    for q in plan:
        assert validate_sql(q.sql, "demo", "employees_messy", allowed_tables=role_schema.table_names).read_only
        assert not list(sqlglot.parse_one(q.sql, read="mysql").find_all(exp.Join))


@pytest.mark.parametrize("name,datatype", [
    ("job_title", "INT"), ("role", "JSON"), ("job_title", "FAKEVARCHAR"),
    ("role", "VARCHARISH"), ("job_title_raw", "TEXT"), ("position", "TEXT"),
])
def test_roles_require_exact_names_and_real_text_types(schema, name, datatype):
    employee = schema.tables[1]
    scoped = replace(schema, tables=({**employee, "columns": employee["columns"] + (
        {"name": name, "type": datatype},)},))
    plan = automatic_queries(scoped, "Department and role pay report")
    assert all(f"`{name}`" not in q.sql for q in plan)
    assert len(plan) == 4


@pytest.mark.parametrize("topic,required,forbidden", [
    ("Workforce hiring trends", "DATE_FORMAT", ("`salary`", "`city`", "`status`", "`department`")),
    ("Business status report", "`status`", ("`salary`", "`city`", "DATE_FORMAT", "`department`")),
    ("Geography overview", "`city`", ("`salary`", "`status`", "DATE_FORMAT", "`department`")),
    ("Department workforce composition", "`department`", ("`salary`", "`status`", "`city`", "DATE_FORMAT")),
])
def test_specific_topics_do_not_receive_generic_overview_plan(schema, topic, required, forbidden):
    plan = automatic_queries(schema, topic)
    assert len(plan) == 2
    sql = " ".join(q.sql for q in plan)
    assert required in sql
    assert all(field not in sql for field in forbidden)


def test_mixed_pay_hiring_topic_excludes_unrequested_status(schema):
    plan = automatic_queries(schema, "Department compensation and hiring trends")
    assert len(plan) == 5
    assert any("DATE_FORMAT" in q.sql for q in plan)
    assert all("`city`" not in q.sql and "`status`" not in q.sql for q in plan)


def test_explicit_status_is_not_dropped_from_a_pay_plan(schema):
    plan = automatic_queries(schema, "Department pay and employment status report")
    status = [q for q in plan if "`status`" in q.sql]
    assert len(status) == 1, "Explicit status must survive the pay-topic branch"
    assert status[0].sql == (
        "SELECT `status` AS status, COUNT(*) AS employee_count "
        "FROM `employees_messy` GROUP BY `status`"
    )
    assert len(plan) == 5
    assert any("AS average_salary" in q.sql for q in plan)
    assert all("`city`" not in q.sql and "DATE_FORMAT" not in q.sql for q in plan)


@pytest.mark.parametrize("topic,column,datatype", [
    ("Salary report", "salary", "DECIMAL(10,2)"),
    ("Hiring trends", "hire_date", "DATETIME(6)"),
    ("Status overview", "status", "VARCHAR(20)"),
    ("Region report", "region", "TEXT"),
])
def test_schema_table_selection_prefers_typed_topic_capability(schema, topic, column, datatype):
    # A contextual workforce table cannot override a table with the actual metric.
    tables = ({"name": "employees_messy", "columns": ({"name": "department", "type": "TEXT"},)},
              {"name": "observed", "columns": ({"name": column, "type": datatype},)})
    scoped = replace(schema, tables=tables)
    assert all("FROM `observed`" in q.sql for q in automatic_queries(scoped, topic))


@pytest.mark.parametrize("column,datatype,topic", [
    ("salary", "POINT", "Pay report"), ("salary", "INTERVAL", "Pay report"),
    ("salary", "DECIMALISH", "Pay report"), ("salary_raw", "DECIMAL(10,2)", "Pay report"),
    ("hire_date", "DATE_TEXT", "Hiring report"), ("hire_date_raw", "DATE", "Hiring report"),
    ("status", "VARCHARISH", "Status report"), ("city", "JSON", "Geography report"),
])
def test_typed_templates_do_not_use_fuzzy_names_or_types(schema, column, datatype, topic):
    scoped = replace(schema, tables=({"name": "employees_messy", "columns": (
        {"name": column, "type": datatype},)},))
    assert len(automatic_queries(scoped, topic)) == 1


def test_actual_identifier_case_is_preserved_and_unsafe_columns_are_ignored(schema):
    scoped = replace(schema, tables=({"name": "Employees", "columns": (
        {"name": "Department_Name", "type": "VARCHAR(80)"},
        {"name": "Salary", "type": "DECIMAL(10,2) UNSIGNED"},
        {"name": "job_title`; DROP TABLE Employees; --", "type": "TEXT"},)},))
    plan = automatic_queries(scoped, "Department pay")
    assert any("`Department_Name`" in q.sql and "`Salary`" in q.sql for q in plan)
    assert all("DROP" not in q.sql for q in plan)
    assert all(validate_sql(q.sql, "demo", "Employees", allowed_tables=scoped.table_names).read_only for q in plan)


@pytest.mark.parametrize("tables", [(), ({"name": "bad`name", "columns": ()},),
                                    ({"name": "other.employees", "columns": ()},)])
def test_missing_or_unsafe_tables_raise_instead_of_empty_or_cross_scope_plan(schema, tables):
    with pytest.raises(ReportError, match="table"):
        automatic_queries(replace(schema, tables=tables), "Department pay")
    with pytest.raises(ReportError, match="table"):
        automatic_queries(replace(schema, tables=None, columns=()), "Department pay")


def test_unsafe_default_table_can_only_fall_back_inside_provided_scope(schema):
    safe = schema.tables[1]
    scoped = replace(schema, table="bad`name", tables=({"name": "bad`name", "columns": ()}, safe))
    assert all("FROM `employees_messy`" in q.sql for q in automatic_queries(scoped, "Pay report"))


def test_pay_profiles_keep_dirty_labels_and_valid_salary_denominators(schema):
    plan = automatic_queries(schema, "Department pay")
    profile = next(q for q in plan if "AS average_salary" in q.sql)
    composition = next(q for q in plan if "GROUP BY `department`" in q.sql and "average_salary" not in q.sql)
    quality = next(q for q in plan if "missing_salary_count" in q.sql)
    assert not any(word in q.sql.upper() for q in plan for word in ("COALESCE", "TRIM", "LOWER", "CAST", "REPLACE"))
    with sqlite3.connect(":memory:") as db:
        db.execute("CREATE TABLE employees_messy (department TEXT, salary NUMERIC)")
        db.executemany("INSERT INTO employees_messy VALUES (?, ?)", [
            ("Sales", 100), ("Sales", 300), ("Sales", None), ("Sales", 0), ("Sales", -5),
            (" sales ", 900), ("Missing", 50), (None, 60), ("", -1),
        ])
        profiles = {row[0]: row[1:] for row in db.execute(profile.sql)}
        counts = dict(db.execute(composition.sql))
        assert profiles["Sales"] == (5, 2, 200)
        assert profiles[" sales "] == (1, 1, 900)
        assert profiles[""] == (1, 0, None)
        assert counts[None] == counts["Missing"] == counts[""] == 1
        assert len(counts) == 5  # NULL and the literal Missing label stay distinct.
        summary = db.execute(quality.sql).fetchone()
        assert summary[:2] == (9, 5)
        assert summary[-2:] == (1, 3)


@pytest.mark.parametrize("topic", ["Business salary report", "Workforce compensation overview", "Payroll report", "Wage report"])
def test_specific_semantic_focus_is_not_overridden_by_generic_workforce_words(topic):
    salary = message("salary", question="Average pay",
                     sql="SELECT AVG(salary) AS amount FROM employees_messy",
                     result=QueryResult(["amount"], [[100]], 0, False, .1))
    assert relevant_messages([salary, message("count")], SCOPE, topic) == [salary]


@pytest.mark.parametrize("topic,question,column", [
    ("Recruitment trends", "Monthly hires", "hires"),
    ("Geography report", "Employees by city", "records"),
    ("Employment status", "Active and inactive workforce", "records"),
    ("Salary report", "Average compensation", "amount"),
])
def test_semantic_synonyms_select_direct_evidence(topic, question, column):
    evidence = message("direct", question=question,
                       sql=f"SELECT COUNT(*) AS {column} FROM employees_messy",
                       result=QueryResult([column], [[42]], 0, False, .1))
    assert relevant_messages([evidence, message("count")], SCOPE, topic) == [evidence]


def test_direct_pay_evidence_outranks_newer_department_context_at_six_query_cap():
    direct = message("direct", question="Highest and lowest departmental compensation",
                     sql="SELECT department, AVG(salary) AS average_salary FROM employees_messy GROUP BY department",
                     result=QueryResult(["department", "average_salary"], [["Sales", 100]], 0, False, .1))
    context = [message(f"support{i}", question="Department composition",
                       sql=f"SELECT department, COUNT(*) AS records FROM employees_messy WHERE salary > {i} GROUP BY department",
                       result=QueryResult(["department", "records"], [["Sales", 10]], 0, False, .1))
               for i in range(8)]
    # Direct 'departmental' wording still has compensation as strong pay evidence.
    selected = relevant_messages([direct, *context, message("generic")], SCOPE, "Workforce department pay report")
    assert selected == [direct, *context[-5:]]


def test_direct_evidence_priority_preserves_latest_sql_deduplication():
    old = message("old", question="Salary report")
    latest = message("latest", question="Compensation overview")
    assert relevant_messages([old, latest], SCOPE, "Salary report") == [latest]