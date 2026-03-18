from __future__ import annotations

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from .models import CustomerTier, TicketPriority
from .service import HelpdeskService

service = HelpdeskService()


class CreateTicketRequest(BaseModel):
    title: str
    priority: TicketPriority = "normal"
    customer_tier: CustomerTier = "standard"
    created_hour: int = 0


app = FastAPI(title="Helpdesk Portal API")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/tickets", status_code=201)
def create_ticket(payload: CreateTicketRequest) -> dict[str, object]:
    try:
        ticket = service.create_ticket(
            title=payload.title,
            priority=payload.priority,
            customer_tier=payload.customer_tier,
            created_hour=payload.created_hour,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "id": ticket.id,
        "title": ticket.title,
        "priority": ticket.priority,
        "customer_tier": ticket.customer_tier,
        "status": ticket.status,
        "assignee": ticket.assignee,
        "created_hour": ticket.created_hour,
        "is_open": ticket.is_open,
    }
