import base64
import os
import re
import time
from collections import OrderedDict
from typing import Any, Dict, List, Optional

import httpx
from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from app.models.schemas import ChatResponse
from app.tickets.delivery import deliver_image
from app.tickets.flow import core_from_env, handle_ticket_message, maybe_handle_ticket
from app.tickets.qr_images import render_entry_ticket

router = APIRouter()
_SENT: Dict[str, bytes] = {}
_SEEN_WAMIDS: "OrderedDict[str, float]" = OrderedDict()


class TicketChatRequest(BaseModel):
    phone: str = Field(min_length=1)
    text: str = Field(min_length=1)
    profileName: str = Field(default="")


class SendTicketRequest(BaseModel):
    orderId: str = Field(min_length=1)
    customerPhone: str = Field(min_length=1)
    customerName: str = Field(min_length=1)
    quantity: int = Field(ge=1)
    accessCode: str = Field(min_length=1)
    ticketQrPayload: str = Field(min_length=1)
    ticketMessage: str = Field(min_length=1)


def clear_sent_tickets() -> None:
    _SENT.clear()


@router.post("/internal/send-ticket")
def send_ticket(
    body: SendTicketRequest,
    x_internal_token: str = Header(default=""),
) -> Dict[str, str]:
    expected = os.getenv("INTERNAL_API_TOKEN", "")
    if expected and x_internal_token != expected:
        raise HTTPException(status_code=401, detail="invalid internal token")
    if body.orderId in _SENT:
        return {"status": "already_sent", "orderId": body.orderId}
    png = render_entry_ticket(body.ticketQrPayload, "Evento", body.accessCode, body.quantity)
    deliver_image(body.customerPhone, png, body.ticketMessage)
    _SENT[body.orderId] = png
    return {
        "status": "sent",
        "orderId": body.orderId,
        "pngBase64": base64.b64encode(png).decode("ascii"),
    }


@router.post("/tickets/chat", response_model=ChatResponse)
def ticket_chat(body: TicketChatRequest) -> ChatResponse:
    return handle_ticket_message(body.phone, body.text, body.profileName, core_from_env())


def _normalize_mx_phone(raw: str) -> str:
    digits = re.sub(r"\D+", "", raw or "")
    if len(digits) == 13 and digits.startswith("521"):
        return "52" + digits[3:]
    if len(digits) == 10:
        return "52" + digits
    return digits


def _extract_meta_messages(payload: Dict[str, Any]) -> List[Dict[str, str]]:
    items: List[Dict[str, str]] = []
    for entry in payload.get("entry") or []:
        for change in entry.get("changes") or []:
            value = change.get("value") or {}
            contacts = value.get("contacts") or []
            for msg in value.get("messages") or []:
                mtype = msg.get("type", "text")
                text = ""
                if mtype == "text":
                    text = (msg.get("text") or {}).get("body", "")
                elif mtype == "interactive":
                    inter = msg.get("interactive") or {}
                    text = (
                        (inter.get("list_reply") or {}).get("id")
                        or (inter.get("button_reply") or {}).get("id")
                        or (inter.get("list_reply") or {}).get("title")
                        or (inter.get("button_reply") or {}).get("title")
                        or ""
                    )
                sender = str(msg.get("from") or "")
                prof_name = ""
                for c in contacts:
                    if str(c.get("wa_id") or "") == sender:
                        prof_name = str((c.get("profile") or {}).get("name") or "")
                if text and sender:
                    items.append({
                        "id": str(msg.get("id") or ""),
                        "from": sender,
                        "text": text.strip(),
                        "profile_name": prof_name,
                    })
    return items


def _send_meta_payload(to_phone: str, body: Dict[str, Any]) -> Dict[str, Any]:
    token = (os.getenv("WHATSAPP_ACCESS_TOKEN") or os.getenv("WHATSAPP_TOKEN") or "").strip()
    phone_id = (os.getenv("PHONE_NUMBER_ID") or os.getenv("WHATSAPP_PHONE_NUMBER_ID") or "").strip()
    version = os.getenv("WHATSAPP_API_VERSION", "v25.0").strip()
    if not token or not phone_id:
        return {"sent": False, "simulated": True, "to": to_phone}
    url = f"https://graph.facebook.com/{version}/{phone_id}/messages"
    resp = httpx.post(
        url,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json=body,
        timeout=15.0,
    )
    print(f"[WhatsApp Outbound] to={to_phone} status={resp.status_code} body={resp.text}", flush=True)
    return {"sent": resp.status_code in (200, 201), "status_code": resp.status_code}


def _process_meta_webhook_sync(payload: Dict[str, Any]) -> None:
    for msg in _extract_meta_messages(payload):
        wamid = msg["id"]
        if wamid and wamid in _SEEN_WAMIDS:
            continue
        if wamid:
            _SEEN_WAMIDS[wamid] = time.time()
            while len(_SEEN_WAMIDS) > 500:
                _SEEN_WAMIDS.popitem(last=False)
        phone = _normalize_mx_phone(msg["from"])
        print(f"[WhatsApp Inbound] from={phone} name={msg['profile_name']!r} text={msg['text']!r}", flush=True)
        chat_res = maybe_handle_ticket(phone, msg["text"], msg["profile_name"], core_from_env())
        if chat_res is None:
            _send_meta_payload(phone, {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": phone,
                "type": "text",
                "text": {
                    "preview_url": False,
                    "body": "Este chat aparta boletos. Escribe boletos para ver las fechas disponibles.",
                },
            })
            continue
        flyer_b64 = (chat_res.tool_output or {}).get("event_flyer_b64")
        if flyer_b64:
            flyer_mime = (chat_res.tool_output or {}).get("event_flyer_mime") or "image/jpeg"
            promo_text = (chat_res.tool_output or {}).get("event_promo_text") or ""
            deliver_image(phone, base64.b64decode(flyer_b64), promo_text, mime_type=flyer_mime)
        qr_b64 = (chat_res.tool_output or {}).get("codi_qr_png_b64")
        if qr_b64:
            deliver_image(phone, base64.b64decode(qr_b64), chat_res.reply)
        elif chat_res.interactive_menu:
            menu = dict(chat_res.interactive_menu)
            menu["body"] = {"text": chat_res.reply[:1020]}
            res = _send_meta_payload(phone, {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": phone,
                "type": "interactive",
                "interactive": menu,
            })
            if not res.get("sent") and not res.get("simulated"):
                _send_meta_payload(phone, {
                    "messaging_product": "whatsapp",
                    "recipient_type": "individual",
                    "to": phone,
                    "type": "text",
                    "text": {"preview_url": False, "body": chat_res.reply[:4000]},
                })
        else:
            _send_meta_payload(phone, {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": phone,
                "type": "text",
                "text": {"preview_url": False, "body": chat_res.reply[:4000]},
            })

        follow_up = (chat_res.tool_output or {}).get("follow_up_text")
        if follow_up:
            _send_meta_payload(phone, {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": phone,
                "type": "text",
                "text": {"preview_url": False, "body": str(follow_up).strip()},
            })


@router.get("/webhook")
def verify_meta_webhook(
    hub_mode: Optional[str] = Query(default=None, alias="hub.mode"),
    hub_verify_token: Optional[str] = Query(default=None, alias="hub.verify_token"),
    hub_challenge: Optional[str] = Query(default=None, alias="hub.challenge"),
) -> PlainTextResponse:
    expected = (os.getenv("WHATSAPP_VERIFY_TOKEN") or os.getenv("VERIFY_TOKEN") or "verify-token-123").strip()
    if hub_mode == "subscribe" and hub_verify_token == expected and hub_challenge is not None:
        return PlainTextResponse(content=str(hub_challenge), status_code=200)
    raise HTTPException(status_code=403, detail="Invalid verify token")


@router.post("/webhook")
async def receive_meta_webhook(request: Request, background_tasks: BackgroundTasks) -> Dict[str, Any]:
    payload = await request.json()
    background_tasks.add_task(_process_meta_webhook_sync, payload)
    return {"status": "EVENT_RECEIVED"}
