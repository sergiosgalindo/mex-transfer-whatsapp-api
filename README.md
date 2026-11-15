# mex-transfer-whatsapp-api

WhatsApp bridge for the CoDi event-ticket checkout. This process is the only one that talks to Meta. `mex-transfer-api` owns orders, prices, and Pey. `mex-transfer-web` is the organizer UI.

A person writes `boletos`, chooses a date, an event, a ticket type, and a quantity, then confirms their name and phone. The bot sends the CoDi charge QR and the payment link. After Pey confirms the payment, the core calls `POST /internal/send-ticket` and this service sends the entry QR.

Buyer-facing messages stay in Spanish. Event questions are answered only from the published event sheet. If the answer is not there, the bot says so.

## Run

```bash
pip install -r requirements-dev.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

`GET /api/health` returns `{"status":"UP"}`.

## Routes

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/webhook` | Meta verification |
| `POST` | `/webhook` | Incoming WhatsApp messages |
| `POST` | `/internal/send-ticket` | Entry QR after a paid order. Idempotent by `orderId` |
| `POST` | `/tickets/chat` | Same conversation, without Meta, for local checks |

## Configuration

Copy `.env.example`. Secrets stay in the environment.

| Variable | Default | Purpose |
|---|---|---|
| `CODI_CORE_API_URL` | `http://localhost:8080` | `mex-transfer-api` |
| `INTERNAL_API_TOKEN` | empty | Sent as `X-Internal-Token` when the core requires it |
| `WHATSAPP_ACCESS_TOKEN` | empty | Meta token. Without it, sends are simulated |
| `PHONE_NUMBER_ID` | empty | WhatsApp phone number id |
| `WHATSAPP_VERIFY_TOKEN` | `verify-token-123` | Meta webhook verification |
| `GROQ_API_KEY` | empty | Optional. Rewrites event answers. The facts still come from the event sheet |

## Tests

```bash
pytest tests/test_ticket_checkout.py
```

The charge PNG decodes to the CoDi `qrString`. The entry PNG decodes the ticket JSON. A second `POST /internal/send-ticket` for the same order does not send another image.
