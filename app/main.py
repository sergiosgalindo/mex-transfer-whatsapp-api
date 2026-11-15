from fastapi import FastAPI

from app.tickets.router import router

app = FastAPI(
    title="mex-transfer-whatsapp-api",
    description="WhatsApp bridge for the CoDi event-ticket checkout. It talks to Meta and to mex-transfer-api.",
    version="1.0.0",
)
app.include_router(router)


@app.get("/api/health")
def health() -> dict:
    return {"status": "UP"}
