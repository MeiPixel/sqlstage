"""测试专用合成夹具：运行时创建临时 SQLite，不分发任何业务数据。"""
import sqlite3
from contextlib import closing
from pathlib import Path


def create_database(directory):
    path = Path(directory) / "demo.sqlite"
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.executescript("""
            CREATE TABLE products(id INTEGER PRIMARY KEY, name TEXT, price REAL, brand TEXT);
            CREATE TABLE customers(id INTEGER PRIMARY KEY, name TEXT);
            CREATE TABLE orders(id INTEGER PRIMARY KEY, customer_id INTEGER, product_id INTEGER,
                FOREIGN KEY(customer_id) REFERENCES customers(id),
                FOREIGN KEY(product_id) REFERENCES products(id));
            INSERT INTO products VALUES (1, 'Test One', 10, 'Acme'),
                (2, 'Test Two', 25, 'Other'), (3, 'Test Three', 80, 'Acme');
            INSERT INTO customers VALUES (1, 'Test Customer');
            INSERT INTO orders VALUES (1, 1, 1);
        """)
    return path


def question_pool():
    return [{"query": f"Find products priced below {limit}",
             "sql": f"SELECT name, price FROM products WHERE price < {limit}"}
            for limit in range(10, 310, 10)]
