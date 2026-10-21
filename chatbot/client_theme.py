"""Explicit client themes; no private Streamlit state or menu automation."""
from copy import deepcopy
import html

import pandas as pd
import streamlit as st


def theme_css(dark: bool) -> str:
    mode = "dark" if dark else "light"
    bg, surface, text, muted, border, accent, bubble = (
        ("#0f172a", "#334155", "#f8fafc", "#cbd5e1", "#64748b", "#a5b4fc", "#3b4b63") if dark else
        ("#f8fafc", "#ffffff", "#0f172a", "#475569", "#cbd5e1", "#4f46e5", "#eef2ff"))
    return f"""
    .stApp {{color-scheme:{mode}; --chat-bg:{bg}; --chat-surface:{surface};
      --chat-text:{text}; --chat-muted:{muted}; --chat-border:{border};
      --chat-accent:{accent}; --chat-bubble:{bubble};}}
    .stApp, [data-testid="stMain"], [data-testid="stBottom"] {{background:{bg};color:{text};}}
    [data-testid="stSidebar"] {{background:{surface};color:{text};}}
    [data-testid="stSidebar"] p, [data-testid="stSidebar"] label,
    [data-testid="stSidebar"] [data-testid="stMarkdownContainer"],
    [data-testid="stSidebar"] [data-testid="stText"],
    [data-testid="stSidebar"] [data-baseweb="select"] span {{color:{text};}}
    [data-testid="stSidebar"] button {{color:{text};}}
    [data-testid="stSidebar"] [data-baseweb="select"] > div,
    [data-testid="stSidebar"] input {{background:{bg};color:{text};}}
    .stApp p, .stApp label, .stApp h1, .stApp h2, .stApp h3,
    .stApp [data-testid="stText"], .stApp [data-testid="stText"] *,
    .stApp [data-testid="stMarkdownContainer"] span, .stApp [data-testid="stMetricValue"],
    .stApp [data-baseweb="select"] span {{color:{text};}}
    .stApp input, .stApp textarea, .stApp [data-baseweb="select"] > div,
    .stApp [data-testid="stChatInput"], [data-testid="stPopoverBody"],
    [data-baseweb="popover"], [data-baseweb="menu"], [role="listbox"] {{background:{surface};color:{text};}}
    .stApp [data-testid="stChatInput"] > div, .stApp [data-testid="stChatInput"] > div > div,
    .stApp [data-testid="stChatInput"] button, .stApp [data-testid="stPopover"] button {{background:{surface};color:{text};}}
    .stApp textarea::placeholder, .stApp input::placeholder {{color:{muted};opacity:1;}}
    .stApp [data-testid="stChatMessageAvatarAssistant"] {{background:{bubble};color:{accent};}}
    .stApp [data-testid="stTooltipIcon"] {{color:{muted};}}
    .stApp button {{color:{text};}}
    .stApp [role="tab"] {{color:{muted};}}
    .stApp [role="tab"][aria-selected="true"] {{color:{accent};}}
    .client-table {{overflow:auto;max-height:420px;border:1px solid {border};border-radius:8px;}}
    .client-table table {{width:100%;border-collapse:collapse;color:{text};font-size:14px;}}
    .client-table th {{position:sticky;top:0;background:{bubble};font-weight:600;}}
    .client-table th,.client-table td {{padding:9px 12px;text-align:left;border-bottom:1px solid {border};white-space:nowrap;}}
    .client-table tr:nth-child(even) {{background:{surface};}}
    """


def themed_spec(spec: dict, dark: bool) -> dict:
    result = deepcopy(spec)
    ink = "#e2e8f0" if dark else "#0f172a"
    grid = "#334155" if dark else "#e2e8f0"
    colors = ["#818cf8", "#38bdf8", "#2dd4bf", "#c084fc"] if dark else ["#4f46e5", "#0284c7", "#0d9488", "#9333ea"]
    config = result.setdefault("config", {})
    config.setdefault("axis", {}).update(labelColor=ink, titleColor=ink, gridColor=grid, tickColor=grid)
    config.setdefault("legend", {}).update(labelColor=ink, titleColor=ink)
    config.setdefault("title", {})["color"] = ink
    config.setdefault("range", {})["category"] = colors
    config.setdefault("mark", {})["color"] = colors[0]
    result["background"] = "transparent"
    return result


def client_table(frame: pd.DataFrame) -> None:
    """Escaped, scrollable table that follows explicit light/dark palettes."""
    columns = "".join(f"<th>{html.escape(str(c))}</th>" for c in frame.columns)
    rows = "".join("<tr>" + "".join(f"<td>{html.escape(str(v))}</td>" for v in row) + "</tr>"
                   for row in frame.itertuples(index=False, name=None))
    st.html(f'<div class="client-table" role="region" aria-label="Result data" tabindex="0"><table><thead><tr>{columns}</tr></thead><tbody>{rows}</tbody></table></div>')