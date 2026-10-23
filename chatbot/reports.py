"""Report contracts and deterministic, evidence-only presentation.

UI interface (no UI, database, or model side effects in this module):
* ReportService(settings, database, scope_table=None).snapshot(title, messages)
  accepts selected message dicts with read_only=True, sql, result (QueryResult),
  executed_at (timezone-aware datetime or ISO string), query_id and scope.
  Scope is (database, scope_table), with None for schema scope; the exact labels
  database / database.table are also accepted. Old unprovenanced messages are
  not eligible. Snapshot NEVER discovers metadata or executes SQL.
* .live(title, queries, run_id) consumes a session-local ID even on failure.
  Instantiate once per chat/scope, NOT on each rerun. At 64 consumed IDs create
  a new session service; IDs are never evicted/re-enabled on the old instance.
* .build_report(title, evidence, source) and build_report below are local only.
* ChatModel.plan_report(topic, scoped_schema, question_context) proposes only;
  call only after question/schema consent, preview then explicitly run .live.
  ChatModel.report_narrative(report, consent=True) is optional result sharing;
  export/report rendering never calls it automatically.
* report_pdf(report) returns bytes, does not fetch or refresh anything.

Evidence retains internal SQL/results; UI must not dump raw evidence into PDFs.
Results mean the executed query output, NOT the full business population.
Live reads use separate connections, not a consistent transactional snapshot.
No inferred currency, salary pay period, employer cost, revenue, or causality.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import re
from typing import Literal
from uuid import uuid4

import sqlglot
from sqlglot import exp

from chatbot.db import QueryResult, Schema

ReportSource = Literal["snapshot", "live"]
ReportScope = tuple[str, str | None]
MAX_QUERIES = 6
MAX_FINDINGS = 120


class ReportError(ValueError):
    """Invalid provenance, ambiguous metrics, or an expired/unsafe report job."""


@dataclass(frozen=True)
class ReportQuery:
    id: str
    question: str
    sql: str


@dataclass(frozen=True)
class ReportEvidence:
    id: str
    question: str
    sql: str
    result: QueryResult | None
    executed_at: datetime
    source: ReportSource
    error: str = ""


@dataclass(frozen=True)
class BusinessReport:
    id: str
    title: str
    scope: ReportScope
    source: ReportSource
    created_at: datetime
    evidence: tuple[ReportEvidence, ...]
    summary: str
    findings: tuple[str, ...]
    recommendations: tuple[str, ...]
    limitations: tuple[str, ...]
    question: str = ""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def evidence_time(value: object) -> datetime:
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
    except ValueError as exc:
        raise ReportError("Evidence needs a valid execution timestamp.") from exc
    if not isinstance(timestamp, datetime) or timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ReportError("Evidence needs a timezone-aware execution timestamp.")
    return timestamp.astimezone(timezone.utc)


def validate_queries(queries: object) -> tuple[ReportQuery, ...]:
    if not isinstance(queries, (list, tuple)) or not 1 <= len(queries) <= MAX_QUERIES:
        raise ReportError("Select between one and six report queries.")
    ids: set[str] = set()
    for query in queries:
        if (not isinstance(query, ReportQuery) or not isinstance(query.id, str)
                or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", query.id)
                or query.id in ids or not isinstance(query.question, str)
                or not 0 < len(query.question.strip()) <= 2000
                or not isinstance(query.sql, str) or not 0 < len(query.sql.strip()) <= 20000):
            raise ReportError("Report queries need unique IDs, bounded questions and SQL.")
        ids.add(query.id)
    return tuple(queries)


_PRIVATE = re.compile(r"name|email|phone|address|(?:employee|emp|customer|person|user)[_ ]?id|ssn|birth", re.I)
_DIMENSION = re.compile(r"^(?:department(?:_name)?|category(?:_name)?|job_title|job_role|role|city|country|region|status|month|year|period|team|product_category)$", re.I)


def aggregate_result(evidence: ReportEvidence) -> QueryResult | None:
    """Fail closed for external sharing: aggregate projections + known dimensions only.

    This is data minimization, not anonymization: small groups can be identifying.
    SQL, questions, and individual records are never sent as aggregate data.
    Nested SELECT/CTE results may execute safely but are omitted from external
    presentation because intermediate aliases are not proven privacy-safe.
    """
    result = evidence.result
    if result is None or evidence.error or not result.columns:
        return None
    try:
        ast = sqlglot.parse_one(evidence.sql, read="mysql")
        if (not isinstance(ast, exp.Select) or ast.find(exp.Window) or ast.find(exp.Subquery)
            or sum(isinstance(node, exp.Select) for node in ast.walk()) != 1):
            return None
        projections = ast.expressions
        if len(projections) != len(result.columns):
            return None
        has_aggregate = False
        for projection, column in zip(projections, result.columns):
            if (not isinstance(column, str) or len(column) > 100
                    or (_PRIVATE.search(column) and not _DIMENSION.fullmatch(column))):
                return None
            expression = projection.this if isinstance(projection, exp.Alias) else projection
            if column.lower() in {"job_title", "job_role", "role"} and (
                    not isinstance(expression, exp.Column)
                    or expression.name.lower() not in {"job_title", "job_role", "role"}):
                return None
            aggregates = list(expression.find_all(exp.AggFunc))
            if aggregates:
                has_aggregate = True
                # COUNT(identifier) is safe as a count, MIN/MAX(contact) is not.
                if any(_PRIVATE.search(c.name) and not isinstance(a, exp.Count)
                       for a in aggregates for c in a.find_all(exp.Column)):
                    return None
                # No free record column mixed into an aggregate expression.
                if any(not any(c is child for a in aggregates for child in a.walk())
                       for c in expression.find_all(exp.Column)):
                    return None
            elif (not _DIMENSION.fullmatch(column) or not ast.args.get("group")
                                    or not list(expression.find_all(exp.Column))
                                    or any(not (_DIMENSION.fullmatch(c.name) or c.name.lower() in ("join_date", "hire_date", "hired_at", "order_date"))
                                                 for c in expression.find_all(exp.Column))):
                return None
        if not has_aggregate:
            return None
        safe_rows = []
        for row in result.rows:
            if len(row) != len(result.columns):
                return None
            values = []
            for column, value in zip(result.columns, row):
                if value is None:
                    values.append(None)
                elif _DIMENSION.fullmatch(column):
                    text = str(value)
                    if len(text) > 120 or "@" in text or re.search(r"\d{7,}", text):
                        return None
                    values.append(text)
                else:
                    number = decimal_value(value)
                    if number is None:
                        return None
                    values.append(str(number))
            safe_rows.append(values)
        return QueryResult(list(result.columns), safe_rows, 0, result.truncated, result.elapsed_seconds)
    except (sqlglot.errors.SqlglotError, ValueError, TypeError, AttributeError):
        return None


def decimal_value(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool) or len(str(value)) > 200:
        return None
    try:
        number = Decimal(str(value))
        return number if number.is_finite() and abs(number.adjusted()) <= 100 else None
    except (InvalidOperation, ValueError):
        return None


def verified_findings(evidence: ReportEvidence) -> tuple[str, ...]:
    result = aggregate_result(evidence)
    if result is None:
        return ()
    findings = []
    for row in result.rows:
        labels = ", ".join(f"{c}={v if v is not None else 'missing'}" for c, v in zip(result.columns, row)
                           if _DIMENSION.fullmatch(c))
        for column, value in zip(result.columns, row):
            if _DIMENSION.fullmatch(column):
                continue
            number = decimal_value(value)
            rendered = format(number, "f") if number is not None else "missing"
            findings.append(f"[{evidence.id}] {column}{' (' + labels + ')' if labels else ''}: {rendered}.")
            if len(findings) >= MAX_FINDINGS:
                return tuple(findings)
    return tuple(findings)


def build_report(title: str, evidence: tuple[ReportEvidence, ...], *, scope: ReportScope,
                 source: ReportSource) -> BusinessReport:
    """Build local authoritative sections. Numeric facts use Decimal, never AI."""
    if not isinstance(title, str) or not title.strip() or len(title) > 200:
        raise ReportError("Use a nonempty report title of at most 200 characters.")
    if source not in ("snapshot", "live") or not 1 <= len(evidence) <= MAX_QUERIES:
        raise ReportError("Reports require one to six evidence sections and a valid source.")
    validate_queries([ReportQuery(e.id, e.question, e.sql) for e in evidence])
    if any(e.source != source for e in evidence):
        raise ReportError("Evidence sources must match the report source.")
    for item in evidence:
        evidence_time(item.executed_at)
    limitations = [
        "Values describe the executed queries, including their filters and limits; they do not establish full-population completeness.",
        "Currency, salary pay period and full employer cost are unspecified. Generic amounts are not automatically revenue or profit.",
        "Missing metrics require clarification; no forecasts, targets or causal explanations are inferred.",
        "Only aggregate-safe evidence is presented; individual records and identifying groups are omitted. Small groups are not guaranteed anonymous.",
    ]
    if source == "live":
        limitations.append("Fresh reads use separate connections over the evidence timestamps, not one consistent transactional snapshot.")
    else:
        limitations.append("Retained conversation results only; no database refresh or current-schema verification was performed.")
    findings: list[str] = []
    insights: list[str] = []
    recommendations: list[str] = []
    for item in evidence:
        if item.error or item.result is None:
            limitations.append(f"[{item.id}] Evidence unavailable: {item.error or 'retained result missing'}")
            continue
        safe = aggregate_result(item)
        if safe is None:
            limitations.append(f"[{item.id}] Nonaggregate or privacy-sensitive output omitted from report sections.")
        elif not safe.rows:
            limitations.append(f"[{item.id}] No rows returned; missing values are not zero.")
        else:
            findings.extend(verified_findings(item))
            # Useful business observations are computed locally, not invented by a model.
            dimensions = [i for i, c in enumerate(safe.columns) if _DIMENSION.fullmatch(c)]
            measures = [i for i in range(len(safe.columns)) if i not in dimensions]
            if len(dimensions) == 1 and 2 <= len(safe.rows) <= 500:
                label_index = dimensions[0]
                for measure in measures[:2]:
                    pairs = [(str(row[label_index]), decimal_value(row[measure])) for row in safe.rows
                             if row[label_index] is not None and str(row[label_index]).strip().lower() not in ("", "missing", "unknown")]
                    pairs = [(label, value) for label, value in pairs if value is not None]
                    if len(pairs) < 2:
                        continue
                    highest = max(pairs, key=lambda pair: pair[1])
                    lowest = min(pairs, key=lambda pair: pair[1])
                    if highest[1] == lowest[1]:
                        continue
                    metric = safe.columns[measure].replace("_", " ")
                    insights.append(f"[{item.id}] Among returned groups, {highest[0]} has the highest {metric} "
                                    f"({highest[1]:f}); {lowest[0]} has the lowest ({lowest[1]:f}).")
                    recommendations.append(f"[{item.id}] Review the difference in {metric} between {highest[0]} and "
                                           f"{lowest[0]}; check group sizes, definitions and business context before taking action.")
                    dimension = safe.columns[label_index].lower()
                    if dimension in ("month", "year", "period") and all(re.fullmatch(r"\d{4}(?:-\d{2}(?:-\d{2})?)?", label) for label, _ in pairs):
                        ordered = sorted(pairs)
                        change = ordered[-1][1] - ordered[0][1]
                        insights.append(f"[{item.id}] {metric.title()} changed by {change:f} from "
                                        f"{ordered[0][0]} to {ordered[-1][0]}; this compares observed endpoints only.")
            if len(safe.rows) * len(safe.columns) > MAX_FINDINGS:
                limitations.append(f"[{item.id}] Findings display is bounded; additional aggregate values remain in internal evidence.")
        if item.result and item.result.truncated:
            limitations.append(f"[{item.id}] Query output was truncated; do not extrapolate totals from retained groups.")
        if re.search(r"\bLIMIT\b|\bWHERE\b|\bHAVING\b", item.sql, re.I):
            limitations.append(f"[{item.id}] Explicit SQL filters or selection limits apply; review internal provenance before population claims.")
    if len(findings) > MAX_FINDINGS:
        limitations.append("Display capped at the first verified findings; evidence is retained internally.")
    failures = sum(bool(e.error) or e.result is None for e in evidence)
    summary = ("Partial report: some requested evidence is unavailable. " if failures else "Evidence report. ")
    summary += "Findings below are locally verified aggregate values, not inferred business outcomes."
    if insights:
        summary += " " + " ".join(insights[:4])
    return BusinessReport(uuid4().hex, title.strip(), scope, source, utc_now(), tuple(evidence), summary,
                          tuple(findings[:MAX_FINDINGS]),
                          tuple(recommendations[:4]) + ("Confirm metric definitions, units, period and filters before making business decisions.",
                           "Request additional scoped aggregate evidence for unanswered questions."), tuple(limitations))


def is_report_request(text: str) -> bool:
    """Intent detection only; never generates SQL or executes a report."""
    if not isinstance(text, str) or re.match(r"\s*(?:SELECT|WITH|INSERT|UPDATE|DELETE|DROP|ALTER)\s|\s*CREATE\s+(?:TABLE|INDEX|DATABASE|VIEW)\b", text, re.I):
        return False
    return bool(re.search(r"\b(?:build|create|make|generate|prepare|give|show|suggest|want|need|export|download)\b.{0,100}\breports?\b|\breport\s+(?:on|about|from|based)\b", text[:2000], re.I))


def _tables(schema: Schema) -> tuple[dict, ...]:
    return schema.tables if schema.tables is not None else ({"name": schema.table, "columns": schema.columns},)


def _typed_column(table: dict, names: tuple[str, ...], kind: str) -> str | None:
    for column in table.get("columns", ()):
        name, datatype = column.get("name", ""), str(column.get("type", ""))
        if name.lower() in names and re.search(kind, datatype, re.I):
            return name
    return None


def curated_report_queries(schema: Schema, topic: str) -> list[ReportQuery]:
    """No-model templates; no fuzzy metric guessing, joins, cleaning or raw rows.

    Supported exact topics: counts, workforce composition, salary summary,
    monthly hiring, order amount summary. Other requests need clarification.
    Schema/table/column names are quoted, then the caller's live policy validates.
    """
    if not schema.exists:
        raise ReportError("No base tables are available in the selected scope.")
    topic = topic.strip().lower()
    numeric_type = r"\b(?:TINYINT|SMALLINT|MEDIUMINT|INT|INTEGER|BIGINT|DECIMAL|NUMERIC|FLOAT|DOUBLE|REAL)\b"
    output: list[ReportQuery] = []
    for table in _tables(schema):
        name = table["name"]
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            continue
        quoted = f"`{name}`"
        sql = ""
        question = ""
        if topic == "counts":
            sql, question = f"SELECT COUNT(*) AS record_count FROM {quoted}", f"Record count for {name}"
        elif topic == "workforce composition":
            department = _typed_column(table, ("department", "department_name"), r"CHAR|TEXT|ENUM")
            if department:
                sql = f"SELECT `{department}` AS department, COUNT(*) AS record_count FROM {quoted} GROUP BY `{department}`"
                question = "Record counts by department, including missing labels"
        elif topic == "salary summary":
            salary = _typed_column(table, ("salary",), numeric_type)
            if salary:
                sql = (f"SELECT COUNT(*) AS record_count, COUNT(`{salary}`) AS salary_count, "
                       f"SUM(`{salary}`) AS salary_sum, AVG(`{salary}`) AS salary_average, "
                       f"MIN(`{salary}`) AS salary_minimum, MAX(`{salary}`) AS salary_maximum FROM {quoted}")
                question = "Salary field summary; currency and pay period unspecified; nulls excluded from salary aggregates"
        elif topic == "monthly hiring":
            hire = _typed_column(table, ("join_date", "hire_date", "hired_at"), r"\bDATE|TIMESTAMP")
            if hire:
                sql = (f"SELECT DATE_FORMAT(`{hire}`, '%Y-%m') AS month, COUNT(*) AS hires FROM {quoted} "
                       f"GROUP BY DATE_FORMAT(`{hire}`, '%Y-%m') ORDER BY month")
                question = "Monthly hiring counts including missing dates; timezone unspecified"
        elif topic == "order amount summary" and name.lower() in ("orders", "order"):
            amount = _typed_column(table, ("order_total", "total_amount"), numeric_type)
            if amount:
                sql = (f"SELECT COUNT(*) AS record_count, COUNT(`{amount}`) AS amount_count, "
                       f"SUM(`{amount}`) AS order_amount_sum FROM {quoted}")
                question = "Order amount field sum; currency and business definition unspecified, not assumed revenue"
        if sql:
            output.append(ReportQuery(f"q{len(output) + 1}", question, sql))
            if len(output) == MAX_QUERIES:
                break
    if not output:
        raise ReportError("Clarify the metric, units and eligible typed columns. No safe curated query matches this topic; text salaries/dates are not silently cleaned.")
    return output


def suggest_report_topics(schema: Schema, questions: list[str] | tuple[str, ...]) -> list[str]:
    """Explicit composer suggestions only, schema-grounded; never model/DB calls."""
    topics = []
    for topic in ("counts", "workforce composition", "salary summary", "monthly hiring", "order amount summary"):
        try:
            curated_report_queries(schema, topic)
            topics.append(topic)
        except ReportError:
            pass
    context = " ".join(q[:2000].lower() for q in questions[-20:] if isinstance(q, str))
    return sorted(topics, key=lambda topic: not any(word in context for word in topic.split()))