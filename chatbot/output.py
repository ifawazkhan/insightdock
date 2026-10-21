"""Local display/export helpers. Downloads contain fetched rows only."""

import csv
from io import StringIO

from chatbot.db import QueryResult


def display_columns(columns: list[str]) -> list[str]:
    used: set[str] = set()
    labels = []
    for column in columns:
        name = column
        suffix = 2
        while name in used:
            name = f"{column} ({suffix})"
            suffix += 1
        used.add(name)
        labels.append(name)
    return labels


def safe_csv(result: QueryResult) -> str:
    def cell(value: object) -> object:
        # Spreadsheet formula injection protection applies to headers too.
        if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
            return "'" + value
        return value

    buffer = StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerow([cell(c) for c in display_columns(result.columns)])
    writer.writerows([[cell(v) for v in row] for row in result.rows])
    return buffer.getvalue()