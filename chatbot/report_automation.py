"""Deterministic, topic-aware report sources, titles and read-only plans.

Titles are presentation labels only; callers retain the original topic/question
separately. Plans use one scoped base table, never guessed joins or data cleaning.
"""
import re

from chatbot.db import Schema
from chatbot.reports import ReportEvidence, ReportQuery, aggregate_result, evidence_time, ReportError


_TOPICS = {
    "pay": {"pay", "paid", "salary", "salaries", "compensation", "wage", "wages", "payroll", "earnings"},
    "hiring": {"hiring", "hire", "hires", "hired", "recruitment", "recruiting", "join", "joined", "joining"},
    "status": {"status", "statuses", "active", "inactive", "employment"},
    "geography": {"geography", "geographic", "location", "locations", "city", "cities", "country", "countries", "region", "regions"},
    "department": {"department", "departments", "dept", "depts"},
    "role": {"role", "roles", "job", "jobs", "title", "titles", "position", "positions"},
}
_METRICS = {"pay", "hiring", "status", "geography"}
_STOP_WORDS = set((
    "create build make generate prepare give show want need export download report reports "
    "a an the of from my queries results please based on about for and or by with which what "
    "is are has have how many highest lowest most least compare comparison overview "
    "workforce business employee employees staff headcount composition summary"
).split())
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_TYPES = {
    "text": r"(?:CHAR|VARCHAR|NCHAR|NVARCHAR|TINYTEXT|TEXT|MEDIUMTEXT|LONGTEXT|ENUM)",
    "numeric": r"(?:TINYINT|SMALLINT|MEDIUMINT|INT|INTEGER|BIGINT|DECIMAL|NUMERIC|FLOAT|DOUBLE(?: PRECISION)?|REAL)",
    "date": r"(?:DATE|DATETIME|TIMESTAMP)",
}


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z]+", text.lower()))


def _focus(text: str) -> set[str]:
    words = _words(text)
    focus = {kind for kind, synonyms in _TOPICS.items() if words & synonyms}
    # A bare workforce trend can use hiring dates; salary/status/location trends
    # do not establish that hire dates are the right time basis for those metrics.
    if words & {"trend", "trends"} and not focus & _METRICS:
        focus.add("hiring")
    return focus


def report_title(topic: str) -> str:
    """Return a concise display title, not a copy of the original question."""
    focus = _focus(topic)
    if "pay" in focus:
        if "department" in focus:
            return "Department Pay Report"
        if "role" in focus:
            return "Role Pay Report"
        return "Pay Report"
    if "hiring" in focus:
        return "Hiring Trends Report"
    if "status" in focus:
        return "Workforce Status Report"
    if "geography" in focus:
        return "Workforce Geography Report"
    if "department" in focus:
        return "Department Composition Report"
    if "role" in focus:
        return "Role Composition Report"
    return "Workforce Overview"


def relevant_messages(messages, scope, topic=""):
    """Prefer direct semantic evidence, then requested dimension context.

    Specific topics take precedence over generic 'workforce/business' wording.
    SQL alone never establishes relevance; privacy/provenance checks remain first.
    The best six unique queries are returned in their original history order.
    """
    focus = _focus(topic)
    metrics = focus & _METRICS
    dimensions = focus - _METRICS
    meaningful = _words(topic) - _STOP_WORDS
    selected = []
    seen = set()
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if (message.get("read_only") is not True or message.get("error")
                or message.get("scope") != scope or not message.get("query_id")):
            continue
        try:
            evidence = ReportEvidence(message["query_id"], message["question"], message["sql"],
                                      message.get("result"), evidence_time(message["executed_at"]), "snapshot")
            safe = aggregate_result(evidence)
        except (KeyError, ValueError, TypeError):
            continue
        if safe is None or not safe.rows or evidence.sql in seen:
            continue
        context_text = evidence.question + " " + " ".join(safe.columns)
        context_focus = _focus(context_text)
        if metrics:
            direct = bool(metrics & context_focus)
            supporting = bool(dimensions & context_focus)
            if not direct and not supporting:
                continue
            score = 3 * direct + supporting
        elif dimensions:
            if not dimensions & context_focus:
                continue
            score = len(dimensions & context_focus)
        else:
            if meaningful and not meaningful & _words(context_text):
                continue
            score = 0
        seen.add(evidence.sql)
        selected.append((score, index, message))
    best = sorted(selected, key=lambda item: (item[0], item[1]), reverse=True)[:6]
    return [message for _, _, message in sorted(best, key=lambda item: item[1])]


def _typed_column(table: dict, names: tuple[str, ...], kind: str) -> str | None:
    """Match exact supported column names and SQL types, not substrings."""
    for name in names:
        for column in table.get("columns", ()):
            actual = column.get("name", "")
            if (isinstance(actual, str) and actual.lower() == name
                    and _IDENTIFIER.fullmatch(actual)
                    and re.fullmatch(_TYPES[kind] + r"(?:\s*\([^)]*\))?(?:\s+(?:UNSIGNED|ZEROFILL))*\s*",
                                     str(column.get("type", "")).strip(), re.I)):
                return actual
    return None


def _fields(table: dict) -> dict[str, str | None]:
    return {
        "department": _typed_column(table, ("department", "department_name"), "text"),
        "role": _typed_column(table, ("job_title", "job_role", "role"), "text"),
        "status": _typed_column(table, ("status",), "text"),
        "city": _typed_column(table, ("city",), "text"),
        "country": _typed_column(table, ("country",), "text"),
        "region": _typed_column(table, ("region",), "text"),
        "salary": _typed_column(table, ("salary",), "numeric"),
        "hire": _typed_column(table, ("join_date", "hire_date", "hired_at"), "date"),
    }


def automatic_queries(schema: Schema, topic: str = "") -> list[ReportQuery]:
    """Choose up to six topic-specific aggregates from one safe scoped table.

    Pay comparisons use total group headcount alongside positive-salary counts and
    means. NULL, empty and dirty labels are grouped as stored, without normalization.
    The live report service must validate the whole plan before executing any read.
    """
    tables = schema.tables if schema.tables is not None else ({"name": schema.table, "columns": schema.columns},)
    if not schema.exists or not tables:
        raise ReportError("No selected base table is available for a report.")
    safe_tables = [t for t in tables if isinstance(t.get("name"), str) and _IDENTIFIER.fullmatch(t["name"])]
    if not safe_tables:
        raise ReportError("No safe base table is available in the selected scope.")
    focus = _focus(topic)
    metrics = focus & _METRICS

    def score(table):
        fields = _fields(table)
        capabilities = {"pay": bool(fields["salary"]), "hiring": bool(fields["hire"]),
                        "status": bool(fields["status"]),
                        "geography": any(fields[n] for n in ("city", "country", "region")),
                        "department": bool(fields["department"]), "role": bool(fields["role"])}
        # Requested metrics beat contextual dimensions and the configured default.
        return (sum(capabilities[k] for k in metrics),
                sum(capabilities[k] for k in focus),
                sum(capabilities.values()) if not focus else 0,
                table["name"] == schema.table)

    table = max(safe_tables, key=score)
    fields = _fields(table)
    quoted = f"`{table['name']}`"
    queries: list[ReportQuery] = []

    def add(question, sql):
        if len(queries) < 6:
            queries.append(ReportQuery(f"q{len(queries)+1}", question, sql))

    def composition(alias):
        if fields[alias]:
            field = f"`{fields[alias]}`"
            add(f"Workforce composition counts by {alias}; missing and stored labels retained",
                f"SELECT {field} AS {alias}, COUNT(*) AS employee_count FROM {quoted} GROUP BY {field}")

    def salary_profile(alias):
        if fields["salary"] and fields[alias]:
            salary, dimension = f"`{fields['salary']}`", f"`{fields[alias]}`"
            valid = f"CASE WHEN {salary}>0 THEN {salary} END"
            add(f"{alias.title()} pay profile; positive salaries only; currency and pay period unspecified",
                f"SELECT {dimension} AS {alias}, COUNT(*) AS employee_count, "
                f"COUNT({valid}) AS salary_count, AVG({valid}) AS average_salary "
                f"FROM {quoted} GROUP BY {dimension}")

    def salary_quality():
        if fields["salary"]:
            field = f"`{fields['salary']}`"
            valid = f"CASE WHEN {field}>0 THEN {field} END"
            add("Positive salary summary and salary quality; currency and pay period unspecified",
                f"SELECT COUNT(*) AS employee_count, COUNT({valid}) AS salary_count, "
                f"AVG({valid}) AS average_positive_salary, SUM({valid}) AS total_positive_salary, "
                f"SUM(CASE WHEN {field} IS NULL THEN 1 ELSE 0 END) AS missing_salary_count, "
                f"SUM(CASE WHEN {field}<=0 THEN 1 ELSE 0 END) AS nonpositive_salary_count FROM {quoted}")

    def hiring():
        if fields["hire"]:
            field = f"`{fields['hire']}`"
            add("Monthly hiring trend from stored dates; missing dates retained; timezone unspecified",
                f"SELECT DATE_FORMAT({field}, '%Y-%m') AS month, COUNT(*) AS hires FROM {quoted} "
                f"GROUP BY DATE_FORMAT({field}, '%Y-%m') ORDER BY month")

    add("Workforce record count", f"SELECT COUNT(*) AS employee_count FROM {quoted}")
    if "pay" in focus:
        composition("department")
        composition("role")
        salary_profile("department")
        if "role" in focus or not fields["department"]:
            salary_profile("role")
        salary_quality()
    elif not focus:
        composition("department")
        composition("role")
        salary_quality()
        hiring()
        composition("status")
        composition("city")
    else:
        for alias in ("department", "role"):
            if alias in focus:
                composition(alias)
    # Explicit mixed topics can use remaining slots, never implicit geography/hiring.
    if "hiring" in focus:
        hiring()
    if "status" in focus:
        composition("status")
    if "geography" in focus:
        for alias in ("city", "country", "region"):
            composition(alias)
    return queries