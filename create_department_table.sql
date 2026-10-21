-- Canonical department dimension for the `employees` schema.
-- head_count values reflect the real groupings found in employees_messy,
-- where each department appears under several inconsistent spellings.

CREATE TABLE IF NOT EXISTS department (
    dept_id       INT AUTO_INCREMENT PRIMARY KEY,
    dept_name     VARCHAR(40)    NOT NULL,
    dept_code     VARCHAR(8)     NOT NULL,
    location      VARCHAR(60)    NOT NULL,
    head_count    INT            NOT NULL,
    annual_budget DECIMAL(12, 2) NOT NULL,
    created_at    DATE           NOT NULL,
    UNIQUE KEY uq_dept_name (dept_name),
    UNIQUE KEY uq_dept_code (dept_code)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4;

INSERT INTO department (dept_name, dept_code, location, head_count, annual_budget, created_at)
VALUES
    ('Engineering',     'ENG', 'Bangalore, IN',     116, 18500000.00, '2015-03-02'),
    ('Support',         'SUP', 'Manila, PH',        119,  7250000.00, '2016-07-18'),
    ('Finance',         'FIN', 'London, UK',        115,  9800000.00, '2015-01-12'),
    ('Marketing',       'MKT', 'San Francisco, US', 114, 10400000.00, '2017-05-09'),
    ('Sales',           'SLS', 'New York, US',      113, 12000000.00, '2015-02-23'),
    ('Human Resources', 'HR',  'Austin, US',        110,  5300000.00, '2015-06-30'),
    ('Operations',      'OPS', 'Chicago, US',       107,  8600000.00, '2016-11-14'),
    ('Product',         'PRD', 'Seattle, US',       101, 11200000.00, '2018-04-03')
ON DUPLICATE KEY UPDATE
    location      = VALUES(location),
    head_count    = VALUES(head_count),
    annual_budget = VALUES(annual_budget),
    created_at    = VALUES(created_at);
