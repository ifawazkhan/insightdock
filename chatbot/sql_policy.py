"""Fail-closed, MySQL AST policy. Database grants are the final security boundary."""

from dataclasses import dataclass
import hashlib
import re

import sqlglot
from sqlglot import exp
from sqlglot.errors import ErrorLevel, SqlglotError
from sqlglot.optimizer.scope import traverse_scope

from chatbot.config import identifier


class PolicyError(ValueError):
    """The submitted statement is not in the supported SQL subset."""


# No arbitrary UDFs: a function allowlist is safer than a short dangerous-function denylist.
FUNCTIONS = frozenset({
    "ABS", "AVG", "COUNT", "MAX", "MIN", "SUM", "ROUND", "FLOOR", "CEIL",
    "COALESCE", "IF", "NULLIF", "IFNULL", "LOWER", "UPPER", "TRIM", "LTRIM",
    "RTRIM", "LENGTH", "CHAR_LENGTH", "CONCAT", "CONCAT_WS", "SUBSTRING", "REPLACE",
    "CAST", "TRY_CAST", "YEAR", "MONTH", "DAY", "DAY_OF_MONTH", "DATE", "DATE_FORMAT",
    "DATEDIFF", "DATE_DIFF", "DATE_ADD", "DATE_SUB", "CURRENT_DATE", "CURRENT_TIMESTAMP",
    "ROW_NUMBER", "RANK", "DENSE_RANK", "LAG", "LEAD", "FIRST_VALUE", "LAST_VALUE",
    "NTILE", "GREATEST", "LEAST", "STR_TO_DATE", "TIME_TO_STR", "STR_TO_TIME", "CASE",
})


@dataclass(frozen=True)
class ValidatedSQL:
    sql: str
    operation: str
    read_only: bool
    destructive: bool
    warning: str
    target_tables: tuple[str, ...] = ()

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.sql.encode()).hexdigest()


def validate_sql(sql: str, database: str, table: str, *, allowed_tables: tuple[str, ...] | None = None) -> ValidatedSQL:
    if allowed_tables is None:
        identifier(database)
    identifier(table)
    allowed = set(allowed_tables) if allowed_tables is not None else {table}
    if not sql.strip() or len(sql) > 20000:
        raise PolicyError("Enter one SQL statement, no longer than 20,000 characters.")
    # Conservative, including markers inside string literals. This closes MySQL executable
    # comments and SQLGlot/MySQL differences. No optimizer hints from untrusted input.
    if any(marker in sql for marker in ("/*", "*/", "--", "#", "\\", "\x00")):
        raise PolicyError("SQL comments, backslash escapes and NUL characters are unsupported.")
    if re.search(r"\b(?:OUTFILE|DUMPFILE|PROCEDURE|SQL_CALC_FOUND_ROWS)\b|@", sql, re.I):
        raise PolicyError("File access, routines, session variables and administrative SQL are blocked.")
    try:
        statements = sqlglot.parse(sql, read="mysql", error_level=ErrorLevel.RAISE)
    except SqlglotError as exc:
        raise PolicyError("SQL could not be parsed as a supported MySQL statement.") from exc
    if len(statements) != 1 or statements[0] is None:
        raise PolicyError("Exactly one SQL statement is allowed.")
    ast = statements[0]
    read_only = isinstance(ast, (exp.Select, exp.Union, exp.Intersect, exp.Except))
    if isinstance(ast, (exp.Intersect, exp.Except)) or any(
        isinstance(node, (exp.Intersect, exp.Except)) for node in ast.walk()
    ):
        raise PolicyError("INTERSECT and EXCEPT are not supported across MySQL versions.")
    if not read_only and not isinstance(ast, (
        exp.Insert, exp.Update, exp.Delete, exp.Create, exp.Drop, exp.Alter, exp.TruncateTable
    )):
        raise PolicyError("Only SELECT/UNION, DML and supported table/index DDL are allowed.")
    for node in ast.walk():
        if isinstance(node, (exp.Command, exp.Into, exp.Lock, exp.Parameter, exp.Placeholder)):
            raise PolicyError("Unparsed commands, SELECT INTO, locks and variables are unsupported.")
        # SQLGlot models AND/OR as Func subclasses, but they are SQL operators.
        # Still visit every child so nested function calls remain allowlisted.
        if isinstance(node, exp.Func) and not isinstance(node, (exp.And, exp.Or)):
            name = node.name.upper() if isinstance(node, exp.Anonymous) else node.sql_name().upper()
            if name not in FUNCTIONS:
                raise PolicyError(f"Function {name} is not in the supported built-in allowlist.")

    virtual_tables: set[int] = set()
    try:
        for scope in traverse_scope(ast):
            for node, source in scope.selected_sources.values():
                if not isinstance(source, exp.Table) and isinstance(node, exp.Table):
                    virtual_tables.add(id(node))
    except Exception as exc:
        raise PolicyError("Could not resolve query aliases and CTEs safely.") from exc

    index_name_node = None
    if isinstance(ast, (exp.Create, exp.Drop, exp.Alter)):
        kind = str(ast.args.get("kind", "")).upper()
        if kind not in {"TABLE", "INDEX"}:
            raise PolicyError("Only table and index DDL are supported.")
        if ast.args.get("properties") or ast.args.get("expression"):
            raise PolicyError("DDL table options and CREATE AS/LIKE are not supported.")
        if isinstance(ast, exp.Drop) and kind == "INDEX":
            cluster = ast.args.get("cluster")
            if not isinstance(cluster, exp.OnProperty) or not isinstance(cluster.this, exp.Identifier):
                raise PolicyError("Use DROP INDEX index_name ON employees_messy (unqualified).")
            if cluster.this.name not in allowed:
                raise PolicyError("Index operations must target the allowed table.")
            index_name_node = ast.this
            identifier(index_name_node.name)
        if isinstance(ast, exp.Alter):
            # Narrow action matrix: SQLGlot falls back to Command for many MySQL ALTER forms.
            for action in ast.args.get("actions", []):
                if not isinstance(action, (exp.ColumnDef, exp.Drop, exp.AlterColumn)):
                    raise PolicyError("ALTER supports ADD/DROP/ALTER COLUMN only; renames are blocked.")
                if isinstance(action, exp.Drop) and str(action.args.get("kind", "")).upper() != "COLUMN":
                    raise PolicyError("Only DROP COLUMN is supported inside ALTER.")
        # Disallow constraints referencing other objects or side-effecting defaults/options.
        allowed_ddl_nodes = {
            "Create", "Drop", "Alter", "TruncateTable", "Schema", "Table", "Identifier",
            "ColumnDef", "DataType", "DataTypeParam", "Literal", "ColumnConstraint",
            "NotNullColumnConstraint", "PrimaryKeyColumnConstraint", "UniqueColumnConstraint",
            "DefaultColumnConstraint", "AutoIncrementColumnConstraint", "PrimaryKey",
            "UniqueColumnConstraint", "Index", "IndexParameters", "Ordered", "Column",
            "OnProperty", "AlterColumn", "Null", "Boolean", "Neg", "Tuple",
        }
        if any(type(node).__name__ not in allowed_ddl_nodes for node in ast.walk()):
            raise PolicyError("This DDL form is not in the tested table/index subset.")

    tables = list(ast.find_all(exp.Table))
    for reference in tables:
        if id(reference) in virtual_tables or reference is index_name_node:
            continue
        if (not isinstance(reference.this, exp.Identifier) or reference.name not in allowed
                or reference.catalog or (reference.db and reference.db != database)):
            if allowed_tables is None:
                raise PolicyError(f"Only {database}.{table} may be accessed.")
            raise PolicyError(f"Only discovered base tables in the selected schema {database} may be accessed.")
    if not tables and index_name_node is None:
        raise PolicyError("Queries must reference the employee table.")
    if read_only and not any(id(t) not in virtual_tables for t in tables):
        raise PolicyError("Queries must read the employee table.")
    if isinstance(ast, (exp.Update, exp.Delete)):
        if not isinstance(ast.this, exp.Table) or ast.this.args.get("joins") or ast.args.get("tables"):
            raise PolicyError("Only single-table UPDATE and DELETE are supported.")
    if isinstance(ast, exp.Insert) and (ast.args.get("overwrite") or ast.args.get("alternative")):
        raise PolicyError("Only ordinary INSERT is supported.")
    if isinstance(ast, exp.TruncateTable) and (
        len(ast.expressions) != 1 or any(ast.args.get(key) for key in ("is_database", "cluster", "identity", "option"))
    ):
        raise PolicyError("Only a simple single-table TRUNCATE is supported.")
    if isinstance(ast, exp.Drop) and len(tables) > 1:
        raise PolicyError("Only one table may be dropped at a time.")
    destructive = isinstance(ast, (exp.Drop, exp.Alter, exp.TruncateTable)) or (
        isinstance(ast, (exp.Update, exp.Delete)) and ast.args.get("where") is None
    )
    warning = ""
    if isinstance(ast, (exp.Create, exp.Drop, exp.Alter, exp.TruncateTable)):
        warning = "MySQL DDL can commit immediately and cannot be reliably rolled back. Back up first."
    elif destructive:
        warning = "No WHERE clause: this operation may change or delete every employee row."
    # Execute the exact canonical SQL displayed to the user, not unchecked original text.
    canonical = ast.sql(dialect="mysql", unsupported_level=ErrorLevel.RAISE)
    targets = tuple(sorted({t.name for t in tables if id(t) not in virtual_tables and t is not index_name_node}))
    if index_name_node is not None:
        targets = (ast.args["cluster"].this.name,)
    return ValidatedSQL(canonical, type(ast).__name__.upper(), read_only, destructive, warning, targets)