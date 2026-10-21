-- Practice schema with the employees and orders tables from the exercise.
DROP SCHEMA IF EXISTS sql_practice;
CREATE SCHEMA sql_practice CHARACTER SET utf8mb4;
USE sql_practice;

CREATE TABLE employees (
    employee_id INT PRIMARY KEY,
    name        VARCHAR(60)    NOT NULL,
    department  VARCHAR(40)    NOT NULL,
    salary      INT            NOT NULL,
    manager_id  INT            NULL,
    CONSTRAINT fk_manager FOREIGN KEY (manager_id) REFERENCES employees (employee_id)
) ENGINE = InnoDB;

INSERT INTO employees (employee_id, name, department, salary, manager_id) VALUES
    (1, 'Michael Anderson', 'Management', 120000, NULL),
    (2, 'Emily Johnson',    'IT',          80000, 1),
    (3, 'Daniel Williams',  'Sales',       65000, 1),
    (4, 'Sophia Miller',    'IT',          70000, 2),
    (5, 'Ethan Brown',      'IT',          75000, 2),
    (6, 'Olivia Davis',     'Sales',       55000, 3);

CREATE TABLE orders (
    order_id    INT PRIMARY KEY,
    customer_id VARCHAR(10)    NOT NULL,
    order_date  DATE           NOT NULL,
    amount      INT            NOT NULL
) ENGINE = InnoDB;

INSERT INTO orders (order_id, customer_id, order_date, amount) VALUES
    (101, 'C1', '2026-01-05', 4000),
    (102, 'C2', '2026-01-20', 6000),
    (103, 'C1', '2026-02-03', 5000),
    (104, 'C3', '2026-02-15', 7000),
    (105, 'C2', '2026-03-10', 9000);
