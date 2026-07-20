"""Seed the demo shop. Runs server-side generate_series so 10M orders land fast.

Idempotent: skips tables that already have data. Re-run safe.

  python seed.py            # default sizes
  ORDERS=2000000 python seed.py
"""
import os

from sqlalchemy import text

from db import engine
from models import Base

N_USERS = int(os.environ.get("USERS", 100_000))
N_PRODUCTS = int(os.environ.get("PRODUCTS", 10_000))
N_ORDERS = int(os.environ.get("ORDERS", 10_000_000))


def _count(conn, table):
    return conn.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()


def main():
    Base.metadata.create_all(engine)

    with engine.begin() as conn:
        if _count(conn, "users") == 0:
            print(f"seeding {N_USERS} users")
            conn.execute(
                text(
                    "INSERT INTO users (id, email, name) "
                    "SELECT g, 'user' || g || '@example.com', 'User ' || g "
                    "FROM generate_series(1, :n) g"
                ),
                {"n": N_USERS},
            )

        if _count(conn, "products") == 0:
            print(f"seeding {N_PRODUCTS} products")
            conn.execute(
                text(
                    "INSERT INTO products (id, sku, name, price) "
                    "SELECT g, 'SKU' || lpad(g::text, 8, '0'), 'Product ' || g, "
                    "round((random() * 500 + 5)::numeric, 2) "
                    "FROM generate_series(1, :n) g"
                ),
                {"n": N_PRODUCTS},
            )

        if _count(conn, "orders") == 0:
            print(f"seeding {N_ORDERS} orders (this is the slow one)")
            conn.execute(
                text(
                    "INSERT INTO orders (user_id, email, product_id, total, status, created_at) "
                    "SELECT u, 'user' || u || '@example.com', "
                    "  (random() * :prod)::int + 1, "
                    "  round((random() * 500 + 5)::numeric, 2), "
                    "  CASE WHEN random() < 0.97 THEN 'paid' ELSE 'refunded' END, "
                    "  now() - (random() * interval '365 days') "
                    "FROM generate_series(1, :n) g, "
                    "  LATERAL (SELECT (random() * :users)::int + 1 AS u) s"
                ),
                {"n": N_ORDERS, "users": N_USERS - 1, "prod": N_PRODUCTS - 1},
            )

        print("analyze")
        conn.execute(text("ANALYZE"))

    print("done")


if __name__ == "__main__":
    main()
