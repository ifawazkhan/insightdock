-- Self-referencing category hierarchy for recursive-CTE practice.
USE sql_practice;

DROP TABLE IF EXISTS category;
CREATE TABLE category (
    category_id   INT PRIMARY KEY,
    category_name VARCHAR(60) NOT NULL,
    parent_id     INT NULL,
    CONSTRAINT fk_parent FOREIGN KEY (parent_id) REFERENCES category (category_id)
) ENGINE = InnoDB;

INSERT INTO category (category_id, category_name, parent_id) VALUES
    (1, 'Electronics', NULL),
    (2, 'Mobiles',     1),
    (3, 'Android',     2),
    (4, 'Laptops',     1);
