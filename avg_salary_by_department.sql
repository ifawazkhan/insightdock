-- Average valid salary per matched department.
-- "Valid" excludes the 23 rows in employees_messy with salary <= 0 (min is -4770).
-- INNER JOIN also drops messy spellings that don't match a dept_name (Ops, Mktg, Engg, ...).

SELECT
    d.dept_name             AS department_name,
    d.dept_head             AS department_head,
    ROUND(AVG(m.salary), 2) AS avg_valid_salary,
    COUNT(*)                AS valid_employees
FROM employees_messy AS m
INNER JOIN departments AS d
        ON m.department = d.dept_name
WHERE m.salary > 0
GROUP BY d.dept_name, d.dept_head
ORDER BY avg_valid_salary DESC;
