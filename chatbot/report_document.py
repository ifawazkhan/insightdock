"""Shared, question-led client document, built locally without executing SQL.

Public API: ``build_document(BusinessReport) -> ReportDocument``. Both public
dataclasses are frozen; their collections are tuples. Section tables contain
display strings (including ``Missing``), while ``chart_result`` is a detached
QueryResult retaining unrounded numeric strings and nulls. Renderers must escape
all text. Chart callers pass ``chart_question`` and ``chart_sql`` to their normal
visualization selector; salary profiles deliberately have empty chart SQL because
their two-column projection no longer matches the original multi-measure SELECT.

Evidence, SQL, authoritative findings and input rows are never changed. No model,
database, currency, pay period, population denominator or weighted mean is added.
Percentages refer only to the sum of a proven complete returned COUNT partition,
including its missing-category bucket and any SQL WHERE filter, not to a separate
scalar total or an assumed company population.
"""

from dataclasses import dataclass, replace
from decimal import Decimal, localcontext
import re

import sqlglot
from sqlglot import exp

from chatbot.db import QueryResult
from chatbot.report_presentation import clean_report_text, display_number, human_label
from chatbot.reports import BusinessReport, ReportEvidence, aggregate_result, decimal_value
from chatbot.visualization import _sql_evidence

__all__ = ["ReportSection", "ReportDocument", "build_document"]


@dataclass(frozen=True)
class ReportSection:
    """Client table and commentary, plus independent chart-selection inputs."""

    title: str
    columns: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]
    commentary: str
    chart_result: QueryResult | None
    chart_question: str
    chart_sql: str


@dataclass(frozen=True)
class ReportDocument:
    """One presentation contract for on-screen and exported report documents."""

    title: str
    summary: str
    sections: tuple[ReportSection, ...]
    findings: tuple[str, ...]
    recommendations: tuple[str, ...]
    limitations: tuple[str, ...]
    follow_up: str


_ROLES = {"job_title", "job_role", "role"}
_MISSING = {"", "missing", "unknown", "null", "none", "n/a", "not specified"}
_SALARY_TOPIC = re.compile(r"\bsalar(?:y|ies)\b|\bpay\b|\bcompensation\b", re.I)
_ROLE_TOPIC = re.compile(r"\bjob[ -]titles?\b|\bjob[ -]roles?\b|\broles?\b", re.I)
_GUIDANCE_TOPIC = re.compile(
    r"\badvice\b|\badvise\b|\brecommend(?:ation)?s?\b|\bnext[ -]steps?\b|"
    r"\bfollow[ -]?ups?\b|\bsuggest(?:ion)?s?\b|\baction[ -]plan\b|\bwhat should\b", re.I)
_NO_GUIDANCE = re.compile(
    r"\b(?:no|without|omit|exclude|skip|do not|don't)\s+"
    r"(?:(?:include|add|give|provide|offer|any|unsolicited)\s+)*"
    r"(?:advice|recommendations?|suggestions?|next[ -]steps?|follow[ -]?ups?)\b", re.I)


def _guidance_requested(report: BusinessReport) -> bool:
    """Only the original request/title can opt into decision-oriented sections."""
    question = report.question.replace("_", " ")
    if _NO_GUIDANCE.search(question):
        return False
    return any(_GUIDANCE_TOPIC.search(_NO_GUIDANCE.sub("", value.replace("_", " ")))
               for value in (question, report.title))


def _expression(projection: exp.Expression) -> exp.Expression:
    value = projection.this if isinstance(projection, exp.Alias) else projection
    while isinstance(value, exp.Paren):
        value = value.this
    return value


def _tree(item: ReportEvidence) -> exp.Select | None:
    try:
        statements = sqlglot.parse(item.sql, read="mysql")
    except (sqlglot.errors.SqlglotError, TypeError, ValueError):
        return None
    if len(statements) != 1 or not isinstance(statements[0], exp.Select):
        return None
    return statements[0]


def _safe_result(item: ReportEvidence, tree: exp.Select) -> QueryResult | None:
    """Use the shared privacy gate, with a narrow real-job-role adapter.

    The existing gate has no job_title vocabulary. For gating ONLY, canonicalize
    direct known role columns to category, then restore their original labels.
    This is not executable SQL or chart provenance. Free-form titles, names and
    aliases of unrelated source columns never gain permission via this adapter.
    All aggregate, numeric, shape and identifying-value checks remain in the gate.
    """
    safe = aggregate_result(item)
    if safe is not None or item.result is None or item.error:
        return safe
    if len(tree.expressions) != len(item.result.columns):
        return None
    adapted = tree.copy()
    columns = list(item.result.columns)
    roles = []
    for i, projection in enumerate(adapted.expressions):
        value = _expression(projection)
        if columns[i].lower() in _ROLES:
            if not isinstance(value, exp.Column) or value.name.lower() not in _ROLES:
                return None
            roles.append(i)
            columns[i] = "category"
            if isinstance(projection, exp.Alias):
                projection.set("alias", exp.to_identifier("category"))
    if not roles or len(set(columns)) != len(columns):
        return None
    for column in adapted.find_all(exp.Column):
        if column.name.lower() in _ROLES:
            column.set("this", exp.to_identifier("category"))
    candidate = QueryResult(columns, [list(row) for row in item.result.rows],
                            0, item.result.truncated, item.result.elapsed_seconds)
    safe = aggregate_result(replace(item, sql=adapted.sql(dialect="mysql"), result=candidate))
    if safe is not None:
        safe.columns = list(item.result.columns)
    return safe


def _dimension(value: exp.Expression, name: str) -> str:
    if list(value.find_all(exp.AggFunc)):
        return ""
    sources = {column.name.lower() for column in value.find_all(exp.Column)}
    if sources & _ROLES and isinstance(value, exp.Column):
        return "role"
    if sources & {"department", "department_name"}:
        return "department"
    if sources & {"city", "status"}:
        return "city" if "city" in sources else "status"
    if name.lower() in {"month", "year", "period"}:
        return "time"
    return "category"


def _count(value: exp.Expression) -> bool:
    return isinstance(value, exp.Count) and isinstance(value.this, (exp.Star, exp.Column))


def _positive_salary(value: exp.Expression) -> bool:
    # The automatic pay plan explicitly defines valid salaries as salary > 0.
    # Match that exact CASE, not arbitrary expressions/weighted averages.
    if isinstance(value, exp.Case):
        case = value
        branches = case.args.get("ifs", [])
        if len(branches) == 1 and case.args.get("default") is None:
            branch = branches[0]
            condition, result = branch.this, branch.args.get("true")
            return (isinstance(condition, exp.GT) and isinstance(condition.this, exp.Column)
                    and condition.this.name.lower() == "salary"
                    and isinstance(condition.expression, exp.Literal) and condition.expression.this == "0"
                    and isinstance(result, exp.Column) and result == condition.this)
    return False


def _salary(value: exp.Expression) -> bool:
    return isinstance(value, exp.Avg) and (
        isinstance(value.this, exp.Column) and value.this.name.lower() == "salary"
        or _positive_salary(value.this))


def _label(name: str, value: exp.Expression) -> str:
    if _salary(value):
        return "Average salary"
    if _count(value) and isinstance(value.this, exp.Column) and value.this.name.lower() == "salary":
        return "Salary observations"
    if isinstance(value, exp.Count) and _positive_salary(value.this):
        return "Positive salary observations"
    if _dimension(value, name) == "role":
        return "Job title"
    return human_label(name)


def _known(value: object) -> bool:
    return value is not None and str(value).strip().lower() not in _MISSING


def _join(labels: list[str]) -> str:
    if len(labels) < 2:
        return "".join(labels)
    return ", ".join(labels[:-1]) + " and " + labels[-1]


def _comparison(result: QueryResult, dimension: int, measure: int, *, salary: bool) -> str:
    pairs = [(str(row[dimension]), decimal_value(row[measure])) for row in result.rows
             if _known(row[dimension])]
    pairs = [(label, number) for label, number in pairs if number is not None
             and (salary or number >= 0 and number == number.to_integral_value())]
    if not pairs:
        return "No named groups with valid values are available for comparison."
    highest = max(number for _, number in pairs)
    lowest = min(number for _, number in pairs)
    high = list(dict.fromkeys(label for label, number in pairs if number == highest))
    low = list(dict.fromkeys(label for label, number in pairs if number == lowest))
    metric = "average salary" if salary else "returned count"
    if highest == lowest:
        if len(pairs) == 1:
            return f"Among returned groups, {_join(high)} has {metric} {display_number(highest)}."
        return f"All named returned groups tie at {metric} {display_number(highest)} ({_join(high)})." + (" The spread is 0." if salary else "")
    high_text = (f"{_join(high)} tie for the highest" if len(high) > 1 else f"{high[0]} has the highest")
    if not salary:
        return f"Among returned groups, {high_text} returned count ({display_number(highest)})."
    low_text = (f"{_join(low)} tie for the lowest" if len(low) > 1 else f"{low[0]} has the lowest")
    with localcontext() as context:
        context.prec = 220
        spread = highest - lowest
    return (f"Among returned groups, {high_text} average salary ({display_number(highest)}); "
            f"{low_text} ({display_number(lowest)}). The spread between group averages is {display_number(spread)}.")


def _percentages(result: QueryResult, tree: exp.Select, sql: str,
                 dimensions: list[int], counts: list[int]) -> tuple[str, ...] | None:
    if len(counts) != 1 or not dimensions or tree.find(exp.Window):
        return None
    proof = _sql_evidence(result, sql)
    measure = counts[0]
    if not proof.complete or proof.grouped != set(dimensions) or measure not in proof.additive:
        return None
    keys = [tuple(row[i] for i in dimensions) for row in result.rows]
    if len(set(keys)) != len(keys):
        return None
    numbers = [decimal_value(row[measure]) for row in result.rows]
    if any(n is None or n < 0 or n != n.to_integral_value() for n in numbers):
        return None
    with localcontext() as context:
        context.prec = 220
        total = sum((n for n in numbers if n is not None), Decimal(0))
        if total <= 0:
            return None
        # Derived ratios can have 220 repeating digits, beyond the bounded
        # evidence parser. Quantize locally before display, never source values.
        return tuple(display_number((n / total * 100).quantize(Decimal("0.01"))) + "%"
                 for n in numbers if n is not None)


def build_document(report: BusinessReport) -> ReportDocument:
    """Derive logical sections and <=6 observations from permitted aggregates.

    Headings reflect actual projection roles, not question wording. Summaries
    answer salary questions before workforce counts, preserve partial notices,
    and never average group averages. Requested recommendations are conditional reviews,
    not conclusions about discrimination, staffing needs, turnover or causality.
    Unavailable and unsafe evidence is omitted with a limitation. Missing labels
    remain visible in tables/denominators but never win a named-group ranking.
    """
    sections: list[ReportSection] = []
    # Semantic priority: counts=0, salary averages=1, other metrics=2.
    observations: list[tuple[int, str]] = []
    extra_limits: list[str] = []
    requested = (report.question or report.title).replace("_", " ")
    salary_requested = bool(_SALARY_TOPIC.search(requested))
    accepted_roles: set[str] = set()
    has_roles = has_salary = has_counts = has_hiring = False
    has_salary_groups = False
    for item in report.evidence:
        tree = _tree(item)
        safe = _safe_result(item, tree) if tree is not None else None
        if safe is None:
            extra_limits.append("Some requested evidence is unavailable or not aggregate-safe and was omitted.")
            continue
        if not safe.rows:
            extra_limits.append("A query returned no rows; missing values are not zero.")
            continue
        expressions = [_expression(p) for p in tree.expressions]
        dimensions = [i for i, value in enumerate(expressions) if not list(value.find_all(exp.AggFunc))]
        counts = [i for i, value in enumerate(expressions) if _count(value)]
        salaries = [i for i, value in enumerate(expressions) if _salary(value)]
        roles = {_dimension(expressions[i], safe.columns[i]) for i in dimensions}
        has_roles |= "role" in roles
        if "role" in roles:
            accepted_roles.add(item.id)
        has_salary |= bool(salaries)
        has_counts |= bool(counts)
        has_salary_groups |= bool(salaries and dimensions)
        if not dimensions:
            heading = "Salary Data Quality" if any(_positive_salary(expressions[i].this) for i in salaries) else "Workforce Overview"
        elif roles == {"department"}:
            heading = "Department Salary Profile" if salaries else "Department Breakdown" if counts else "Department Overview"
        elif roles == {"role"}:
            heading = "Job Title Salary Profile" if salaries else "Job Title Breakdown"
        elif roles <= {"city", "status"}:
            heading = "City/Status Distribution"
        elif roles == {"time"} and counts:
            # Counts over arbitrary time are not necessarily hires.
            source_columns = {c.name.lower() for i in dimensions for c in expressions[i].find_all(exp.Column)}
            heading = "Hiring Trends" if source_columns & {"join_date", "hire_date", "hired_at"} else "Period Breakdown"
        else:
            heading = "Aggregate Breakdown"
        has_hiring |= heading == "Hiring Trends"
        columns = tuple(_label(name, value) for name, value in zip(safe.columns, expressions))
        rows = tuple(tuple("Missing" if not _known(v) else str(v) if i in dimensions else display_number(v)
                           for i, v in enumerate(row)) for row in safe.rows)
        percentages = _percentages(safe, tree, item.sql, dimensions, counts)
        if percentages is not None:
            columns += ("Percentage of returned total",)
            rows = tuple(row + (percentage,) for row, percentage in zip(rows, percentages))
        comments: list[str] = []
        if any(_positive_salary(expressions[i].this) for i in salaries):
            extra_limits.append("Salary averages exclude missing and nonpositive salaries; positive salary observation counts show the included records. Headcount includes all records in the returned groups.")
        if len(dimensions) == 1:
            for measure in (salaries[:1] + ([] if salary_requested and salaries else counts[:1])):
                text = _comparison(safe, dimensions[0], measure, salary=measure in salaries)
                if measure in counts and percentages is not None:
                    named = [(i, decimal_value(row[measure])) for i, row in enumerate(safe.rows)
                             if _known(row[dimensions[0]]) and decimal_value(row[measure]) is not None]
                    if named:
                        largest = max(number for _, number in named)
                        leaders = [i for i, number in named if number == largest]
                        shares = "; ".join(f"{safe.rows[i][dimensions[0]]}: {percentages[i]}" for i in leaders)
                        text += f" Share of the returned total ({shares})."
                comments.append(text)
                observations.append((1 if measure in salaries else 0, text))
        elif not dimensions:
            for i, column in enumerate(columns):
                if len(safe.rows) == 1 and (not salary_requested or not salaries or i in salaries):
                    text = f"{column}: {rows[0][i]}."
                    comments.append(text)
                    observations.append((1 if i in salaries else 0 if i in counts else 2, text))
        if not comments:
            comments.append("Returned aggregate values are shown above.")
        if percentages is not None:
            extra_limits.append(f"Percentages use only the returned {_label(safe.columns[counts[0]], expressions[counts[0]]).lower()} total, including missing categories and any query filters.")
        if any(not _known(row[i]) for row in safe.rows for i in dimensions):
            extra_limits.append("Missing categories are shown but excluded from named-group rankings.")
        if safe.truncated or tree.find(exp.Limit) or tree.find(exp.Offset) or tree.args.get("having"):
            extra_limits.append(f"{heading}: query filters, selection limits or truncation prevent full-group totals; comparisons describe returned groups only.")
        if any(row[i] is None for row in safe.rows for i in salaries):
            extra_limits.append(f"{heading}: missing average salary values are not zero and are excluded from salary comparisons.")
        if any((n := decimal_value(row[i])) is not None and (n < 0 or n != n.to_integral_value())
               for row in safe.rows for i in counts):
            extra_limits.append(f"{heading}: invalid count values are not ranked or used for percentages.")
        chart = QueryResult(list(safe.columns), [list(row) for row in safe.rows],
                            0, safe.truncated, safe.elapsed_seconds)
        chart_sql = item.sql
        chart_question = f"Compare {heading.lower()}"
        if salaries and len(dimensions) == 1:
            dimension, salary = dimensions[0], salaries[0]
            chart = QueryResult([safe.columns[dimension], "average_salary"],
                                [[row[dimension], row[salary]] for row in safe.rows],
                                0, safe.truncated, safe.elapsed_seconds)
            chart_sql = ""
            chart_question = f"Compare average salary by {columns[dimension].lower()}"
        elif heading == "Hiring Trends":
            chart_question = "Compare hiring trends over time"
        sections.append(ReportSection(heading, columns, rows, " ".join(comments),
                                      chart, chart_question, chart_sql))

    priorities = {1: 0, 0: 1, 2: 2} if salary_requested else {0: 0, 1: 1, 2: 2}
    ordered = sorted(observations, key=lambda observation: priorities[observation[0]])
    findings = tuple(dict.fromkeys(text for _, text in ordered))[:6]
    if salary_requested and not has_salary:
        summary = "No aggregate-safe salary evidence is available."
    elif findings:
        lead = "The available salary evidence shows: " if salary_requested else "The available workforce evidence shows: "
        if salary_requested:
            salary_facts = list(dict.fromkeys(text for priority, text in ordered if priority == 1))
            summary = lead + " ".join(salary_facts[:2])
        else:
            summary = lead + " ".join(findings[:2])
    else:
        summary = "No aggregate-safe numeric findings are available; missing values are not zero."
    partial = "Partial report: some requested evidence is unavailable."
    if partial in report.summary or any(e.error or e.result is None for e in report.evidence):
        summary += " " + partial
    recommendations = []
    if has_salary_groups:
        recommendations.append("If pay differences warrant investigation, review like-for-like pay groups, responsibilities and group sizes before taking action; group averages alone do not establish unfair pay or discrimination.")
    elif has_salary:
        recommendations.append("If salary decisions are planned, confirm salary definitions and request comparable grouped averages with nonmissing salary observation counts.")
    if has_counts:
        recommendations.append("If these counts inform planning, confirm category definitions, query filters and completeness before changing staffing or resource allocations.")
    if has_hiring:
        recommendations.append("If comparing hiring periods, verify comparable period coverage and date definitions before interpreting changes.")
    if not recommendations:
        recommendations.append("If decisions depend on this topic, request scoped aggregate evidence and confirm metric definitions first.")
    limits = [clean_report_text(note, report) for note in report.limitations
              if not any(note.startswith(f"[{id_}]") and "Nonaggregate or privacy-sensitive" in note for id_ in accepted_roles)]
    limits.extend(extra_limits)
    if has_salary and not any("Currency" in note for note in limits):
        limits.append("Currency and salary pay period are unspecified; no currency, period or weighted overall salary is inferred.")
    elif has_salary_groups:
        limits.append("No weighted overall salary is inferred from group averages.")
    if _ROLE_TOPIC.search(requested) and not has_roles:
        limits.append("No job-title or role evidence is available; department labels are not job titles.")
        summary += " Job-title or role comparisons are unavailable in this evidence."
    if has_salary_groups:
        follow_up = "Would you like to review the observed pay-group differences after confirming the salary definition, included observation counts and comparable responsibilities?"
    elif has_hiring:
        follow_up = "Would you like to compare the returned hiring periods after confirming their coverage and date definitions?"
    elif has_counts:
        follow_up = "Would you like to compare the observed group counts after confirming category definitions and query filters?"
    elif has_salary:
        follow_up = "Would you like a salary profile by an available grouping, with matching nonmissing salary observation counts?"
    else:
        follow_up = "Would you like to clarify the requested metrics and obtain aggregate-safe evidence?"
    if not _guidance_requested(report):
        recommendations = []
        follow_up = ""
    return ReportDocument(report.title.replace("_", " "), summary, tuple(sections), findings,
                          tuple(recommendations), tuple(dict.fromkeys(note for note in limits if note)), follow_up)