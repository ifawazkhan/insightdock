from decimal import Decimal

import pytest

from chatbot.db import QueryResult
from chatbot.visualization import choose_visualization, compatible_visualizations, numeric


def result(columns, rows, truncated=False):
    return QueryResult(columns, rows, 0, truncated, 0.01)


def test_scalar_and_precision():
    visual = choose_visualization(result(["employee_count"], [[895]]))
    assert visual.kind == "metric" and visual.value == "895"
    assert choose_visualization(result(["avg_salary"], [[Decimal("12345.50")]])).value == "12,345.5"


def test_rankings_prefer_salary_not_id_and_preserve_ties():
    visual = choose_visualization(result(["emp_id", "name", "salary", "join_date"],
        [[1, "Alex", 100, "2020-01-01"], [2, "Alex", 100, "2021-01-01"]]), "Top highest paid employees")
    assert visual.kind == "bar"
    assert visual.title == "Salary overview"
    assert visual.spec["data"]["values"] == [{"label": "Alex · 1", "value": 100}, {"label": "Alex · 2", "value": 100}]


def test_department_aggregate():
    visual = choose_visualization(result(["department", "avg_salary"], [["IT", "100.50"], ["Sales", "80.25"]]), "Average salary per department")
    assert visual.kind == "bar"
    assert visual.spec["data"]["values"][0]["value"] == 100.5


def test_monthly_trend():
    visual = choose_visualization(result(["month", "hires"], [["2026-01", 4], ["2026-02", 8]]), "Monthly hiring trend")
    assert visual.kind == "line"
    assert visual.spec["encoding"]["x"]["type"] == "temporal"


def test_distribution():
    visual = choose_visualization(result(["salary"], [[i] for i in range(40)]), "Salary distribution")
    assert visual.kind == "histogram"
    assert visual.spec["encoding"]["x"]["bin"]


@pytest.mark.parametrize("question", [
    "Whose phone number is +91-9226916697 and where is she from?",
    "Show details for this employee",
    "Show all employees",
])
def test_record_lookups_and_plain_lists_do_not_chart(question):
    rows = [[1, "Alex", "Delhi", 100, 4.9], [2, "Pat", "Pune", 120, 4.8]]
    visual = choose_visualization(result(["emp_id", "name", "city", "salary", "rating"], rows), question)
    assert visual.kind == "table" and visual.spec is None


def test_single_record_is_not_a_salary_chart():
    visual = choose_visualization(result(["name", "city", "salary"], [["Alex", "Delhi", 100]]),
                                  "Who lives in Delhi?")
    assert visual.kind == "table"


def test_limits_nulls_and_no_fabricated_values():
    visual = choose_visualization(result(["department", "salary"], [[f"Dept {i}", i] for i in range(40)] + [["Missing", None]], True))
    assert len(visual.spec["data"]["values"]) == 30
    assert "first 30" in visual.note and "Missing" in visual.note and "additional rows" in visual.note


@pytest.mark.parametrize("columns,rows,kind", [
    (["name"], [["Alex"], ["Pat"]], "table"),
    (["emp_id"], [[1], [2]], "table"),
    (["salary"], [], "none"),
    (["salary"], [[None]], "table"),
    (["salary"], [[float("inf")]], "table"),
])
def test_safe_fallback(columns, rows, kind):
    assert choose_visualization(result(columns, rows)).kind == kind


COUNT_SQL = "SELECT department, COUNT(*) AS employee_count FROM employees GROUP BY department"
SHARES = "Show employee shares of all employees by department"


def test_api_defaults_and_registry_concrete_kinds():
    data = result(["department", "employee_count"], [["IT", 60], ["Sales", 40]])
    assert choose_visualization(data).kind == "bar"
    charts = compatible_visualizations(data, SHARES, sql=COUNT_SQL)
    assert {"bar", "vertical_bar", "pie", "donut", "pareto"} <= charts.keys()
    assert "auto" not in charts
    for kind, chart in charts.items():
        assert chart.kind == kind
        assert choose_visualization(data, SHARES, sql=COUNT_SQL, requested=kind).kind == kind
    assert choose_visualization(data, SHARES, sql=COUNT_SQL, requested="radar").kind == "pie"
    assert "fallback" in choose_visualization(data, SHARES, sql=COUNT_SQL, requested="radar").note


@pytest.mark.parametrize("values", [[60, 40], [Decimal("33.3"), Decimal("66.6")], [.4, .6]])
def test_percentage_composition_defaults_to_actual_pie(values):
    data = result(["department", "percentage"], [["IT", values[0]], ["Sales", values[1]]])
    chart = choose_visualization(data, SHARES)
    assert chart.kind == "pie" and chart.spec["mark"]["type"] == "arc"
    assert chart.spec["mark"]["innerRadius"] == 0
    assert [r["value"] for r in chart.spec["data"]["values"]] == [float(v) for v in values]
    assert choose_visualization(data, SHARES, requested="donut").spec["mark"]["innerRadius"] > 0


@pytest.mark.parametrize("name,question,values", [
    ("conversion_rate", SHARES, [60, 40]),
    ("approval_percentage", SHARES, [60, 40]),
    ("pct", "Pie of independent retention rates by department", [60, 40]),
    ("percentage", "Compare percentages by department", [60, 40]),
    ("percentage", SHARES, [20, 30]),
    ("employee_count", "Draw a pie", [60, 40]),
    ("avg_salary", SHARES, [60, 40]),
    ("total", SHARES, [-10, 110]),
    ("total", SHARES, [None, 100]),
    ("total", SHARES, [float("inf"), 100]),
    ("total", SHARES, [float("nan"), 100]),
])
def test_pies_and_compatible_alternatives_share_guards(name, question, values):
    data = result(["department", name], [["IT", values[0]], ["Sales", values[1]]])
    charts = compatible_visualizations(data, question)
    assert not {"pie", "donut", "pareto"} & charts.keys()
    assert choose_visualization(data, question, requested="pie").kind != "pie"


@pytest.mark.parametrize("sql", [
    COUNT_SQL + " LIMIT 2",
    COUNT_SQL + " LIMIT 10000 OFFSET 0",
    COUNT_SQL + " HAVING COUNT(*) > 10",
    "SELECT department, COUNT(DISTINCT employee_id) AS employee_count FROM employees GROUP BY department",
    "SELECT department, AVG(salary) AS employee_count FROM employees GROUP BY department",
    "SELECT department, COUNT(*) AS employee_count FROM employees GROUP BY department, city",
    "SELECT department, COUNT(*) AS employee_count FROM employees GROUP BY department WITH ROLLUP",
    "SELECT department, COUNT(*) AS employee_count FROM employees JOIN jobs USING(employee_id) GROUP BY department",
    "SELECT department, COUNT(*) AS employee_count FROM (SELECT * FROM employees LIMIT 2) e GROUP BY department",
    "WITH e AS (SELECT * FROM employees) SELECT department, COUNT(*) AS employee_count FROM e GROUP BY department",
    COUNT_SQL + " UNION ALL " + COUNT_SQL,
    "SELECT department, COUNT(*) AS wrong_alias FROM employees GROUP BY department",
    "SELECT department, employee_count FROM employees",
    "not sql",
    COUNT_SQL + "; " + COUNT_SQL,
])
def test_sql_unsafe_partition_or_incomplete_denominator(sql):
    data = result(["department", "employee_count"], [["IT", 60], ["Sales", 40]])
    assert not {"pie", "donut", "pareto"} & compatible_visualizations(data, SHARES, sql=sql).keys()
    assert choose_visualization(data, SHARES, sql=sql, requested="pie").kind == "bar"


@pytest.mark.parametrize("group", ["department", "1"])
def test_sql_additive_partition_and_filtered_population(group):
    sql = f"SELECT department, SUM(amount) AS total FROM sales WHERE year = 2026 GROUP BY {group}"
    data = result(["department", "total"], [["IT", 6], ["Sales", 4]])
    chart = choose_visualization(data, SHARES, sql=sql)
    assert chart.kind == "pie" and "selected by SQL filters" in chart.note


def test_sql_percentage_window_proof_not_independent_rates():
    sql = "SELECT department, COUNT(*) / SUM(COUNT(*)) OVER () * 100 AS pct FROM employees GROUP BY department"
    data = result(["department", "pct"], [["IT", 60], ["Sales", 40]])
    assert choose_visualization(data, "Compare department percentages", sql=sql).kind == "pie"
    for unsafe in [sql + " LIMIT 2", sql.replace("OVER ()", "OVER (PARTITION BY region)"),
                   sql.replace("SUM(COUNT(*)) OVER ()", "COUNT(approved)")]:
        assert "pie" not in compatible_visualizations(data, SHARES, sql=unsafe)


def test_many_slices_other_only_from_complete_source():
    data = result(["department", "employee_count"], [[f"D{i}", i + 1] for i in range(12)])
    chart = choose_visualization(data, SHARES, sql=COUNT_SQL)
    records = chart.spec["data"]["values"]
    assert len(records) == 8 and records[-1]["label"] == "Other"
    assert sum(r["value"] for r in records) == 78
    assert records[-1]["value"] == 15 and "Top 7" in chart.note
    assert "pie" not in compatible_visualizations(data, SHARES, sql=COUNT_SQL + " LIMIT 12")
    data.truncated = True
    assert "pie" not in compatible_visualizations(data, SHARES, sql=COUNT_SQL)


@pytest.mark.parametrize("rows", [ [["IT", 0], ["Sales", 0]], [["IT", 5], ["IT", 5]] ])
def test_zero_and_duplicate_compositions(rows):
    data = result(["department", "employee_count"], rows)
    assert "pie" not in compatible_visualizations(data, SHARES, sql=COUNT_SQL)
    if all(r[1] == 0 for r in rows):
        assert choose_visualization(data, SHARES).kind == "table"


def test_single_percentage_is_metric_not_slice():
    data = result(["pct"], [[100]])
    assert choose_visualization(data, SHARES, requested="pie").kind == "metric"
    assert set(compatible_visualizations(data, SHARES)) == {"metric"}


def test_vertical_bars_keep_query_order_and_values():
    data = result(["department", "avg_salary"], [["Z", 10], ["A", 20]])
    chart = choose_visualization(data, "Compare salary by department", requested="vertical_bar")
    assert chart.kind == "vertical_bar"
    assert chart.spec["encoding"]["x"]["field"] == "label"
    assert chart.spec["encoding"]["x"]["sort"] is None
    assert chart.spec["data"]["values"] == [{"label": "Z", "value": 10}, {"label": "A", "value": 20}]


def test_numeric_scatter_ignores_ids_and_uses_real_pairs():
    data = result(["emp_id", "salary", "rating"], [[1, 100, 4], [2, 200, 5], [3, None, 3]])
    chart = choose_visualization(data, "Salary versus rating")
    assert chart.kind == "scatter"
    assert chart.spec["encoding"]["x"]["type"] == "quantitative"
    assert chart.spec["data"]["values"] == [{"x": 100, "y": 4}, {"x": 200, "y": 5}]
    assert "causation" in chart.note and "Missing" in chart.note


@pytest.mark.parametrize("identity", ["employee_id", "empId", "employeeID", "phone", "postal_code", "year"])
def test_identity_not_scatter_axis(identity):
    data = result([identity, "salary"], [[1, 100], [2, 200]])
    assert "scatter" not in compatible_visualizations(data, "Scatter salary versus employee identity")
    assert choose_visualization(data, "Scatter", requested="scatter").kind != "scatter"


def test_bubble_requires_explicit_nonnegative_third_measure():
    data = result(["salary", "rating", "tenure"], [[100, 4, 2], [200, 5, 3]])
    assert "bubble" not in compatible_visualizations(data, "Bubble salary versus rating")
    chart = choose_visualization(data, "Bubble salary versus rating with size tenure")
    assert chart.kind == "bubble" and chart.spec["encoding"]["size"]["field"] == "size"
    assert chart.spec["data"]["values"][0]["size"] == 2
    for bad in (-1, None, float("inf"), 0):
        invalid = result(data.columns, [[100, 4, bad], [200, 5, bad]])
        assert "bubble" not in compatible_visualizations(invalid, "Bubble with size tenure")


def test_numeric_scatter_bounds_and_aggregate_rejection():
    data = result(["salary", "rating"], [[i, i / 100] for i in range(510)], True)
    chart = choose_visualization(data, "Scatter salary versus rating")
    assert len(chart.spec["data"]["values"]) == 500
    assert "500" in chart.note and "additional rows" in chart.note
    sql = "SELECT AVG(salary) AS salary, AVG(rating) AS rating FROM employees GROUP BY department"
    assert "scatter" not in compatible_visualizations(data, "Compare salary versus rating", sql=sql)


SERIES_SQL = "SELECT department, category, COUNT(*) AS employee_count FROM employees GROUP BY department, category"
SERIES_ROWS = [["IT", "A", 6], ["IT", "B", 4], ["Sales", "A", 3], ["Sales", "B", 7]]


@pytest.mark.parametrize("kind", ["grouped_bar", "stacked_bar", "normalized_bar", "heatmap"])
def test_observed_long_series_families(kind):
    data = result(["department", "category", "employee_count"], SERIES_ROWS)
    chart = choose_visualization(data, "Compare employee counts by department and category", sql=SERIES_SQL, requested=kind)
    assert chart.kind == kind
    assert len(chart.spec["data"]["values"]) == 4
    if kind == "normalized_bar":
        assert chart.spec["encoding"]["y"]["stack"] == "normalize"


def test_stacks_reject_averages_rates_overlapping_unproven_components():
    for name in ("avg_salary", "approval_rate", "employee_count"):
        data = result(["department", "category", name], SERIES_ROWS)
        charts = compatible_visualizations(data, "Compare grouped components")
        assert "grouped_bar" in charts
        assert not {"stacked_bar", "normalized_bar"} & charts.keys()
    data = result(["department", "category", "employee_count"], SERIES_ROWS)
    charts = compatible_visualizations(data, "Compare disjoint components")
    assert {"stacked_bar", "normalized_bar"} <= charts.keys()


def test_normalized_supplied_percentages_need_valid_within_group_wholes():
    data = result(["department", "category", "pct"], [["IT", "A", 40], ["IT", "B", 60], ["Sales", "A", .3], ["Sales", "B", .7]])
    question = "Normalized shares of all employees within department"
    assert choose_visualization(data, question, requested="normalized_bar").kind == "normalized_bar"
    assert "stacked_bar" not in compatible_visualizations(data, question)
    data.rows[-1][-1] = .2
    assert "normalized_bar" not in compatible_visualizations(data, question)


def test_missing_long_cells_never_become_zeroes():
    data = result(["department", "category", "employee_count"], SERIES_ROWS[:-1])
    charts = compatible_visualizations(data, "Compare grouped cells", sql=SERIES_SQL)
    assert len(charts["heatmap"].spec["data"]["values"]) == 3
    assert len(charts["grouped_bar"].spec["data"]["values"]) == 3
    assert "stacked_bar" not in charts and "normalized_bar" not in charts
    duplicate = result(data.columns, SERIES_ROWS + [SERIES_ROWS[0]])
    assert "heatmap" not in compatible_visualizations(duplicate, "Heatmap", sql=SERIES_SQL)


def test_wide_grouped_requires_comparable_units():
    data = result(["department", "junior_count", "senior_count"], [["IT", 2, 3], ["Sales", 4, 5]])
    assert choose_visualization(data, "Compare grouped employee counts").kind == "grouped_bar"
    mixed = result(["department", "salary", "rating"], [["IT", 100, 4], ["Sales", 200, 5]])
    assert "grouped_bar" not in compatible_visualizations(mixed, "Compare grouped values")


def test_grouped_display_bounds_and_normalized_limit_safety():
    rows = [[f"D{i}", f"S{j}", 1] for i in range(32) for j in range(6)]
    data = result(["department", "category", "employee_count"], rows)
    charts = compatible_visualizations(data, "Compare groups", sql=SERIES_SQL)
    assert len(charts["grouped_bar"].spec["data"]["values"]) == 150
    assert "omitted" in charts["grouped_bar"].note
    assert "normalized_bar" not in charts
    small = result(data.columns, SERIES_ROWS)
    assert "normalized_bar" not in compatible_visualizations(small, "Compare groups", sql=SERIES_SQL + " LIMIT 4")


TIME_SQL = "SELECT month, category, COUNT(*) AS hires FROM employees GROUP BY month, category"
TIME_ROWS = [["2026-03", "A", 3], ["2026-01", "A", 1], ["2026-03", "B", 4], ["2026-01", "B", 2]]


@pytest.mark.parametrize("kind", ["line", "area", "stacked_area"])
def test_multi_temporal_series_sorted_without_missing_months(kind):
    data = result(["month", "category", "hires"], TIME_ROWS)
    chart = choose_visualization(data, "Monthly hiring trend", sql=TIME_SQL, requested=kind)
    assert chart.kind == kind
    assert len(chart.spec["data"]["values"]) == 4
    assert chart.spec["encoding"]["x"]["type"] == "temporal"
    assert "color" in chart.spec["encoding"]
    assert all("2026-02" not in r["label"] for r in chart.spec["data"]["values"])
    assert chart.spec["data"]["values"][0]["label"].startswith("2026-01")
    assert data.rows == TIME_ROWS


def test_simple_temporal_sort_and_repeated_date_scatter():
    data = result(["month", "hires"], [["2026-03", 3], ["2026-01", 1]])
    chart = choose_visualization(data, "Monthly trend")
    assert chart.kind == "line" and chart.spec["data"]["values"][0]["period"].startswith("2026-01")
    repeated = result(data.columns, [["2026-01", 3], ["2026-01", 1]])
    assert choose_visualization(repeated, "Monthly trend").kind == "scatter"
    assert "line" not in compatible_visualizations(repeated, "Monthly trend")


def test_areas_reject_negative_averages_and_missing_components():
    data = result(["month", "avg_salary"], [["2026-01", 100], ["2026-03", 200]])
    assert "area" not in compatible_visualizations(data, "Monthly trend")
    negative = result(["month", "hires"], [["2026-01", -1], ["2026-03", 2]])
    assert "area" not in compatible_visualizations(negative, "Monthly trend")
    sparse = result(["month", "category", "hires"], TIME_ROWS[:-1])
    charts = compatible_visualizations(sparse, "Monthly trend", sql=TIME_SQL)
    assert "line" in charts and "stacked_area" not in charts


def test_raw_grouped_boxplot_and_small_explicit_histogram():
    data = result(["department", "salary"], [[d, i] for d in ("IT", "Sales") for i in range(5)] + [["IT", None]])
    chart = choose_visualization(data, "Salary boxplot by department")
    assert chart.kind == "boxplot" and chart.spec["mark"]["extent"] == 1.5
    assert len(chart.spec["data"]["values"]) == 10 and "quartiles" in chart.note
    small = result(["salary"], [[1], [2], [3]])
    assert choose_visualization(small, "Salary chart", requested="histogram").kind == "histogram"
    assert "boxplot" not in compatible_visualizations(small, "Salary distribution")


@pytest.mark.parametrize("name", ["avg_salary", "mean_salary", "salary_count", "median_salary"])
def test_boxplot_not_aggregate_values(name):
    data = result(["department", name], [["IT", i] for i in range(10)])
    charts = compatible_visualizations(data, "Distribution boxplot by department")
    assert "boxplot" not in charts and "histogram" not in charts


def test_sql_aggregate_alias_cannot_disguise_raw_distribution():
    data = result(["department", "salary"], [["IT", i] for i in range(10)])
    sql = "SELECT department, AVG(salary) AS salary FROM employees GROUP BY department"
    charts = compatible_visualizations(data, "Salary distribution boxplot", sql=sql)
    assert not {"boxplot", "histogram"} & charts.keys()


def test_heatmap_bounds_and_null_cells():
    columns = ["department", "category", "total"]
    assert "heatmap" not in compatible_visualizations(result(columns, [[f"D{i}", "A", i] for i in range(16)]), "Heatmap")
    for row in ([None, "A", 1], ["IT", None, 1], ["IT", "A", None]):
        assert "heatmap" not in compatible_visualizations(result(columns, [row, ["Sales", "B", 2]]), "Heatmap")


WATERFALL_QUESTION = "Waterfall ordered delta steps from supplied baseline"


def test_every_chart_family_renders_to_static_png():
    renderer = pytest.importorskip("vl_convert")
    cases = [
        (result(["department", "employee_count"], [["IT", 6], ["Sales", 4]]), SHARES, COUNT_SQL),
        (result(["department", "category", "employee_count"], SERIES_ROWS), "Compare employee counts", SERIES_SQL),
        (result(["month", "category", "hires"], TIME_ROWS), "Monthly hiring trend", TIME_SQL),
        (result(["salary", "rating", "tenure"], [[100, 4, 2], [200, 5, 3]]), "Bubble salary versus rating with size tenure", ""),
        (result(["department", "salary"], [[d, i] for d in ("IT", "Sales") for i in range(5)]), "Salary boxplot by department", ""),
        (result(["salary"], [[i] for i in range(10)]), "Salary histogram", ""),
        (result(["step", "delta"], [["Baseline", 100], ["Gain", 20], ["Loss", -5], ["Total", 115]]), WATERFALL_QUESTION, ""),
    ]
    rendered = set()
    for data, question, sql in cases:
        for kind, chart in compatible_visualizations(data, question, sql=sql).items():
            if kind in rendered or not chart.spec:
                continue
            image = renderer.vegalite_to_png(chart.spec)
            assert image.startswith(b"\x89PNG"), kind
            assert len(image) > 1000, kind
            rendered.add(kind)
    assert {"bar", "vertical_bar", "pie", "donut", "grouped_bar", "stacked_bar", "normalized_bar",
            "scatter", "bubble", "line", "area", "stacked_area", "histogram", "boxplot", "heatmap",
            "waterfall", "pareto"} <= rendered


def test_waterfall_supplied_baseline_order_and_verified_total():
    data = result(["step", "delta"], [["Baseline", 100], ["Gain", 20], ["Loss", -5], ["Total", 115]])
    chart = choose_visualization(data, WATERFALL_QUESTION)
    assert chart.kind == "waterfall"
    assert chart.spec["data"]["values"] == [
        {"label": "Baseline", "start": 0, "end": 100, "delta": 100},
        {"label": "Gain", "start": 100, "end": 120, "delta": 20},
        {"label": "Loss", "start": 120, "end": 115, "delta": -5},
        {"label": "Total", "start": 0, "end": 115, "delta": 115},
    ]


def test_waterfall_sql_requires_explicit_order_and_complete_result():
    data = result(["step", "delta"], [["Baseline", 100], ["Gain", 20]])
    sql = "SELECT step, delta FROM changes"
    assert "waterfall" not in compatible_visualizations(data, WATERFALL_QUESTION, sql=sql)
    assert choose_visualization(data, WATERFALL_QUESTION, sql=sql + " ORDER BY sequence").kind == "waterfall"
    assert "waterfall" not in compatible_visualizations(data, WATERFALL_QUESTION, sql=sql + " ORDER BY sequence LIMIT 2")


@pytest.mark.parametrize("rows,question", [
    ([["Gain", 20], ["Loss", -5]], WATERFALL_QUESTION),
    ([["Baseline", 100], ["Total", 999]], WATERFALL_QUESTION),
    ([["Baseline", 100], ["Gain", None]], WATERFALL_QUESTION),
    ([["Baseline", 100], ["Gain", 20]], "Waterfall"),
    ([["Baseline", 100], ["Gain", 20]], "Waterfall baseline changes"),
    ([["Baseline", 100], ["Subtotal", 100], ["Gain", 20]], WATERFALL_QUESTION),
])
def test_waterfall_ambiguous_or_inconsistent_falls_back(rows, question):
    data = result(["step", "delta"], rows)
    assert "waterfall" not in compatible_visualizations(data, question)
    assert choose_visualization(data, question, requested="waterfall").kind != "waterfall"


def test_pareto_complete_additive_categories_not_average_or_limited():
    data = result(["department", "employee_count"], [["A", 10], ["B", 30], ["C", 60]])
    chart = choose_visualization(data, "Pareto employee counts", sql=COUNT_SQL)
    assert chart.kind == "pareto" and "layer" in chart.spec
    assert "mark" not in chart.spec
    assert [r["value"] for r in chart.spec["data"]["values"]] == [60, 30, 10]
    assert [r["cumulative_pct"] for r in chart.spec["data"]["values"]] == [60, 90, 100]
    assert "pareto" not in compatible_visualizations(data, "Pareto", sql=COUNT_SQL + " LIMIT 3")


def test_plain_lookups_have_no_compatible_chart_menu():
    data = result(["emp_id", "name", "salary", "rating"], [[1, "Alex", 100, 4], [2, "Pat", 200, 5]])
    for question in ("Show all employees", "Show details for employees", "Who earns 100?"):
        assert compatible_visualizations(data, question) == {}
        assert choose_visualization(data, question).kind == "table"


def test_invalid_rows_duplicate_columns_finite_and_bool_guards():
    for data in (result(["salary", "salary"], [[1, 2], [3, 4]]), result(["salary"], [[1, 2], [3]])):
        assert choose_visualization(data, "Chart").kind == "table"
        assert compatible_visualizations(data, "Chart") == {}
    assert numeric([True, 1]) is None
    assert numeric([10 ** 1000]) is None
    assert numeric([float("nan")]) is None


def test_totals_are_not_slices_or_stack_components_and_overflows_fail_closed():
    data = result(["department", "employee_count"], [["IT", 50], ["Total", 100]])
    assert "pie" not in compatible_visualizations(data, SHARES, sql=COUNT_SQL)
    overflowing = result(["department", "employee_count"], [["IT", 1e308], ["Sales", 1e308]])
    assert "pie" not in compatible_visualizations(overflowing, SHARES, sql=COUNT_SQL)
    stack = result(["department", "category", "employee_count"], [["IT", "A", 50], ["IT", "Total", 100]])
    assert "stacked_bar" not in compatible_visualizations(stack, "Compare groups", sql=SERIES_SQL)


def test_expanded_charts_are_theme_neutral_and_do_not_mutate_source():
    data = result(["department", "employee_count"], [["IT", Decimal("60.01")], ["Sales", Decimal("39.99")]])
    original = [list(row) for row in data.rows]
    for chart in compatible_visualizations(data, SHARES, sql=COUNT_SQL).values():
        assert "color" not in chart.spec.get("mark", {})
        assert "labelColor" not in chart.spec["config"]["axis"]
        assert "scheme" not in str(chart.spec) and "#" not in str(chart.spec)
    assert data.rows == original and isinstance(data.rows[0][1], Decimal)


REAL_PERCENT_SQL = (
    "SELECT department, ROUND(COUNT(*) * 100.0 / NULLIF(SUM(COUNT(*)) OVER (), 0), 2) "
    "AS percentage FROM employees GROUP BY department"
)


@pytest.mark.parametrize("projection,group", [
    ("COALESCE(department, 'Missing') AS department", "department"),
    ("COALESCE(department, 'Missing') AS department_label", "department_label"),
    ("COALESCE(department, 'Missing') AS department_label", "department"),
    ("COALESCE(department, 'Missing') AS department_label", "COALESCE(department, 'Missing')"),
    ("COALESCE(department, 'Missing') AS department_label", "1"),
    ("(COALESCE(department, 'Missing')) AS department_label", "(COALESCE(department, 'Missing'))"),
    ("e.department AS department_label", "e.department"),
    ("e.department AS department_label", "department_label"),
    ("COALESCE(e.department, 'Missing') AS department", "e.department"),
    ("COALESCE(e.department, 'Missing') AS department", "department"),
])
def test_percentage_coalesce_dimensions_and_safe_group_references(projection, group):
    name = projection.rsplit(" AS ", 1)[1]
    sql = REAL_PERCENT_SQL.replace("SELECT department,", f"SELECT {projection},").replace(
        "FROM employees GROUP BY department", f"FROM employees e GROUP BY {group}")
    data = result([name, "percentage"], [["Missing", Decimal("40.00")], ["IT", Decimal("60.00")]])
    original = [list(row) for row in data.rows]
    charts = compatible_visualizations(data, "percentages by department", sql=sql)
    assert {"pie", "donut"} <= charts.keys()
    chart = choose_visualization(data, "percentages by department", sql=sql)
    assert chart.kind == "pie"
    assert [(r["label"], r["value"]) for r in chart.spec["data"]["values"]] == [("Missing", 40), ("IT", 60)]
    assert data.rows == original


def test_plain_percentage_intent_selects_pie_only_for_proven_whole():
    data = result(["department", "employee_count"], [["IT", 60], ["Sales", 40]])
    assert choose_visualization(data, "percentages by department", sql=COUNT_SQL).kind == "pie"
    for sql in ("", COUNT_SQL + " LIMIT 2", COUNT_SQL.replace("COUNT(*)", "AVG(salary)")):
        assert choose_visualization(data, "percentages by department", sql=sql).kind != "pie"


@pytest.mark.parametrize("sql,name,values", [(COUNT_SQL, "employee_count", [40, 60]),
                                          (REAL_PERCENT_SQL, "percentage", [40, 60]),
                                          ("", "percentage", [40, 60])])
def test_null_category_is_local_missing_slice_without_dropping(sql, name, values):
    data = result(["department", name], [[None, values[0]], ["IT", values[1]]])
    original = [list(row) for row in data.rows]
    charts = compatible_visualizations(data, SHARES, sql=sql)
    for kind in ("pie", "donut") + (("pareto",) if name == "employee_count" else ()):
        records = charts[kind].spec["data"]["values"]
        assert {r["label"]: r["value"] for r in records} == {"Missing": 40, "IT": 60}
        assert "Missing" in charts[kind].note
    assert data.rows == original and data.rows[0][0] is None


@pytest.mark.parametrize("rows", [
    [[None, 40], ["Missing", 60]], [[None, 40], [None, 60]],
    [["IT", 40], ["IT", 60]], [[None, None], ["IT", 100]],
])
def test_missing_label_collisions_duplicates_and_missing_values_fail_closed(rows):
    data = result(["department", "percentage"], rows)
    assert not {"pie", "donut"} & compatible_visualizations(data, SHARES, sql=REAL_PERCENT_SQL).keys()


@pytest.mark.parametrize("projection,group", [
    ("COALESCE(department, city) AS department_label", "department_label"),
    ("COALESCE(department, 0) AS department_label", "department_label"),
    ("COALESCE(department, RAND()) AS department_label", "department_label"),
    ("COALESCE(department, 'Missing', 'Unknown') AS department_label", "1"),
    ("COALESCE(LOWER(department), 'Missing') AS department_label", "1"),
    ("LOWER(department) AS department_label", "department_label"),
    ("COALESCE(department, 'Missing') AS department_label", "department, city"),
    ("COALESCE(department, 'Missing') AS department_label", "department, department_label"),
    ("COALESCE(department, 'Missing') AS department_label", "COALESCE(department, 'Unknown')"),
    ("COALESCE(department, 'Missing') AS department_label", "e.department_label"),
    ("e.department AS department_label", "other.department_label"),
    ("COALESCE(city, 'Missing') AS department", "department"),
    ("COALESCE(department, 'Missing') AS department_label", "RAND()"),
])
def test_group_expression_or_alias_ambiguity_fails_closed(projection, group):
    name = projection.rsplit(" AS ", 1)[1]
    sql = REAL_PERCENT_SQL.replace("SELECT department,", f"SELECT {projection},").replace(
        "FROM employees GROUP BY department", f"FROM employees e GROUP BY {group}")
    # The conflicting source name makes alias resolution ambiguous.
    if projection == "COALESCE(city, 'Missing') AS department":
        sql = sql.replace("COUNT(*)", "COUNT(department)")
    data = result([name, "percentage"], [["IT", 60], ["Sales", 40]])
    assert not {"pie", "donut"} & compatible_visualizations(data, SHARES, sql=sql).keys()


@pytest.mark.parametrize("suffix,truncated", [(" LIMIT 2", False), (" LIMIT 10 OFFSET 0", False), ("", True)])
def test_coalesce_missing_share_still_requires_complete_population(suffix, truncated):
    sql = REAL_PERCENT_SQL.replace("SELECT department,", "SELECT COALESCE(department, 'Missing') AS department,")
    data = result(["department", "percentage"], [["Missing", 40], ["IT", 60]], truncated)
    assert "pie" not in compatible_visualizations(data, "percentages by department", sql=sql + suffix)


@pytest.mark.parametrize("unsafe", [
    "COUNT(approved)", "SUM(COUNT(approved)) OVER ()", "SUM(COUNT(*)) OVER (PARTITION BY region)",
])
def test_coalesce_does_not_prove_independent_percentage_denominators(unsafe):
    sql = REAL_PERCENT_SQL.replace("SELECT department,", "SELECT COALESCE(department, 'Missing') AS department,")
    sql = sql.replace("SUM(COUNT(*)) OVER ()", unsafe)
    data = result(["department", "percentage"], [["Missing", 60], ["IT", 40]])
    assert "pie" not in compatible_visualizations(data, "percentages by department", sql=sql)


def test_coalesce_rate_intent_and_labels_still_fail_closed():
    sql = REAL_PERCENT_SQL.replace("SELECT department,", "SELECT COALESCE(department, 'Missing') AS department,")
    data = result(["department", "percentage"], [["Missing", 60], ["IT", 40]])
    assert "pie" not in compatible_visualizations(data, "Independent retention percentages by department", sql=sql)
    data.columns[-1] = "approval_percentage"
    assert "pie" not in compatible_visualizations(data, SHARES, sql=sql.replace("AS percentage", "AS approval_percentage"))


def test_missing_category_remainder_preserves_complete_denominator():
    data = result(["department", "employee_count"], [[None, 1]] + [[f"D{i}", i + 2] for i in range(9)])
    chart = choose_visualization(data, "percentages by department", sql=COUNT_SQL)
    assert chart.kind == "pie"
    records = chart.spec["data"]["values"]
    assert sum(r["value"] for r in records) == 55
    assert records[-1]["label"] == "Other" and records[-1]["value"] == 6
    assert all(r["denominator"] == 55 for r in records)
    assert "Missing" in chart.note and data.rows[0][0] is None


@pytest.mark.parametrize("expression", [
    "ROUND(COUNT(*) * 100.0 / NULLIF(SUM(COUNT(*)) OVER (), 0), 2)",
    "ROUND(100.0 * COUNT(*) / NULLIF((SUM(COUNT(*)) OVER ()), 0.0), 2)",
    "ROUND((COUNT(*) / NULLIF(SUM(COUNT(*)) OVER (), 0)) * 100.00, 2)",
    "ROUND(100 * (COUNT(*) / SUM(COUNT(*)) OVER ()), 2)",
    "(COUNT(*) * 100 / (SUM(COUNT(*)) OVER ()))",
])
def test_real_world_percentage_sql_proves_pie(expression):
    sql = f"SELECT department, {expression} AS percentage FROM employees GROUP BY department"
    data = result(["department", "percentage"], [["IT", Decimal("66.67")], ["Sales", Decimal("33.33")]])
    original = [list(row) for row in data.rows]
    charts = compatible_visualizations(data, "Department percentages", sql=sql)
    assert {"pie", "donut"} <= charts.keys()
    chart = choose_visualization(data, "Department percentages", sql=sql)
    assert chart.kind == "pie"
    records = chart.spec["data"]["values"]
    assert [r["value"] for r in records] == [66.67, 33.33]
    assert [r["percent"] for r in records] == pytest.approx([66.67, 33.33])
    assert all(r["denominator"] == 100 for r in records)
    assert not any("magnitude" in r for r in records)
    assert "not inferred" in chart.note
    assert data.rows == original


@pytest.mark.parametrize("expression,values", [
    ("COUNT(*) / NULLIF(SUM(COUNT(*)) OVER (), 0)", [.4, .6]),
    ("ROUND(COUNT(*) * 100 / SUM(COUNT(*)) OVER ())", [33, 33, 33]),
    ("ROUND(COUNT(*) * 100 / SUM(COUNT(*)) OVER (), 0)", [17] * 6),
])
def test_proven_fraction_and_rounding_keep_supplied_values(expression, values):
    sql = f"SELECT department, {expression} AS share FROM employees GROUP BY department"
    chart = choose_visualization(result(["department", "share"], [[f"D{i}", v] for i, v in enumerate(values)]),
                                 "Department composition", sql=sql)
    assert chart.kind == "pie"
    records = chart.spec["data"]["values"]
    assert [r["value"] for r in records] == values
    assert sum(r["percent"] for r in records) == pytest.approx(100)


@pytest.mark.parametrize("sql", [
    REAL_PERCENT_SQL + " LIMIT 2",
    REAL_PERCENT_SQL + " HAVING COUNT(*) > 0",
    REAL_PERCENT_SQL.replace("OVER ()", "OVER (PARTITION BY region)"),
    REAL_PERCENT_SQL.replace("OVER ()", "OVER (ORDER BY department)"),
    REAL_PERCENT_SQL.replace("OVER ()", "OVER w") + " WINDOW w AS (PARTITION BY region)",
    REAL_PERCENT_SQL.replace("SUM(COUNT(*)) OVER ()", "COUNT(approved)"),
    REAL_PERCENT_SQL.replace("SUM(COUNT(*)) OVER ()", "SUM(COUNT(approved)) OVER ()"),
    REAL_PERCENT_SQL.replace("NULLIF(SUM(COUNT(*)) OVER (), 0)", "(SUM(COUNT(*)) OVER () + 1)"),
    REAL_PERCENT_SQL.replace("100.0", "50.0"),
    REAL_PERCENT_SQL.replace("100.0", "'100'"),
    REAL_PERCENT_SQL.replace("COUNT(*) * 100.0", "AVG(salary) * 100.0"),
    REAL_PERCENT_SQL.replace("COUNT(*)", "COUNT(DISTINCT employee_id)"),
    REAL_PERCENT_SQL.replace("percentage", "approval_percentage"),
    REAL_PERCENT_SQL.replace(", 2) AS", ", -1) AS"),
])
def test_wrapped_share_sql_does_not_relax_independent_rate_guards(sql):
    data = result(["department", "approval_percentage" if "approval_percentage" in sql else "percentage"],
                  [["IT", 60], ["Sales", 40]])
    assert "pie" not in compatible_visualizations(data, SHARES, sql=sql)
    assert choose_visualization(data, SHARES, sql=sql, requested="pie").kind != "pie"


def test_percentage_and_matching_raw_count_companion():
    sql = REAL_PERCENT_SQL.replace("SELECT department,", "SELECT department, COUNT(*) AS employee_count,")
    data = result(["department", "employee_count", "percentage"], [["IT", 2, 66.67], ["Sales", 1, 33.33]])
    chart = choose_visualization(data, "Department percentages", sql=sql)
    assert chart.kind == "pie"
    records = chart.spec["data"]["values"]
    assert [r["value"] for r in records] == [66.67, 33.33]
    assert [r["magnitude"] for r in records] == [2, 1]
    tooltips = {t["field"]: t for t in chart.spec["encoding"]["tooltip"]}
    assert tooltips["magnitude"]["title"] == "Employee Count"
    assert tooltips["percent"]["title"] == "% of plotted total"
    for bad_rows in ([["IT", 1, 66.67], ["Sales", 2, 33.33]],
                     [["IT", None, 66.67], ["Sales", 1, 33.33]],
                     [["IT", -2, 66.67], ["Sales", 1, 33.33]]):
        assert "pie" not in compatible_visualizations(result(data.columns, bad_rows), SHARES, sql=sql)
    unrelated = sql.replace("COUNT(*) AS employee_count", "SUM(salary) AS employee_count")
    assert "pie" not in compatible_visualizations(data, SHARES, sql=unrelated)
    assert "pie" not in compatible_visualizations(data, SHARES)


def test_additive_pie_tooltips_show_actual_magnitude_and_denominator():
    data = result(["department", "employee_count"], [["IT", 2], ["Sales", 1]])
    chart = choose_visualization(data, SHARES, sql=COUNT_SQL)
    records = chart.spec["data"]["values"]
    assert [r["value"] for r in records] == [2, 1]
    assert [r["percent"] for r in records] == pytest.approx([200 / 3, 100 / 3])
    assert all(r["denominator"] == 3 for r in records)
    assert {t["field"] for t in chart.spec["encoding"]["tooltip"]} == {"label", "value", "percent", "denominator"}


def test_other_slice_preserves_raw_counts_and_rounded_percentage_sums():
    sql = REAL_PERCENT_SQL.replace("SELECT department,", "SELECT department, COUNT(*) AS employee_count,")
    data = result(["department", "employee_count", "percentage"], [[f"D{i}", 1, 11.11] for i in range(9)])
    chart = choose_visualization(data, SHARES, sql=sql)
    assert chart.kind == "pie"
    records = chart.spec["data"]["values"]
    assert records[-1]["magnitude"] == 2
    assert records[-1]["value"] == 22.22
    assert sum(r["magnitude"] for r in records) == 9
    assert sum(r["value"] for r in records) == pytest.approx(99.99)
    assert sum(r["percent"] for r in records) == pytest.approx(100)


@pytest.mark.parametrize("sql", ["SELECT salary, rating FROM employees", "SELECT * FROM employees",
                                  "SELECT e.* FROM employees e", "SELECT emp_id, e.* FROM employees e"])
def test_raw_sql_scatter_including_star_expansion(sql):
    data = result(["salary", "rating"], [[100, 4], [200, 5], [None, 3]])
    if "*" in sql:
        data = result(["emp_id", "salary", "rating"], [[1, 100, 4], [2, 200, 5], [3, None, 3]])
    chart = choose_visualization(data, "Scatter salary versus rating", sql=sql)
    assert chart.kind == "scatter"
    assert chart.spec["data"]["values"] == [{"x": 100, "y": 4}, {"x": 200, "y": 5}]
    assert "scatter" in compatible_visualizations(data, "Salary versus rating", sql=sql)
    assert "pie" not in compatible_visualizations(data, SHARES, sql=sql)


@pytest.mark.parametrize("sql", ["SELECT * FROM employees", "SELECT e.* FROM employees e",
                                  "SELECT * FROM employees GROUP BY department"])
def test_star_cannot_guess_percentage_partition(sql):
    data = result(["department", "percentage"], [["IT", 60], ["Sales", 40]])
    assert "pie" not in compatible_visualizations(data, SHARES, sql=sql)


def test_mixed_null_bubble_sizes_and_axes_never_crash_or_fabricate():
    question = "Bubble salary versus rating with size tenure"
    columns = ["salary", "rating", "tenure"]
    missing_size = result(columns, [[100, 4, None], [200, 5, 3]])
    assert "bubble" not in compatible_visualizations(missing_size, question, sql="SELECT * FROM employees")
    missing_axis = result(columns, [[100, 4, 2], [None, 5, 3], [200, 5, 4]])
    chart = choose_visualization(missing_axis, question, sql="SELECT * FROM employees")
    assert chart.kind == "bubble"
    assert chart.spec["data"]["values"] == [{"x": 100, "y": 4, "size": 2}, {"x": 200, "y": 5, "size": 4}]
    assert "Missing" in chart.note


def test_temporal_series_reject_hidden_dates_duplicates_and_null_cells():
    columns = ["month", "category", "hires"]
    for rows in (TIME_ROWS + [TIME_ROWS[0]], TIME_ROWS[:-1] + [["2026-01", "B", None]]):
        charts = compatible_visualizations(result(columns, rows), "Monthly hiring trend", sql=TIME_SQL)
        assert not {"line", "area", "stacked_area"} & charts.keys()
    two_dates = result(["month", "period", "category", "hires"],
                       [["2026-01", "2025-01", "A", 1], ["2026-03", "2025-03", "B", 2]])
    charts = compatible_visualizations(two_dates, "Monthly hiring trend")
    assert not {"line", "area", "stacked_area", "grouped_bar"} & charts.keys()


def test_sum_shares_with_companion_and_neutral_alias_use_ast_not_names():
    sql = (
        "SELECT department, SUM(amount) AS total_amount, "
        "ROUND(100.0 * SUM(amount) / NULLIF(SUM(SUM(amount)) OVER (), 0), 2) AS portion "
        "FROM sales WHERE year = 2026 GROUP BY department"
    )
    data = result(["department", "total_amount", "portion"], [["IT", 20, 66.67], ["Sales", 10, 33.33]])
    chart = choose_visualization(data, "Compare department sales", sql=sql)
    assert chart.kind == "pie"
    assert [r["magnitude"] for r in chart.spec["data"]["values"]] == [20, 10]
    assert "selected by SQL filters" in chart.note
    assert "pie" not in compatible_visualizations(data, "Compare independent conversion rates", sql=sql)


def test_proven_share_scale_and_companion_totals_must_be_finite_and_consistent():
    percentage = result(["department", "percentage"], [["IT", .6], ["Sales", .4]])
    assert "pie" not in compatible_visualizations(percentage, SHARES, sql=REAL_PERCENT_SQL)
    sql = REAL_PERCENT_SQL.replace("SELECT department,", "SELECT department, COUNT(*) AS employee_count,")
    overflowing = result(["department", "employee_count", "percentage"],
                         [["IT", 1e308, 50], ["Sales", 1e308, 50]])
    assert "pie" not in compatible_visualizations(overflowing, SHARES, sql=sql)
    huge = result(["department", "employee_count"], [["IT", 1e308], ["Sales", 0]])
    chart = choose_visualization(huge, SHARES, sql=COUNT_SQL)
    assert [r["percent"] for r in chart.spec["data"]["values"]] == [100, 0]


def test_wide_multilines_preserve_observed_series_and_require_comparable_units():
    data = result(["month", "junior_count", "senior_count"],
                  [["2026-03", 3, 4], ["2026-01", 1, 2]])
    chart = choose_visualization(data, "Monthly hiring trend")
    assert chart.kind == "line"
    assert chart.spec["encoding"]["color"]["field"] == "series"
    assert chart.spec["encoding"]["y"]["stack"] is None
    assert [(r["series"], r["value"]) for r in chart.spec["data"]["values"]] == [
        ("junior_count", 1), ("senior_count", 2), ("junior_count", 3), ("senior_count", 4)]
    assert "stacked_area" not in compatible_visualizations(data, "Monthly hiring trend")
    invalid = result(data.columns, [["2026-03", 3, None], ["2026-01", 1, 2]])
    assert "line" not in compatible_visualizations(invalid, "Monthly hiring trend")
    mixed = result(["month", "salary", "rating"], [["2026-03", 100, 4], ["2026-01", 200, 5]])
    assert "line" not in compatible_visualizations(mixed, "Monthly salary and rating trend")


def test_grouped_nulls_and_stack_sum_overflow_fail_closed():
    data = result(["department", "category", "employee_count"],
                  [["IT", "A", 1], ["IT", "B", None], ["Sales", "A", 3], ["Sales", "B", 4]])
    charts = compatible_visualizations(data, "Compare employee counts by category", sql=SERIES_SQL)
    assert not {"grouped_bar", "stacked_bar", "normalized_bar"} & charts.keys()
    overflowing = result(data.columns, [["IT", "A", 1e308], ["IT", "B", 1e308]])
    charts = compatible_visualizations(overflowing, "Compare groups", sql=SERIES_SQL)
    assert "grouped_bar" in charts
    assert not {"stacked_bar", "normalized_bar"} & charts.keys()