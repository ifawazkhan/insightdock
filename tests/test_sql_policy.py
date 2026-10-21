import pytest

from chatbot.sql_policy import PolicyError, validate_sql


@pytest.mark.parametrize("sql", [
    "SELECT * FROM employees_messy",
    "SELECT m.department, COUNT(*), AVG(m.salary) FROM hr_demo.employees_messy m GROUP BY m.department",
    "WITH x AS (SELECT salary FROM employees_messy) SELECT * FROM x",
    "WITH x AS (SELECT * FROM employees_messy), y AS (SELECT * FROM x) SELECT * FROM y",
    "SELECT * FROM employees_messy WHERE salary > (SELECT AVG(salary) FROM employees_messy)",
    "SELECT salary FROM employees_messy UNION ALL SELECT salary FROM employees_messy",
    "SELECT CASE WHEN salary > 0 THEN salary ELSE 0 END FROM employees_messy",
    "SELECT ROW_NUMBER() OVER (ORDER BY salary DESC) FROM employees_messy",
    "SELECT * FROM employees_messy WHERE department LIKE '%Sales%'",
    "SELECT 'O''Brien' FROM employees_messy LIMIT 1",
])
def test_reads(sql):
    result = validate_sql(sql, "hr_demo", "employees_messy")
    assert result.read_only
    assert not result.destructive


@pytest.mark.parametrize("sql", [
    "SELECT * FROM employees_messy WHERE salary > 0 AND rating > 3",
    "SELECT * FROM employees_messy WHERE salary > 0 OR rating > 3",
    "SELECT * FROM employees_messy WHERE (salary > 0 AND rating > 3) OR NOT status = 'inactive'",
    "SELECT name, COUNT(*) AS duplicate_count FROM employees_messy "
    "WHERE name IS NOT NULL AND name <> '' GROUP BY name HAVING COUNT(*) > 1",
    "WITH duplicates AS (SELECT emp_id, name, department, salary, join_date_raw, "
    "join_date, city, email, phone, rating, status, COUNT(*) AS duplicate_count "
    "FROM employees_messy GROUP BY emp_id, name, department, salary, join_date_raw, "
    "join_date, city, email, phone, rating, status HAVING COUNT(*) > 1) "
    "SELECT e.*, d.duplicate_count FROM employees_messy e JOIN duplicates d ON "
    "e.emp_id <=> d.emp_id AND e.name <=> d.name AND e.department <=> d.department "
    "AND e.salary <=> d.salary AND e.join_date_raw <=> d.join_date_raw "
    "AND e.join_date <=> d.join_date AND e.city <=> d.city AND e.email <=> d.email "
    "AND e.phone <=> d.phone AND e.rating <=> d.rating AND e.status <=> d.status",
])
def test_logical_operators_and_duplicate_queries(sql):
    result = validate_sql(sql, "hr_demo", "employees_messy")
    assert result.read_only
    assert not result.destructive
    # The canonical statement must remain valid under the same policy.
    assert validate_sql(result.sql, "hr_demo", "employees_messy").sql == result.sql


@pytest.mark.parametrize("predicate", [
    "salary > 0 AND SLEEP(1) = 0",
    "salary > 0 OR unknown_udf(salary) = 0",
    "salary > 0 AND (rating > 3 OR LOAD_FILE('/etc/passwd') IS NOT NULL)",
    "salary > 0 AND emp_id IN (SELECT emp_id FROM other)",
])
def test_logical_operators_do_not_bypass_safety(predicate):
    with pytest.raises(PolicyError):
        validate_sql(f"SELECT * FROM employees_messy WHERE {predicate}", "hr_demo", "employees_messy")


@pytest.mark.parametrize("sql,destructive", [
    ("INSERT INTO employees_messy(salary) VALUES(42)", False),
    ("INSERT INTO employees_messy(salary) SELECT salary FROM employees_messy", False),
    ("UPDATE employees_messy SET salary=42 WHERE employee_id=1", False),
    ("UPDATE employees_messy SET salary=42", True),
    ("DELETE FROM employees_messy WHERE salary < 0", False),
    ("DELETE FROM employees_messy", True),
    ("CREATE TABLE employees_messy(id INT PRIMARY KEY, salary DECIMAL(10,2))", False),
    ("ALTER TABLE employees_messy ADD COLUMN test_col INT", True),
    ("ALTER TABLE employees_messy DROP COLUMN test_col", True),
    ("CREATE INDEX ix_salary ON employees_messy(salary)", False),
    ("DROP INDEX ix_salary ON employees_messy", True),
    ("DROP TABLE employees_messy", True),
    ("TRUNCATE TABLE employees_messy", True),
])
def test_mutations(sql, destructive):
    result = validate_sql(sql, "hr_demo", "employees_messy")
    assert not result.read_only
    assert result.destructive == destructive


@pytest.mark.parametrize("sql", [
    "", "SELECT 1", "SELECT * FROM other", "SELECT * FROM otherdb.employees_messy",
    "SELECT * FROM employees_messy JOIN departments ON 1=1",
    "SELECT * FROM employees_messy WHERE salary IN (SELECT salary FROM other)",
    "WITH x AS (SELECT * FROM other) SELECT * FROM x",
    "WITH employees_messy AS (SELECT * FROM other) SELECT * FROM employees_messy",
    "WITH x AS (SELECT 1) SELECT * FROM x",
    "SELECT * FROM employees_messy; DELETE FROM employees_messy",
    "SELECT * FROM employees_messy; ;",
    "SELECT * FROM employees_messy /*!50000 INTO OUTFILE '/tmp/out' */",
    "SELECT * FROM employees_messy -- trailing comment",
    "SELECT * FROM employees_messy FOR UPDATE",
    "SELECT * INTO OUTFILE '/tmp/out' FROM employees_messy",
    "SELECT LOAD_FILE('/etc/passwd') FROM employees_messy",
    "SELECT SLEEP(1) FROM employees_messy", "SELECT BENCHMARK(10, 1) FROM employees_messy",
    "SELECT unknown_udf(salary) FROM employees_messy",
    "SELECT @@version FROM employees_messy", "SET @x=1", "USE hr_demo",
    "GRANT ALL ON hr_demo.* TO 'x'", "CALL routine()", "LOAD DATA INFILE '/tmp/data' INTO TABLE employees_messy",
    "CREATE DATABASE employees_messy", "CREATE VIEW employees_messy AS SELECT 1",
    "CREATE TABLE employees_messy AS SELECT * FROM other",
    "CREATE TABLE employees_messy LIKE other",
    "CREATE TABLE employees_messy (id INT) DATA DIRECTORY='/tmp'",
    "ALTER TABLE employees_messy RENAME TO other",
    "ALTER TABLE employees_messy ADD FOREIGN KEY (id) REFERENCES other(id)",
    "DROP TABLE employees_messy, other", "TRUNCATE TABLE other",
    "DROP INDEX ix_salary ON other", "CREATE INDEX ix ON other(salary)",
    "UPDATE other SET salary=1", "DELETE FROM other",
    "INSERT INTO employees_messy(salary) SELECT salary FROM other",
    "DELETE m FROM employees_messy m JOIN other o ON m.id=o.id",
    "SELECT * FROM employees_messy INTERSECT SELECT * FROM employees_messy",
])
def test_rejections(sql):
    with pytest.raises(PolicyError):
        validate_sql(sql, "hr_demo", "employees_messy")