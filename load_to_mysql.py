"""Load the generated employee dataset into MySQL so it can be browsed in Workbench."""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from urllib.parse import quote_plus

import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import Boolean, Date, Integer, String, create_engine, text
from sqlalchemy.engine import Engine

from generate_employee_dataset import OUTPUT_PATH, SHEET_NAME

IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

COLUMN_TYPES = {
    "employee_id": String(10),
    "first_name": String(50),
    "last_name": String(50),
    "email": String(120),
    "department": String(40),
    "job_title": String(60),
    "hire_date": Date(),
    "salary": Integer(),
    "city": String(80),
    "is_active": Boolean(),
}


def _safe_identifier(value: str, label: str) -> str:
    """MySQL identifiers cannot be bound as parameters, so reject anything not plainly alphanumeric."""
    if not IDENTIFIER.fullmatch(value):
        raise ValueError(f"Unsafe {label}: {value!r}")
    return value


def _make_engine(database: str | None) -> Engine:
    host = os.environ.get("MYSQL_HOST", "127.0.0.1")
    port = os.environ.get("MYSQL_PORT", "3306")
    user = os.environ.get("MYSQL_USER", "root")
    password = os.environ.get("MYSQL_PASSWORD")
    if password is None:
        raise SystemExit("MYSQL_PASSWORD is not set. Copy .env.example to .env and fill it in.")

    suffix = f"/{database}" if database else ""
    url = f"mysql+pymysql://{quote_plus(user)}:{quote_plus(password)}@{host}:{port}{suffix}?charset=utf8mb4"
    return create_engine(url, pool_pre_ping=True)


def load(frame: pd.DataFrame, database: str, table: str) -> int:
    with _make_engine(None).connect() as connection:
        connection.execute(text(f"CREATE DATABASE IF NOT EXISTS `{database}` CHARACTER SET utf8mb4"))
        connection.commit()

    engine = _make_engine(database)
    frame.to_sql(table, engine, if_exists="replace", index=False, dtype=COLUMN_TYPES)

    with engine.connect() as connection:
        connection.execute(text(f"ALTER TABLE `{table}` MODIFY `employee_id` VARCHAR(10) NOT NULL"))
        connection.execute(text(f"ALTER TABLE `{table}` ADD PRIMARY KEY (`employee_id`)"))
        connection.execute(text(f"ALTER TABLE `{table}` ADD UNIQUE KEY `uq_email` (`email`)"))
        connection.execute(text(f"ALTER TABLE `{table}` ADD INDEX `ix_department` (`department`)"))
        connection.commit()
        return connection.execute(text(f"SELECT COUNT(*) FROM `{table}`")).scalar_one()


def main() -> None:
    load_dotenv(Path(__file__).parent / ".env")
    database = _safe_identifier(os.environ.get("MYSQL_DATABASE", "hr_demo"), "database name")
    table = _safe_identifier(os.environ.get("MYSQL_TABLE", "employees"), "table name")

    if not OUTPUT_PATH.exists():
        raise SystemExit(f"{OUTPUT_PATH.name} not found. Run generate_employee_dataset.py first.")

    frame = pd.read_excel(OUTPUT_PATH, sheet_name=SHEET_NAME)
    frame["hire_date"] = pd.to_datetime(frame["hire_date"]).dt.date

    count = load(frame, database, table)
    print(f"Loaded {count} rows into `{database}`.`{table}`", file=sys.stderr)


if __name__ == "__main__":
    main()
