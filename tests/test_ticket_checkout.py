import base64
import io
import json

import httpx
import zxingcpp
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

from app.tickets.core_client import CoreApi
from app.tickets.delivery import sent_images
from app.tickets.flow import TicketSession, _SESSIONS, clear_sessions, handle_ticket_message, maybe_handle_ticket
from app.tickets.router import clear_sent_tickets, router


def _decode(png: bytes) -> str:
    image = Image.open(io.BytesIO(png))
    found = zxingcpp.read_barcodes(image)
    assert found, "the PNG does not contain a decodable QR"
    return found[0].text


def _client() -> CoreApi:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/upcoming-dates"):
            return httpx.Response(200, json={"dates": ["2026-11-15"]})
        if request.method == "GET" and request.url.path.endswith("/events"):
            return httpx.Response(200, json=[{
                "id": "evt-1",
                "name": "Social de salsa",
                "venue": "Foro",
                "description": "Noche de salsa en el foro",
                "ticketTypes": [{
                    "id": "tt-1",
                    "name": "General",
                    "currentPrice": 180.0,
                    "available": 4,
                }],
            }])
        if request.method == "POST" and request.url.path.endswith("/orders"):
            body = json.loads(request.content)
            assert body == {
                "ticketTypeId": "tt-1",
                "quantity": 2,
                "customerName": "Ana López",
                "customerPhone": "525512345678",
            }
            return httpx.Response(200, json={
                "orderId": "ord-1",
                "quantity": 2,
                "amount": 360.0,
                "qrString": "CODI|360.00|1",
                "paymentLinkUrl": "https://pay.example/link/ord-1",
                "expiresAt": "2026-11-14T18:10:00Z",
            })
        return httpx.Response(404, json={"error": "missing"})

    return CoreApi("http://core.test", "", httpx.Client(transport=httpx.MockTransport(handler)))


def test_checkout_sends_a_decodable_codi_qr_and_the_payment_link():
    clear_sessions()
    client = _client()
    phone = "525512345678"
    dates = handle_ticket_message(phone, "boletos", "Ana López", client)
    assert dates.interactive_menu["action"]["sections"][0]["rows"][0]["id"] == "codi:date:2026-11-15"
    handle_ticket_message(phone, "codi:date:2026-11-15", "Ana López", client)
    handle_ticket_message(phone, "codi:event:evt-1", "Ana López", client)
    handle_ticket_message(phone, "codi:ticket:tt-1", "Ana López", client)
    ask_name = handle_ticket_message(phone, "2", "Ana López", client)
    assert "nombre y apellido" in ask_name.reply
    confirm = handle_ticket_message(phone, "Ana López", "Ana López", client)
    assert "525512345678" in confirm.reply
    assert "Ana López" in confirm.reply
    charged = handle_ticket_message(phone, "codi:confirm:yes", "Ana López", client)
    assert "Pagar / Escanear QR" in charged.reply
    assert "https://pay.example/link/ord-1" not in charged.reply
    assert charged.tool_output["paymentLinkUrl"] == "https://pay.example/link/ord-1"
    assert _decode(base64.b64decode(charged.tool_output["codi_qr_png_b64"])) == "CODI|360.00|1"


def test_faqs_use_the_event_sheet_and_do_not_invent_missing_facts():
    clear_sessions()
    _SESSIONS["525500000001"] = TicketSession(
        step="tickets",
        event_id="evt-1",
        event_name="Social de salsa",
        event_description="Noche de salsa en el foro",
    )
    client = _client()
    unknown = handle_ticket_message("525500000001", "¿Hay estacionamiento?", "Ana", client)
    assert "No tengo ese dato" in unknown.reply
    assert "gratis" not in unknown.reply.lower()
    known = handle_ticket_message("525500000001", "¿Habrá salsa?", "Ana", client)
    assert known.reply == "Según la ficha del evento: Noche de salsa en el foro"
    rule = handle_ticket_message("525500000001", "¿Cuánto tiempo tengo para pagar?", "Ana", client)
    assert "10 minutos" in rule.reply


def test_a_credit_question_stays_with_the_existing_assistant():
    clear_sessions()
    assert maybe_handle_ticket("522462168664", "¿Cuál es mi estado de cuenta y saldo disponible?") is None


def test_user_can_correct_quantity_directly_from_confirmation_step():
    clear_sessions()
    client = _client()
    phone = "522462168664"
    handle_ticket_message(phone, "boletos", "Segio", client)
    handle_ticket_message(phone, "codi:date:2026-11-15", "Segio", client)
    handle_ticket_message(phone, "codi:event:evt-1", "Segio", client)
    handle_ticket_message(phone, "codi:ticket:tt-1", "Segio", client)
    ask_name = handle_ticket_message(phone, "3", "Segio", client)
    assert "nombre y apellido" in ask_name.reply
    first_confirm = handle_ticket_message(phone, "Sergio Galindo", "Segio", client)
    assert "3 × General" in first_confirm.reply
    assert "$540.00 MXN" in first_confirm.reply
    assert "Sergio Galindo" in first_confirm.reply

    updated_confirm = handle_ticket_message(phone, "No, mejor 4 boletos", "Segio", client)
    assert updated_confirm.active_use_case == "CODI-TICKETS-CONFIRM"
    assert "4 × General" in updated_confirm.reply
    assert "$720.00 MXN" in updated_confirm.reply
    assert _SESSIONS[phone].quantity == 4
    assert _SESSIONS[phone].step == "confirm"


def test_internal_send_ticket_png_decodes_and_a_duplicate_does_not_send_again():
    clear_sent_tickets()
    sent_images.clear()
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    payload = '{"accessCode":"ABC123","orderId":"ord-1","eventId":"evt-1","quantity":2}'
    body = {
        "orderId": "ord-1",
        "customerPhone": "525512345678",
        "customerName": "Ana López",
        "quantity": 2,
        "accessCode": "ABC123",
        "ticketQrPayload": payload,
        "ticketMessage": "Boleto Social. Titular: Ana López. Entradas: 2. Presenta este QR en la puerta.",
    }
    first = client.post("/internal/send-ticket", json=body)
    assert first.status_code == 200
    assert first.json()["status"] == "sent"
    assert _decode(base64.b64decode(first.json()["pngBase64"])) == payload
    assert len(sent_images) == 1
    assert sent_images[0]["caption"].startswith("Boleto Social")
    second = client.post("/internal/send-ticket", json=body)
    assert second.status_code == 200
    assert second.json()["status"] == "already_sent"
    assert len(sent_images) == 1


def test_conversational_inbound_intents_lead_to_ticket_checkout():
    client = _client()
    clear_sessions()
    res1 = maybe_handle_ticket("522462168664", "hola, ¿cómo estás?", "Sergio", client)
    assert res1 is not None
    assert res1.active_use_case == "CODI-TICKETS-DATES"
    assert res1.interactive_menu is not None

    clear_sessions()
    res2 = maybe_handle_ticket("522462168664", "¿qué eventos hay este fin?", "Sergio", client)
    assert res2 is not None
    assert res2.active_use_case == "CODI-TICKETS-DATES"
    assert res2.interactive_menu is not None

    clear_sessions()
    res3 = maybe_handle_ticket("522462168664", "necesito ayuda por favor", "Sergio", client)
    assert res3 is not None
    assert res3.active_use_case == "CODI-TICKETS-DATES"


def test_affirmative_follow_up_answers_faq_instead_of_resetting_dates():
    client = _client()
    clear_sessions()
    # Step 1: User asks location
    res1 = maybe_handle_ticket("522462168664", "El Social de salsa en qué lugar se realizará?", "Sergio", client)
    assert res1 is not None
    assert res1.active_use_case == "CODI-TICKETS-FAQ"

    # Step 2: User answers affirmatively to the assistant's question
    res2 = maybe_handle_ticket("522462168664", "Si por favor", "Sergio", client)
    assert res2 is not None
    # Must NOT reset to date picker menu
    assert res2.active_use_case == "CODI-TICKETS-FAQ"
    assert res2.interactive_menu is None


def test_customer_name_with_prefix_and_substring_keywords():
    client = _client()
    phone = "525512345678"

    # Test 1: "A nombre de Josefina"
    clear_sessions()
    handle_ticket_message(phone, "boletos", "Sergio", client)
    handle_ticket_message(phone, "codi:date:2026-11-15", "Sergio", client)
    handle_ticket_message(phone, "codi:event:evt-1", "Sergio", client)
    handle_ticket_message(phone, "codi:ticket:tt-1", "Sergio", client)
    handle_ticket_message(phone, "3", "Sergio", client)
    confirm = handle_ticket_message(phone, "A nombre de Josefina", "Sergio", client)
    assert confirm.active_use_case == "CODI-TICKETS-CONFIRM"
    assert "Josefina" in confirm.reply

    # Test 2: "Josefina Martínez"
    clear_sessions()
    handle_ticket_message(phone, "boletos", "Sergio", client)
    handle_ticket_message(phone, "codi:date:2026-11-15", "Sergio", client)
    handle_ticket_message(phone, "codi:event:evt-1", "Sergio", client)
    handle_ticket_message(phone, "codi:ticket:tt-1", "Sergio", client)
    handle_ticket_message(phone, "3", "Sergio", client)
    confirm2 = handle_ticket_message(phone, "Josefina Martínez", "Sergio", client)
    assert confirm2.active_use_case == "CODI-TICKETS-CONFIRM"
    assert "Josefina Martínez" in confirm2.reply


def test_thanks_addresses_profile_name_not_attendee_customer_name():
    client = _client()
    phone = "525512345678"
    clear_sessions()
    handle_ticket_message(phone, "boletos", "Sergio", client)
    handle_ticket_message(phone, "codi:date:2026-11-15", "Sergio", client)
    handle_ticket_message(phone, "codi:event:evt-1", "Sergio", client)
    handle_ticket_message(phone, "codi:ticket:tt-1", "Sergio", client)
    handle_ticket_message(phone, "2", "Sergio", client)
    # Tickets are registered for Lucia Giménez
    handle_ticket_message(phone, "A nombre de Lucia Giménez", "Sergio", client)

    # When Sergio says thanks, the bot must address Sergio, NOT Lucia Giménez
    thanks = handle_ticket_message(phone, "Gracias", "Sergio", client)
    assert "Sergio" in thanks.reply
    assert "Lucia" not in thanks.reply


def test_user_says_no_tengo_codi_in_confirm_step():
    client = _client()
    phone = "525512345678"
    clear_sessions()
    handle_ticket_message(phone, "boletos", "Sergio", client)
    handle_ticket_message(phone, "codi:date:2026-11-15", "Sergio", client)
    handle_ticket_message(phone, "codi:event:evt-1", "Sergio", client)
    handle_ticket_message(phone, "codi:ticket:tt-1", "Sergio", client)
    handle_ticket_message(phone, "1", "Sergio", client)
    handle_ticket_message(phone, "Sergio", "Sergio", client)

    # In confirm step, user says "No tengo CODI"
    reply = handle_ticket_message(phone, "No tengo CODI", "Sergio", client)
    assert reply.active_use_case == "CODI-TICKETS-FAQ"
    assert "SPEI" in reply.reply or "transferencia" in reply.reply.lower()
    session = _SESSIONS.get(phone)
    # Verify session step was NOT reset to dates
    assert session.step == "confirm"
    # Verify step hint preserves order details
    from app.tickets.flow import _step_hint
    assert "Tu apartado sigue listo" in _step_hint(session)
