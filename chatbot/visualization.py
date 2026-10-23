"""Deterministic chart selection using actual fetched values, never invented data."""

from dataclasses import dataclass, field
from datetime import date, datetime
import math
import re

import pandas as pd
import sqlglot
from sqlglot import exp

from chatbot.db import QueryResult


@dataclass
class Visualization:
    kind: str
    title: str
    spec: dict | None = None
    value: str = ""
    note: str = ""


def numeric(values: list) -> list[float | None] | None:
    output = []
    for value in values:
        if value is None:
            output.append(None)
            continue
        if isinstance(value, bool):
            return None
        try:
            number = float(value)
        except (ValueError, TypeError, OverflowError):
            return None
        if not math.isfinite(number):
            return None
        output.append(number)
    return output if any(value is not None for value in output) else None


def _legacy_visualization(result: QueryResult, question: str = "") -> Visualization:
    if not result.rows or not result.columns:
        return Visualization("none", "")
    title = lambda name: name.replace("_", " ").title()
    numeric_columns = {
        index: numbers for index, name in enumerate(result.columns)
        if not _IDENTITY.search(name)
        and (numbers := numeric([row[index] for row in result.rows])) is not None
    }
    if len(result.rows) == 1 and len(result.columns) == 1 and numeric_columns:
        value = next(iter(numeric_columns.values()))[0]
        return Visualization("metric", title(result.columns[0]), value=f"{value:,.2f}".rstrip("0").rstrip(".")
                             if value % 1 else f"{value:,.0f}")
    if not numeric_columns:
        return Visualization("table", "Matching records")
    # A number somewhere in a record is not a reason to draw a chart. Identity,
    # contact and individual-detail lookups are clearer as concise answers/tables.
    comparison = bool(re.search(
        r"\b(chart|graph|plot|visuali[sz]e|compare|comparison|versus|vs|top|bottom|highest|lowest|"
        r"ranking|ranked|distribution|histogram|trend|monthly|yearly)\b|over time|"
        r"(?:each|per|by)\s+(?:department|category|city|month|year|team)|GROUP\s+BY|"
        r"ORDER\s+BY\s+(?:\w+\.)?(?:salary|rating|amount|total|count|avg|average|sum)\b",
        question, re.I))
    lookup = bool(re.search(r"\b(who|whose)\b|phone|email|contact|belongs to|where.*(?:from|live|based)|details (?:of|for|about)",
                            question, re.I))
    aggregate_shape = len(result.columns) <= 3 and any(re.search(
        r"(?:^|_)(?:count|avg|average|sum|total|hires)(?:$|_)|COUNT\(|AVG\(|SUM\(",
        name, re.I) for name in result.columns)
    implicit_comparison = not question.strip() and len(result.columns) == 2
    if len(result.rows) < 2 or (lookup and not comparison) or not (comparison or aggregate_shape or implicit_comparison):
        return Visualization("table", "Matching records")
    # Prefer the measure the question actually asks about; don't graph IDs or phone numbers.
    candidates = list(numeric_columns)
    requested = [i for i in candidates if any(
        word in question.lower() for word in result.columns[i].lower().split("_") if len(word) > 3
    )]
    measure = (requested or [i for i in candidates if "salary" in result.columns[i].lower()] or candidates)[0]
    values = numeric_columns[measure]
    if sum(value is not None for value in values) < 2:
        return Visualization("table", "Matching records")
    measure_name = title(result.columns[measure])
    note = f"Chart displays {len(result.rows):,} returned rows." if result.truncated else ""
    dates = []
    for i, name in enumerate(result.columns):
        if i == measure or not re.search(r"date|month|year|period", name, re.I):
            continue
        raw = [row[i] for row in result.rows]
        if all(isinstance(v, (date, datetime)) or isinstance(v, str) and re.fullmatch(r"\d{4}-\d{2}(?:-\d{2}.*)?", v) for v in raw):
            parsed = pd.to_datetime(raw, errors="coerce", format="mixed")
            if not parsed.isna().any():
                dates.append((i, [v.isoformat() for v in parsed]))
    categorical = [i for i in range(len(result.columns)) if i not in numeric_columns and i not in [d[0] for d in dates]
                   and not re.search(r"email|phone|id|date", result.columns[i], re.I)]
    label_col = next((i for i in categorical if result.columns[i].lower() == "name"),
                     categorical[0] if categorical else None)
    # Temporal charts only for genuinely time-oriented output, not incidental employee hire dates.
    temporal = bool(dates) and (len(result.columns) <= 3 or bool(re.search(r"trend|over time|by month|by year|monthly|yearly", question, re.I)))
    if temporal:
        date_index, date_values = dates[0]
        records = [{"period": d, "value": v} for d, v in zip(date_values, values) if v is not None]
        mark = {"type": "line", "point": True} if len(set(date_values)) == len(date_values) else {"type": "point", "filled": True}
        encoding = {"x": {"field": "period", "type": "temporal", "title": title(result.columns[date_index])},
                    "y": {"field": "value", "type": "quantitative", "title": measure_name, "scale": {"zero": False}},
                    "tooltip": [{"field": "period", "type": "temporal"}, {"field": "value", "type": "quantitative", "title": measure_name}]}
        kind = "line" if mark["type"] == "line" else "scatter"
    elif (label_col is not None or len(result.rows) <= 20) and not re.search(r"distribution|histogram", question, re.I):
        records = []
        labels = [str(row[label_col]) if label_col is not None and row[label_col] is not None else f"Result {i + 1}"
                  for i, row in enumerate(result.rows)]
        for i, (label, value) in enumerate(zip(labels, values)):
            if value is not None:
                records.append({"label": f"{label} · {i + 1}" if labels.count(label) > 1 else label, "value": value})
        omitted = len(records) > 30
        records = records[:30]
        if omitted:
            note += " Showing the first 30 results in query order; no aggregation applied."
        mark = {"type": "bar", "cornerRadiusEnd": 4}
        encoding = {"y": {"field": "label", "type": "nominal", "sort": None, "title": None,
                          "axis": {"labelLimit": 180}},
                    "x": {"field": "value", "type": "quantitative", "title": measure_name, "scale": {"zero": True}},
                    "tooltip": [{"field": "label", "type": "nominal", "title": "Record"},
                                {"field": "value", "type": "quantitative", "title": measure_name}]}
        kind = "bar"
    else:
        records = [{"value": v} for v in values if v is not None]
        mark = {"type": "bar", "cornerRadiusEnd": 3}
        encoding = {"x": {"field": "value", "type": "quantitative", "bin": {"maxbins": 20}, "title": measure_name},
                    "y": {"aggregate": "count", "type": "quantitative", "title": "Fetched records"},
                    "tooltip": [{"aggregate": "count", "type": "quantitative", "title": "Fetched records"}]}
        kind = "histogram"
    spec = {"data": {"values": records}, "mark": mark, "encoding": encoding,
            "height": min(420, max(180, len(records) * 30)) if kind == "bar" else 260,
            "background": "transparent", "config": {"view": {"stroke": None},
                "axis": {"domain": False},
                "font": "Segoe UI"}}
    if any(v is None for v in values):
        note += " Missing numeric values are omitted from the chart."
    return Visualization(kind, measure_name + (" distribution" if kind == "histogram" else " overview"), spec, note=note.strip())


# Eligibility is deliberately conservative. SQL is evidence, not an instruction:
# no execution, schema inspection, inference of hidden totals, or external calls.
_IDENTITY = re.compile(r"(?:^|_)(?:id|phone|year|month|zip|postal)(?:$|_)|email|identifier|contact|(?-i:Id$|ID$)", re.I)
_RATE = re.compile(r"rate|retention|conversion|approval|average|(?:^|_)avg(?:_|$)|rating|ratio", re.I)
_ADDITIVE = re.compile(r"(?:^|_)(?:count|total|sum|hires|frequency)(?:_|$)", re.I)
_SHARE = re.compile(r"\bshare(?:s)?\b|composition|proportion|breakdown|\bpercent(?:age)?s?\b|\bpie\b|\bdonut\b", re.I)
_WHOLE = re.compile(r"\bshares?\s+of\b|\bcomposition\b|\bproportions?\s+of\b|\b(?:whole|all|100%)\b|percentage distribution", re.I)
_PERCENT = re.compile(r"percent|percentage|pct|share|fraction", re.I)
_KINDS = ("bar", "vertical_bar", "pie", "donut", "grouped_bar", "stacked_bar",
          "normalized_bar", "scatter", "bubble", "line", "area", "stacked_area",
          "histogram", "boxplot", "heatmap", "waterfall", "pareto")


@dataclass
class _ShareProof:
    magnitude: exp.Expression
    scale: float
    decimals: int | None


@dataclass
class _Evidence:
    additive: set[int]
    grouped: set[int]
    shares: set[int]
    complete: bool
    raw: bool
    ordered: bool = False
    share_proofs: dict[int, _ShareProof] = field(default_factory=dict)
    companions: dict[int, set[int]] = field(default_factory=dict)


def _unparen(expression):
    while isinstance(expression, exp.Paren):
        expression = expression.this
    return expression


def _literal_number(expression):
    expression = _unparen(expression)
    if not isinstance(expression, exp.Literal) or expression.is_string:
        return None
    values = numeric([expression.this])
    return values[0] if values else None


def _additive_expression(expression):
    expression = _unparen(expression)
    if isinstance(expression, exp.Count):
        return isinstance(expression.this, (exp.Star, exp.Column)) and not _RATE.search(expression.sql())
    return (isinstance(expression, exp.Sum) and isinstance(expression.this, exp.Column)
            and not _RATE.search(expression.this.name))


def _share_proof(expression):
    """Recognize only scaled grouped magnitude / its global window total.

    Parentheses, outer ROUND, multiplication by 1/100 (on either side of
    division), and NULLIF(total, 0) do not change denominator lineage.
    No generic arithmetic simplification or alias/name-based rate inference.
    """
    expression = _unparen(expression)
    decimals = None
    if isinstance(expression, exp.Round):
        precision = expression.args.get("decimals")
        value = _literal_number(precision) if precision is not None else 0.0
        if value is None or not value.is_integer() or not 0 <= value <= 15:
            return None
        decimals = int(value)
        expression = _unparen(expression.this)

    def scaled(expression):
        expression = _unparen(expression)
        if isinstance(expression, exp.Mul):
            for literal, other in ((expression.this, expression.expression), (expression.expression, expression.this)):
                factor = _literal_number(literal)
                if factor in (1, 100):
                    return _unparen(other), factor
        return expression, 1.0

    expression, scale = scaled(expression)
    if not isinstance(expression, exp.Div):
        return None
    magnitude, numerator_scale = scaled(expression.this)
    scale *= numerator_scale
    if scale not in (1, 100) or not _additive_expression(magnitude):
        return None
    denominator = _unparen(expression.expression)
    if isinstance(denominator, exp.Nullif) and _literal_number(denominator.expression) == 0:
        denominator = _unparen(denominator.this)
    if (not isinstance(denominator, exp.Window) or not isinstance(denominator.this, exp.Sum)
            or _unparen(denominator.this.this) != magnitude
            or any(denominator.args.get(k) for k in ("partition_by", "order", "spec", "alias"))):
        return None
    return _ShareProof(magnitude, scale, decimals)


def _sql_evidence(result: QueryResult, sql: str) -> _Evidence:
    """Prove simple single-table grouping; complex lineage fails closed.

    COUNT(DISTINCT), joins, CTEs, subqueries, HAVING, rollups and unions do
    not prove an exclusive partition. LIMIT/OFFSET anywhere invalidate totals.
    A WHERE filter defines the returned population, never the whole table.
    """
    evidence = _Evidence(set(), set(), set(), not result.truncated, True)
    if not sql.strip():
        evidence.raw = not any(_ADDITIVE.search(n) or re.search(r"(?:^|_)(?:avg|average|mean|median|min|max)(?:_|$)|COUNT\(|AVG\(|SUM\(", n, re.I)
                       for n in result.columns)
        return evidence
    evidence.complete = False
    evidence.raw = False
    try:
        statements = sqlglot.parse(sql, read="mysql")
    except (sqlglot.errors.ParseError, ValueError, TypeError):
        return evidence
    if len(statements) != 1 or not isinstance(statements[0], exp.Select):
        return evidence
    tree = statements[0]
    evidence.ordered = tree.args.get("order") is not None
    complete = not result.truncated and not any(tree.find_all(exp.Limit, exp.Offset))
    if (any(tree.find_all(exp.Join, exp.Subquery, exp.CTE)) or len(list(tree.find_all(exp.Table))) != 1
            or tree.args.get("having") or tree.args.get("distinct")):
        return evidence
    evidence.raw = not any(tree.find_all(exp.AggFunc, exp.Window)) and tree.args.get("group") is None
    if len(tree.expressions) != len(result.columns):
        # SELECT * expands at execution time. The AST can prove raw observations
        # without mapping individual projections, but cannot prove pie lineage.
        if not any(isinstance(e, exp.Star) or isinstance(e, exp.Column) and e.is_star for e in tree.expressions):
            evidence.raw = False
        return evidence
    evidence.complete = complete
    group = tree.args.get("group")
    if group is None or any(group.args.get(k) for k in ("rollup", "cube", "grouping_sets")):
        return evidence
    expressions = [e.this if isinstance(e, exp.Alias) else e for e in tree.expressions]
    # Result labels must correspond to the projection, not merely its position.
    if (len({name.lower() for name in result.columns}) != len(result.columns)
            or any(e.alias_or_name.lower() != name.lower() for e, name in zip(tree.expressions, result.columns))):
        return evidence
    table = next(tree.find_all(exp.Table))
    qualifiers = {table.name.lower(), table.alias_or_name.lower()}
    if any(column.table and column.table.lower() not in qualifiers for column in tree.find_all(exp.Column)):
        return evidence

    def normalized(expression):
        # A single source table makes qualified/unqualified columns equivalent.
        expression = _unparen(expression).copy()
        for column in expression.find_all(exp.Column):
            column.set("table", None)
        return expression

    def dimension_column(expression):
        expression = _unparen(expression)
        if isinstance(expression, exp.Coalesce):
            defaults = expression.expressions
            if (len(defaults) != 1 or not isinstance(defaults[0], exp.Literal)
                    or not defaults[0].is_string):
                return None
            expression = _unparen(expression.this)
        return expression if isinstance(expression, exp.Column) and not expression.is_star else None

    dimensions = {i: e for i, e in enumerate(expressions) if dimension_column(e) is not None}
    # MySQL gives source columns precedence over SELECT aliases in GROUP BY.
    # Without schema access, conflicting observed source names fail closed.
    source_names = {column.name.lower() for e in expressions for column in e.find_all(exp.Column)}
    if tree.args.get("where"):
        source_names.update(column.name.lower() for column in tree.args["where"].find_all(exp.Column))
    resolved = set()
    for g in group.expressions:
        g = _unparen(g)
        if isinstance(g, exp.Literal) and g.is_int:
            index = int(g.this) - 1
            if index not in dimensions:
                return evidence
        else:
            if dimension_column(g) is None:
                return evidence
            aliases = [i for i, p in enumerate(tree.expressions)
                       if isinstance(g, exp.Column) and not g.table and p.alias
                       and p.alias.lower() == g.name.lower()]
            if aliases and g.name.lower() not in source_names:
                matches = [i for i in aliases if i in dimensions]
            else:
                matches = [i for i, e in dimensions.items() if normalized(e) == normalized(g)
                           or isinstance(g, exp.Column)
                           and normalized(dimension_column(e)) == normalized(g)]
                if aliases and any(i not in matches for i in aliases):
                    return evidence
            if len(matches) != 1:
                return evidence
            index = matches[0]
        if index in resolved:
            return evidence  # Repeated or overlapping grouping references are ambiguous.
        resolved.add(index)
    if not resolved:
        return evidence
    evidence.grouped = resolved

    for i, e in enumerate(expressions):
        if _additive_expression(e) and not _RATE.search(result.columns[i]):
            evidence.additive.add(i)
        proof = _share_proof(e)
        if proof is not None and not _RATE.search(result.columns[i]):
            evidence.shares.add(i)
            evidence.share_proofs[i] = proof
    for i, proof in evidence.share_proofs.items():
        evidence.companions[i] = {j for j in evidence.additive if _unparen(expressions[j]) == proof.magnitude}
    evidence.complete = complete
    return evidence


def _title(name: str) -> str:
    return name.replace("_", " ").title()


def _chart(kind, title, records, mark, encoding, note="", **extra) -> Visualization:
    spec = {"data": {"values": records}, "mark": mark, "encoding": encoding,
            "height": 260, "background": "transparent",
            "config": {"view": {"stroke": None}, "axis": {"domain": False}, "font": "Segoe UI"}}
    spec.update(extra)
    if "layer" in extra:
        spec.pop("mark")
        spec.pop("encoding")
    return Visualization(kind, title, spec, note=note.strip())


def _field(field, kind="quantitative", **kwargs):
    return {"field": field, "type": kind, **kwargs}


@dataclass
class _Context:
    result: QueryResult
    question: str
    sql: str
    legacy: Visualization
    numbers: dict[int, list[float | None]]
    dimensions: list[int]
    dates: dict[int, list[str]]
    measure: int
    evidence: _Evidence

    @property
    def note(self):
        return f"Chart displays {len(self.result.rows):,} returned rows." if self.result.truncated else ""

    def additive(self, index):
        if _RATE.search(self.result.columns[index]):
            return False
        return index in self.evidence.additive if self.sql.strip() else bool(_ADDITIVE.search(self.result.columns[index]))

    def nonnegative(self, indices):
        return all(all(v is not None and v >= 0 for v in self.numbers[i]) for i in indices)


def _context(result, question, sql, force=False):
    if (not result.rows or not result.columns or len(set(result.columns)) != len(result.columns)
            or any(len(row) != len(result.columns) for row in result.rows)):
        return None
    lookup = re.search(r"\b(who|whose)\b|phone|email|contact|belongs to|details (?:of|for|about)", question, re.I)
    intent = re.search(r"chart|graph|plot|compar|versus|\bvs\b|distribution|histogram|trend|monthly|yearly|"
                       r"top|bottom|highest|lowest|ranking|scatter|bubble|heatmap|pareto|waterfall|"
                       r"area|stacked|grouped|normalized|\bshare|pie|donut|composition|percentage|"
                       r"correlation|relationship|over time|\b(?:by|per|each)\s+\w+", question, re.I)
    if lookup and not intent:
        return None
    evidence = _sql_evidence(result, sql)
    legacy = _legacy_visualization(result, question + (" chart" if force or intent else ""))
    if legacy.kind in ("none", "table", "metric"):
        return None
    nums = {i: values for i, name in enumerate(result.columns) if not _IDENTITY.search(name)
            and (values := numeric([r[i] for r in result.rows])) is not None}
    if not nums:
        return None
    dates = {}
    for i, name in enumerate(result.columns):
        raw = [r[i] for r in result.rows]
        if re.search(r"date|month|year|period", name, re.I) and all(
                isinstance(v, (date, datetime)) or isinstance(v, str) and re.fullmatch(r"\d{4}-\d{2}(?:-\d{2}.*)?", v)
                for v in raw):
            parsed = pd.to_datetime(raw, errors="coerce", format="mixed")
            if not parsed.isna().any():
                dates[i] = [v.isoformat() for v in parsed]
    dims = [i for i, name in enumerate(result.columns) if i not in nums and i not in dates
            and not _IDENTITY.search(name) and not re.search(r"date", name, re.I)]
    candidates = list(nums)
    mentioned = [i for i in candidates if any(w in question.lower() for w in result.columns[i].lower().split("_") if len(w) > 3)]
    measure = (mentioned or [i for i in candidates if "salary" in result.columns[i].lower()] or candidates)[0]
    # Pie/percentage questions often mention the dimension before the measure.
    shares = [i for i in candidates if i in evidence.shares]
    if not shares and _SHARE.search(question):
        shares = [i for i in candidates if _PERCENT.search(result.columns[i])]
    if shares:
        measure = shares[0]
    return _Context(result, question, sql, legacy, nums, dims, dates, measure, evidence)


def _composition(c):
    if len(c.dimensions) != 1 or c.dates or not c.evidence.complete:
        return None
    dimension, measure = c.dimensions[0], c.measure
    companions = set(c.numbers) - {measure}
    if len(companions) > 1 or not companions <= c.evidence.companions.get(measure, set()):
        return None
    missing = any(r[dimension] is None for r in c.result.rows)
    labels = ["Missing" if r[dimension] is None else str(r[dimension]) for r in c.result.rows]
    if len(set(labels)) != len(labels) or not c.nonnegative([measure]):
        return None
    if any(re.fullmatch(r"total|subtotal|all|overall|grand total", str(label), re.I) for label in labels):
        return None  # A supplied whole cannot also be a slice of itself.
    values = c.numbers[measure]
    total = sum(values)
    if not math.isfinite(total) or total <= 0 or len(values) < 2 or _RATE.search(c.result.columns[measure]) or _RATE.search(c.question):
        return None
    proof = c.evidence.share_proofs.get(measure)
    percent = bool(proof or _PERCENT.search(c.result.columns[measure]))
    if percent:
        if proof:
            tolerance = len(values) * .5 * 10 ** -proof.decimals if proof.decimals is not None else proof.scale * .01
            if abs(total - proof.scale) > tolerance + 1e-9:
                return None
        elif not (abs(total - 100) <= 1 or abs(total - 1) <= .01):
            return None
    if c.sql.strip():
        if c.evidence.grouped != {dimension} or measure not in (c.evidence.shares if percent else c.evidence.additive):
            return None
    elif not (_SHARE.search(c.question) and _WHOLE.search(c.question) and (percent or c.additive(measure))):
        return None
    records = [{"label": str(label), "value": value} for label, value in zip(labels, values)]
    if companions:
        companion = next(iter(companions))
        if not c.nonnegative([companion]):
            return None
        magnitudes = c.numbers[companion]
        magnitude_total = sum(magnitudes)
        if not math.isfinite(magnitude_total) or magnitude_total <= 0:
            return None
        tolerance = .5 * 10 ** -proof.decimals if proof.decimals is not None else 1e-9
        if any(not math.isclose(value, proof.scale * (magnitude / magnitude_total), rel_tol=1e-9,
                                abs_tol=tolerance + 1e-9) for value, magnitude in zip(values, magnitudes)):
            return None
        for record, magnitude in zip(records, magnitudes):
            record["magnitude"] = magnitude
    note = "Shares of the complete returned population" + (" selected by SQL filters." if c.sql.strip() else "; denominator is the supplied whole.")
    if missing:
        note += " Null categories are labeled Missing locally; their supplied values are retained."
    if len(records) > 8:
        ordered = sorted(records, key=lambda r: -r["value"])
        other = "Other (remaining categories)" if any(r["label"] == "Other" for r in records) else "Other"
        remainder = {"label": other, "value": sum(r["value"] for r in ordered[7:])}
        if companions:
            remainder["magnitude"] = sum(r["magnitude"] for r in ordered[7:])
        records = ordered[:7] + [remainder]
        note += " Top 7 plus Other; Other sums all remaining supplied categories."
    for record in records:
        record["percent"] = 100 * (record["value"] / total)
        record["denominator"] = total
    note += " Tooltip percentages use the sum of supplied slice values; supplied rounded values are preserved."
    if percent and not companions:
        note += " Raw counts or magnitudes were not supplied and are not inferred."
    return records, note


def _series(c, temporal=False):
    """Observed long cells or wide comparable measures; never synthesize cells."""
    if temporal:
        if len(c.dates) != 1:
            return None
        dimension = next(iter(c.dates))
        categories = c.dates[dimension]
        series_dims = c.dimensions
    else:
        if not c.dimensions or c.dates:
            return None
        dimension = c.dimensions[0]
        categories = [r[dimension] for r in c.result.rows]
        series_dims = c.dimensions[1:]
    if any(v is None for v in categories) or len(series_dims) > 1:
        return None
    measures = list(c.numbers)
    records = []
    if series_dims and len(measures) == 1:
        si = series_dims[0]
        if any(r[si] is None for r in c.result.rows):
            return None
        for index, row in enumerate(c.result.rows):
            records.append({"label": str(categories[index]), "series": str(row[si]), "value": c.numbers[c.measure][index]})
    elif not series_dims and len(measures) >= 2:
        names = [c.result.columns[i].lower() for i in measures]
        # Counts share a unit; otherwise only a common explicit metric suffix
        # (e.g. domestic_sales / overseas_sales) establishes comparability.
        comparable = all(re.search(r"(?:^|_)(?:count|hires)(?:_|$)", n) for n in names) or (
            len({n.split("_")[-1] for n in names}) == 1 and all("_" in n for n in names))
        if not comparable:
            return None
        for index, category in enumerate(categories):
            for i in measures:
                records.append({"label": str(category), "series": c.result.columns[i], "value": c.numbers[i][index]})
    else:
        return None
    keys = [(r["label"], r["series"]) for r in records]
    if len(set(keys)) != len(keys) or any(r["value"] is None for r in records):
        return None
    series = list(dict.fromkeys(r["series"] for r in records))
    categories = list(dict.fromkeys(r["label"] for r in records))
    if len(series) < 2:
        return None
    note = c.note
    if temporal:
        if len(series) > 5 or len(records) > 500:
            return None
        records.sort(key=lambda r: (r["label"], r["series"]))
    else:
        if len(series) > 5 or len(categories) > 30:
            records = [r for r in records if r["series"] in series[:5] and r["label"] in categories[:30]]
            note += " Showing first 30 categories and first 5 series in query order; other observed cells omitted."
    return records, measures, note


def _build(c: _Context, kind: str) -> Visualization | None:
    name = _title(c.result.columns[c.measure])
    if kind == "bar":
        return c.legacy if c.legacy.kind == "bar" else None
    if kind == "vertical_bar":
        if c.legacy.kind != "bar":
            return None
        source = c.legacy.spec
        encoding = dict(source["encoding"])
        encoding["x"], encoding["y"] = encoding["y"], encoding["x"]
        return _chart(kind, c.legacy.title, source["data"]["values"], source["mark"], encoding, c.legacy.note)
    if kind in ("pie", "donut", "pareto"):
        composition = _composition(c)
        if composition is None:
            return None
        records, note = composition
        if kind == "pareto":
            # Pareto needs actual additive magnitudes, not rounded percentages.
            if not c.additive(c.measure) or _PERCENT.search(c.result.columns[c.measure]):
                return None
            records = sorted([{"label": "Missing" if r[c.dimensions[0]] is None else str(r[c.dimensions[0]]), "value": v}
                              for r, v in zip(c.result.rows, c.numbers[c.measure])], key=lambda r: -r["value"])
            if len(records) > 30:
                return None  # No omitted denominator and no fabricated remainder.
            total = sum(r["value"] for r in records)
            cumulative = 0
            for r in records:
                cumulative += r["value"]
                r["cumulative_pct"] = 100 * (cumulative / total)
            return _chart(kind, name + " Pareto", records, "bar", {}, note,
                          layer=[{"mark": "bar", "encoding": {"x": _field("label", "nominal", sort=None), "y": _field("value")}},
                                 {"mark": {"type": "line", "point": True}, "encoding": {"x": _field("label", "nominal", sort=None),
                                  "y": _field("cumulative_pct", scale={"domain": [0, 100]}, title="Cumulative %")}}],
                          resolve={"scale": {"y": "independent"}})
        tooltip = [_field("label", "nominal"), _field("value", title=name),
                   _field("percent", title="% of plotted total", format=".2f"),
                   _field("denominator", title="Sum of supplied slice values")]
        companions = set(c.numbers) - {c.measure}
        if companions:
            tooltip.append(_field("magnitude", title=_title(c.result.columns[next(iter(companions))])))
        return _chart(kind, name + " shares", records, {"type": "arc", "innerRadius": 60 if kind == "donut" else 0},
                      {"theta": _field("value", stack=True), "color": _field("label", "nominal"),
                       "tooltip": tooltip}, note)
    if kind in ("scatter", "bubble"):
        if kind == "scatter" and c.legacy.kind == "scatter":
            return c.legacy
        # Numeric scatter is raw paired data, not arbitrary category counts/IDs.
        if not c.evidence.raw or len(c.numbers) < 2 or c.dates:
            return None
        measures = list(c.numbers)
        if kind == "bubble":
            sized = [i for i in measures if re.search(r"\bsize\s+(?:by\s+|of\s+|showing\s+)?" + re.escape(c.result.columns[i]) + r"\b", c.question, re.I)]
            if len(measures) != 3 or len(sized) != 1 or not c.nonnegative(sized):
                return None
            size = sized[0]
            if not any(v is not None and v > 0 for v in c.numbers[size]):
                return None
            measures = [i for i in measures if i != size]
        elif len(measures) != 2:
            return None
        x, y = measures
        records = [{"x": a, "y": b, **({"size": c.numbers[size][i]} if kind == "bubble" else {})}
                   for i, (a, b) in enumerate(zip(c.numbers[x], c.numbers[y])) if a is not None and b is not None]
        if len(records) < 2:
            return None
        note = c.note + " Observed pairs only; association does not establish causation."
        if len(records) > 500:
            note += " Showing first 500 valid pairs in query order."
        if any(v is None for i in measures for v in c.numbers[i]):
            note += " Missing numeric pairs omitted."
        encoding = {"x": _field("x", title=_title(c.result.columns[x]), scale={"zero": False}),
                    "y": _field("y", title=_title(c.result.columns[y]), scale={"zero": False}),
                    "tooltip": [_field("x"), _field("y")]}
        if kind == "bubble":
            encoding["size"] = _field("size", title=_title(c.result.columns[size]), scale={"zero": True})
            encoding["tooltip"].append(_field("size"))
        return _chart(kind, "Observed relationship", records[:500], {"type": "point", "filled": True}, encoding, note)
    if kind in ("grouped_bar", "stacked_bar", "normalized_bar", "stacked_area"):
        temporal = kind == "stacked_area"
        series = _series(c, temporal)
        if series is None:
            return None
        records, measures, note = series
        if kind != "grouped_bar":
            percent = (kind == "normalized_bar" and len(measures) == 1
                       and bool(_PERCENT.search(c.result.columns[measures[0]]))
                       and not _RATE.search(c.result.columns[measures[0]]) and not _RATE.search(c.question)
                       and bool(_SHARE.search(c.question)) and bool(_WHOLE.search(c.question))
                       and not c.sql.strip())
            if not (percent or all(c.additive(i) for i in measures)) or not c.nonnegative(measures):
                return None
            if c.sql.strip() and (len(measures) != 1 or set(c.dimensions + (list(c.dates) if temporal else [])) != c.evidence.grouped):
                return None
            if not c.sql.strip() and not percent and not re.search(r"disjoint components|mutually exclusive components", c.question, re.I):
                return None  # Names alone don't prove components don't overlap.
            if any(re.fullmatch(r"total|all|subtotal|overall", r["series"], re.I) for r in records):
                return None
            # Missing components cannot be silently treated as zero. Reject
            # stacks unless every observed category has the same series set.
            sets = {}
            for r in records:
                sets.setdefault(r["label"], set()).add(r["series"])
            if len({frozenset(s) for s in sets.values()}) != 1:
                return None
            if kind == "normalized_bar" and (not c.evidence.complete or any(
                    sum(r["value"] for r in records if r["label"] == label) <= 0 for label in sets)):
                return None
            if any(not math.isfinite(sum(r["value"] for r in records if r["label"] == label)) for label in sets):
                return None
            if percent and any(not (abs(sum(r["value"] for r in records if r["label"] == label) - 100) <= 1
                                    or abs(sum(r["value"] for r in records if r["label"] == label) - 1) <= .01) for label in sets):
                return None
            if "omitted" in note:
                return None  # Dropping components would change the whole.
        encoding = {"x": _field("label", "temporal" if temporal else "nominal", sort=None),
                    "y": _field("value", stack=None if kind == "grouped_bar" else "normalize" if kind == "normalized_bar" else "zero"),
                    "color": _field("series", "nominal"), "tooltip": [_field("label", "temporal" if temporal else "nominal"), _field("series", "nominal"), _field("value")]}
        if kind == "grouped_bar":
            encoding["xOffset"] = _field("series", "nominal")
        return _chart(kind, name + " by series", records, "area" if temporal else "bar", encoding,
                      note + (" Within-group shares of observed components." if kind == "normalized_bar" else ""))
    if kind in ("line", "area"):
        if c.legacy.kind != "line" and not c.dates:
            return None
        series = _series(c, True)
        if series:
            records, measures, note = series
            if kind == "area" and (not all(c.additive(i) for i in measures) or not c.nonnegative(measures)):
                return None
            encoding = {"x": _field("label", "temporal"), "y": _field("value", stack=None, scale={"zero": kind == "area"}),
                        "color": _field("series", "nominal"), "tooltip": [_field("label", "temporal"), _field("series", "nominal"), _field("value")]}
            return _chart(kind, name + " over time", records, {"type": kind, **({"point": True} if kind == "line" else {})}, encoding,
                          note + " Observed periods only; missing periods are not filled.")
        if c.legacy.kind != "line" or c.dimensions or len(c.numbers) != 1:
            return None
        if kind == "area" and (not c.additive(c.measure) or not c.nonnegative([c.measure])):
            return None
        source = c.legacy.spec
        records = sorted(source["data"]["values"], key=lambda r: r["period"])
        return _chart(kind, c.legacy.title, records, {"type": kind, **({"point": True} if kind == "line" else {})},
                      source["encoding"], c.legacy.note + " Observed periods only; missing periods are not filled.")
    if kind == "histogram":
        if not c.evidence.raw or len(c.numbers) != 1 or c.dates:
            return None
        if c.legacy.kind == "histogram":
            return c.legacy
        records = [{"value": v} for v in c.numbers[c.measure] if v is not None]
        if len(records) < 2:
            return None
        return _chart(kind, name + " distribution", records, "bar",
                      {"x": _field("value", bin={"maxbins": 20}, title=name),
                       "y": {"aggregate": "count", "type": "quantitative", "title": "Fetched records"}},
                      c.note + " Fetched raw observations; missing numeric values omitted.")
    if kind == "boxplot":
        if not c.evidence.raw or len(c.numbers) != 1 or len(c.dimensions) > 1 or c.dates:
            return None
        records = [{"label": str(r[c.dimensions[0]]) if c.dimensions else "Fetched records", "value": v}
                   for r, v in zip(c.result.rows, c.numbers[c.measure]) if v is not None
                   and (not c.dimensions or r[c.dimensions[0]] is not None)]
        groups = set(r["label"] for r in records)
        if not groups or len(groups) > 30 or any(sum(r["label"] == g for r in records) < 5 for g in groups):
            return None
        return _chart(kind, name + " distribution", records, {"type": "boxplot", "extent": 1.5},
                      {"x": _field("label", "nominal", sort=None), "y": _field("value", title=name, scale={"zero": False})},
                      c.note + " Sample of fetched raw observations; Vega-Lite quartiles and 1.5 IQR whiskers. Null observations excluded; not aggregate means.")
    if kind == "heatmap":
        if len(c.dimensions) != 2 or len(c.numbers) != 1 or c.dates:
            return None
        x, y = c.dimensions
        records = []
        for row, v in zip(c.result.rows, c.numbers[c.measure]):
            if row[x] is None or row[y] is None or v is None:
                return None
            records.append({"x": str(row[x]), "y": str(row[y]), "value": v})
        keys = [(r["x"], r["y"]) for r in records]
        if len(set(keys)) != len(keys) or any(len(set(r[a] for r in records)) > 15 for a in ("x", "y")):
            return None
        return _chart(kind, name + " by observed cells", records, "rect",
                      {"x": _field("x", "nominal", sort=None), "y": _field("y", "nominal", sort=None),
                       "color": _field("value"), "tooltip": [_field("x", "nominal"), _field("y", "nominal"), _field("value")]},
                      c.note + " Observed cells only; absent cells remain missing. No duplicate-cell aggregation.")
    if kind == "waterfall":
        if (not re.search(r"waterfall|delta|changes", c.question, re.I)
                or not re.search(r"baseline|opening|starting", c.question, re.I)
                or not re.search(r"ordered|sequence|steps", c.question, re.I)
                or len(c.dimensions) != 1 or len(c.numbers) != 1 or c.dates or not c.evidence.complete
                or not re.search(r"delta|change", c.result.columns[c.measure], re.I)
                or _RATE.search(c.result.columns[c.measure])
                or c.sql.strip() and not (c.evidence.ordered and (c.evidence.raw or c.additive(c.measure)))):
            return None
        labels = [str(r[c.dimensions[0]]) if r[c.dimensions[0]] is not None else "" for r in c.result.rows]
        values = c.numbers[c.measure]
        if (not re.fullmatch(r"baseline|opening|start|starting", labels[0], re.I)
                or any(v is None for v in values) or not all(labels) or len(values) > 30 or len(set(labels)) != len(labels)
            or any(re.fullmatch(r"baseline|opening|start|starting", label, re.I) for label in labels[1:])
            or any(re.search(r"subtotal|balance|cumulative|grand total", label, re.I) for label in labels[1:])):
            return None
        records, running = [], 0.0
        for index, (label, value) in enumerate(zip(labels, values)):
            is_total = bool(re.fullmatch(r"total|closing|ending", label, re.I))
            if is_total:
                if index != len(values) - 1 or not math.isclose(running, value, rel_tol=1e-9, abs_tol=1e-9):
                    return None
                start, end = 0, value
            else:
                start, end = running, running + value
                running = end
            if not math.isfinite(end):
                return None
            records.append({"label": label, "start": start, "end": end, "delta": value})
        return _chart(kind, "Ordered changes from supplied baseline", records, "bar",
                      {"x": _field("label", "nominal", sort=None), "y": _field("start"), "y2": {"field": "end"},
                       "tooltip": [_field("label", "nominal"), _field("delta"), _field("start"), _field("end")]},
                      "Supplied baseline and query-order deltas; any supplied closing total is verified.")
    return None


def _registry(result, question, sql, force=False):
    context = _context(result, question, sql, force)
    if context is None:
        if (result.rows and len(set(result.columns)) == len(result.columns)
                and all(len(row) == len(result.columns) for row in result.rows)):
            legacy = _legacy_visualization(result, question)
            if legacy.kind == "metric":
                return None, {"metric": legacy}
        return None, {}
    charts = {}
    for kind in _KINDS:
        chart = _build(context, kind)
        if chart is not None:
            charts[kind] = chart
    return context, charts


def compatible_visualizations(result: QueryResult, question: str = "", *, sql: str = "") -> dict[str, Visualization]:
    """Return eligible concrete kinds only; 'auto' belongs to the caller/UI.

    Every candidate uses the same evidence checks as explicit selection. This
    function cannot execute SQL and does not mutate the result or source rows.
    """
    return _registry(result, question, sql)[1]


def choose_visualization(result: QueryResult, question: str = "", *, sql: str = "",
                         requested: str | None = None) -> Visualization:
    """Choose an eligible requested chart, otherwise a deterministic safe default.

    Existing positional callers remain supported. Requests are concrete registry
    kind names (or 'auto'); they cannot bypass completeness or semantic guards.
    """
    if any(len(row) != len(result.columns) for row in result.rows) or len(set(result.columns)) != len(result.columns):
        return Visualization("table", "Matching records", note="Ambiguous columns or malformed rows cannot be charted.")
    requested_kind = requested.lower().replace("-", "_").replace(" ", "_") if isinstance(requested, str) else "auto"
    context, charts = _registry(result, question, sql, requested_kind not in ("auto", ""))
    if requested_kind in charts:
        return charts[requested_kind]
    base = context.legacy if context else _legacy_visualization(result, question)
    selected = None
    # A clearly named explicit family is advisory, never a safety override.
    for pattern, kind in ((r"\bdonut\b", "donut"), (r"\bpie\b", "pie"), (r"pareto", "pareto"),
                          (r"waterfall", "waterfall"), (r"bubble", "bubble"), (r"scatter|versus|\bvs\b|relationship|correlation", "scatter"),
                          (r"box\s*plot|boxplot", "boxplot"), (r"heatmap", "heatmap"),
                          (r"stacked area", "stacked_area"), (r"normalized|100% stacked", "normalized_bar"),
                          (r"stacked", "stacked_bar"), (r"grouped", "grouped_bar"), (r"vertical", "vertical_bar"),
                          (r"\barea\b", "area")):
        if re.search(pattern, question, re.I) and kind in charts:
            selected = charts[kind]
            break
    if selected is None and "pie" in charts and (_SHARE.search(question) or context and (
            context.measure in context.evidence.shares or _PERCENT.search(result.columns[context.measure]))):
        selected = charts["pie"]
    if selected is None and context and context.dates:
        selected = charts.get("line") or charts.get("scatter")
    if selected is None and "grouped_bar" in charts:
        selected = charts["grouped_bar"]
    if selected is None:
        selected = charts.get(base.kind)
    if selected is None:
        # Don't expose an unvalidated legacy timeline that connects mixed
        # series, or an aggregate histogram that invents a raw distribution.
        selected = base if base.kind in ("none", "table", "metric", "bar", "scatter") else Visualization("table", "Matching records")
    if context and _SHARE.search(question) and "pie" not in charts:
        if context.nonnegative([context.measure]) and sum(context.numbers[context.measure]) == 0:
            selected = Visualization("table", "Matching records", note="Zero total cannot define shares.")
        else:
            selected = Visualization(selected.kind, selected.title, selected.spec, selected.value,
                                     (selected.note + " Shares require a complete, nonnegative, exclusive whole; rates or ambiguous/incomplete denominators are not pies.").strip())
    if requested_kind not in ("auto", "") and requested_kind not in charts:
        selected = Visualization(selected.kind, selected.title, selected.spec, selected.value,
                                 (selected.note + " Requested chart is unsupported or ineligible; using a safe fallback.").strip())
    return selected