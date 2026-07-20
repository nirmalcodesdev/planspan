"""Demo shop API. Real models, seed, and endpoints land in PR2."""
from fastapi import FastAPI

app = FastAPI(title="PlanSpan demo shop")


@app.get("/health")
def health():
    return {"ok": True}
