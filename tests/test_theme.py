"""Appearance follows native themes rather than masking light/dark widgets."""
from pathlib import Path
import tomllib

from chatbot.db import QueryResult
from chatbot.visualization import choose_visualization
from chatbot.client_theme import theme_css, themed_spec

ROOT = Path(__file__).resolve().parents[1]


def test_native_palette_and_light_default():
    config = tomllib.loads((ROOT / ".streamlit/config.toml").read_text())["theme"]
    assert config["base"] == "light"
    assert config["light"]["backgroundColor"] == "#F8FAFC"
    assert config["dark"]["backgroundColor"] == "#0F172A"
    assert config["light"]["primaryColor"] == "#4F46E5"
    assert config["dark"]["primaryColor"] == "#818CF8"
    assert config["dark"]["sidebar"]["backgroundColor"] == "#334155"
    assert config["dark"]["sidebar"]["secondaryBackgroundColor"] == "#1E293B"
    for mode in ("light", "dark"):
        assert config[mode]["chartCategoricalColors"][0] == config[mode]["primaryColor"]


def test_charts_leave_colors_to_native_theme():
    result = QueryResult(["department", "avg_salary"], [["IT", 100], ["Sales", 80]], 0, False, .01)
    chart = choose_visualization(result, "Compare average salary by department")
    assert chart.spec is not None
    assert "color" not in chart.spec["mark"]
    assert "labelColor" not in chart.spec["config"]["axis"]
    assert chart.spec["data"]["values"] == [{"label": "IT", "value": 100}, {"label": "Sales", "value": 80}]


def test_styles_use_both_modes_without_texture():
    css = (ROOT / "assets/chat.css").read_text()
    assert "light-dark(#f8fafc, #0f172a)" in css
    assert '[data-testid="stSidebar"] .st-key-new_chat_button div.stButton > button' in css
    assert "--chat-action-bg: light-dark(#e0e7ff, #c7d2fe)" in css
    assert '[data-testid="stSidebar"] [data-testid="stBaseButton-primary"]' in css
    assert ".hero-mark img" in css
    assert "fractalNoise" not in css
    assert "gradient(" not in css


def test_explicit_client_palette_and_chart_copy():
    spec = choose_visualization(QueryResult(["department", "salary"], [["IT", 100], ["Sales", 80]], 0, False, .01), "Compare salary").spec
    assert "color-scheme:dark" in theme_css(True)
    assert "color-scheme:light" in theme_css(False)
    assert "[data-testid=\"stSidebar\"] {background:#334155;color:#f8fafc;}" in theme_css(True)
    dark = themed_spec(spec, True)
    assert dark["config"]["axis"]["labelColor"] == "#e2e8f0"
    assert "labelColor" not in spec["config"]["axis"]
    assert dark["data"] == spec["data"]


def test_only_native_menu_hidden_not_sidebar_reopen():
    css = (ROOT / "assets/chat.css").read_text()
    assert '#MainMenu, [data-testid="stMainMenu"]' in css
    assert '[data-testid="stToolbar"] { visibility: visible; }' in css


def test_answer_charts_and_metrics_have_no_padded_boxes():
    css = (ROOT / "assets/chat.css").read_text()
    assert '[data-testid="stVegaLiteChart"] { background: transparent; border: none; border-radius: 0; padding: 0; }' in css
    assert '[data-testid="stMetric"] { background: transparent; border: none; border-radius: 0; }' in css
    assert 'st.metric(visual.title, visual.value, border=False)' in (ROOT / "chat_app.py").read_text()
    report_ui = (ROOT / "chatbot/report_ui.py").read_text()
    assert 'st.container(border=False, key=f"business_report_{report.id}")' in report_ui