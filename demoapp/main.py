from fastapi import FastAPI
from sqlalchemy import func, select

from db import SessionLocal
from models import Order, Product, User
from tracing import setup_tracing

app = FastAPI(title="PlanSpan demo shop")
setup_tracing(app)


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/orders")
def orders_by_email(email: str):
    # indexed lookup on orders.email — until you DROP ix_orders_email live,
    # then it degrades to a full seq scan over 10M rows. the demo hinge.
    with SessionLocal() as s:
        rows = s.execute(
            select(Order.id, Order.total, Order.status, Order.created_at)
            .where(Order.email == email)
            .order_by(Order.created_at.desc())
            .limit(50)
        ).all()
    return [
        {"id": r.id, "total": float(r.total), "status": r.status, "created_at": r.created_at}
        for r in rows
    ]


@app.get("/search")
def revenue_by_status():
    # unindexed aggregate over the whole orders table — reliably slow,
    # good for showing a Hash Aggregate / seq scan plan.
    with SessionLocal() as s:
        rows = s.execute(
            select(Order.status, func.count().label("n"), func.sum(Order.total).label("revenue"))
            .group_by(Order.status)
        ).all()
    return [{"status": r.status, "count": r.n, "revenue": float(r.revenue or 0)} for r in rows]


@app.post("/checkout")
def checkout(user_id: int, product_id: int):
    with SessionLocal() as s:
        user = s.get(User, user_id)
        product = s.get(Product, product_id)
        if not user or not product:
            return {"error": "unknown user or product"}
        order = Order(
            user_id=user.id,
            email=user.email,
            product_id=product.id,
            total=product.price,
        )
        s.add(order)
        s.commit()
        return {"order_id": order.id, "total": float(order.total)}
