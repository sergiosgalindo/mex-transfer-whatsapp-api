import json
import os
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import httpx
from app.models.schemas import ChatResponse, RetrievedSource
from app.tickets.core_client import CoreApi
from app.tickets.qr_images import render_qr_png

_SESSIONS: Dict[str, "TicketSession"] = {}
_TRIGGERS = {
    "boletos",
    "boleto",
    "eventos",
    "evento",
    "entradas",
    "entrada",
    "hola",
    "buenas",
    "info",
    "informes",
    "reservacion",
    "reservación",
    "reservar",
}
_THANKS = {"gracias", "muchas gracias", "mil gracias", "perfecto gracias", "ok gracias"}
_WORD_NUMBERS = {
    "un": 1,
    "uno": 1,
    "una": 1,
    "dos": 2,
    "tres": 3,
    "cuatro": 4,
    "cinco": 5,
    "seis": 6,
    "siete": 7,
    "ocho": 8,
    "nueve": 9,
    "diez": 10,
}
_CHECKOUT_RULES = (
    (
        ("activar codi", "como uso codi", "cómo uso codi", "tutorial codi", "que es codi", "qué es codi", "como activo codi", "cómo activo codi"),
        "CoDi® es el sistema oficial y gratuito de Banco de México; viene integrado en tu app bancaria y se activa en solo 1 minuto sin comisiones. Puedes ver el tutorial oficial de Banxico aquí: https://www.banxico.org.mx/sistemas-de-pago/codi-cobro-digital-banco-me.html ✨",
    ),
    (
        ("spei", "transferencia", "transferir", "clabe", "datos de cuenta", "cuenta bancaria", "no tengo codi", "no me funciona codi", "no sirve codi", "no pude con codi", "otra forma de pago", "no quiero codi", "deposito", "depósito"),
        "¡No te preocupes! Si por alguna razón se te complica o prefieres no usar CoDi®, con mucho gusto te apoyamos con una *transferencia bancaria SPEI* directa. Solo escribe *asesor* para proporcionarte los datos de la cuenta y registrar tu pago de inmediato. 😊",
    ),
    (
        ("pagar", "pago", "vigencia", "expira", "minutos"),
        "¡Claro! 😊 Tienes *10 minutos* para completar tu pago desde que generamos tu QR de CoDi® o tu enlace; durante ese tiempo tus lugares quedan apartados para ti.",
    ),
    (
        ("puerta", "ingreso", "escaneo", "entrar"),
        "Tu código QR de entrada es estilo cine 🎟️: con un solo escaneo en la puerta ingresan todas las entradas incluidas en tu orden.",
    ),
)

_PEY_BANK_ACCOUNT_CACHE: Optional[Dict[str, str]] = None


def get_spei_bank_account_from_pey() -> Optional[Dict[str, str]]:
    global _PEY_BANK_ACCOUNT_CACHE
    if _PEY_BANK_ACCOUNT_CACHE is not None:
        return _PEY_BANK_ACCOUNT_CACHE

    bank_account_id = os.getenv("PEY_BANK_ACCOUNT_ID", "").strip()
    pey_key = os.getenv("PEY_API_KEY", "").strip()
    pey_base_url = os.getenv("PEY_BASE_URL", "https://api.pey.mx").rstrip("/")

    if bank_account_id and pey_key:
        try:
            resp = httpx.get(
                f"{pey_base_url}/bank-account/{bank_account_id}",
                headers={"Authorization": f"Bearer {pey_key}"},
                timeout=8.0,
            )
            if resp.status_code == 200:
                data = resp.json()
                if data.get("clabe"):
                    _PEY_BANK_ACCOUNT_CACHE = {
                        "bank": str(data.get("alias") or "Banco"),
                        "clabe": str(data.get("clabe")),
                        "holder": str(data.get("name") or "CoDi Eventos"),
                    }
                    print(f"[Pey Bank Account] loaded from Pey API: {_PEY_BANK_ACCOUNT_CACHE['bank']} {data.get('clabe')[:6]}...", flush=True)
                    return _PEY_BANK_ACCOUNT_CACHE
        except Exception as exc:
            print(f"[Pey Bank Account] failed to fetch bank account: {exc}", flush=True)

    clabe = os.getenv("ORGANIZER_SPEI_CLABE", "").strip()
    if clabe:
        _PEY_BANK_ACCOUNT_CACHE = {
            "bank": os.getenv("ORGANIZER_SPEI_BANK", "Banco").strip(),
            "clabe": clabe,
            "holder": os.getenv("ORGANIZER_SPEI_BENEFICIARY", "CoDi Eventos").strip(),
        }
        return _PEY_BANK_ACCOUNT_CACHE

    return None


def _spei_account_info() -> Optional[str]:
    acc = get_spei_bank_account_from_pey()
    if acc:
        return (
            f"🏦 *Banco:* {acc['bank']}\n"
            f"💳 *CLABE:* `{acc['clabe']}`\n"
            f"👤 *Beneficiario:* {acc['holder']}"
        )
    return None


@dataclass
class TicketSession:
    step: str = "dates"
    date: Optional[str] = None
    event_id: Optional[str] = None
    event_name: Optional[str] = None
    event_description: str = ""
    ticket_id: Optional[str] = None
    ticket_name: Optional[str] = None
    unit_price: Optional[str] = None
    quantity: Optional[int] = None
    customer_name: Optional[str] = None
    profile_name: Optional[str] = None
    events: List[Dict[str, Any]] = field(default_factory=list)
    history: List[Dict[str, str]] = field(default_factory=list)


_TICKET_SYSTEM_PROMPT = (
    "# ROL Y PERSONALIDAD\n"
    "Eres Valeria, asesora especialista en atención al cliente y venta de boletos oficiales para CoDi Eventos. "
    "Tu personalidad es sumamente amable, cortés, cálida y empática. Te comunicas en español de México con un tono "
    "cercano, humano y profesional, haciendo que el usuario se sienta bienvenido, escuchado y acompañado en todo momento.\n\n"
    "# OBJETIVO PRINCIPAL\n"
    "Ayudar a los usuarios a resolver cualquier duda sobre los eventos disponibles y guiarlos con calidez paso a paso "
    "para que completen su apartado y pago oficial mediante CoDi® (Cobro Digital) de forma fácil, rápida, segura y sin comisiones de boletera.\n\n"
    "# INSTRUCCIONES DE INTERACCIÓN\n"
    "1. Saludo cálido: Inicia siempre con calidez y entusiasmo, usando el nombre del cliente si lo conoces y poniéndote a su disposición.\n"
    "2. Claridad y empatía: Si el usuario tiene dudas o inquietudes sobre el evento o el pago CoDi®, responde con paciencia, tranquilidad y sin tecnicismos complejos; explica todo de manera sencilla y transparente.\n"
    "3. Guía paso a paso concisa: No abrumes con muros de texto. Responde en 2 a 3 oraciones breves, naturales y bien estructuradas. "
    "Cuando el sistema adjunte botones interactivos de WhatsApp, invita con amabilidad a usarlos en lugar de listar textos largos.\n"
    "4. Cierre proactivo: Varía tus expresiones de cortesía (ej. 'Con mucho gusto', 'Será un placer ayudarte', 'Estoy aquí para apoyarte').\n\n"
    "# FLUJO DE ATENCIÓN Y SOPORTE EN PAGOS\n"
    "- Consultas sobre eventos: Proporciona detalles claros sobre fechas, horarios, recinto, tipos de acceso y precios usando la información oficial del contexto RAG.\n"
    "- Proceso de compra: Explica cómo seleccionar fechas, tipo de boleto, cantidad e ingresar su nombre completo para el acceso oficial.\n"
    "- Método principal (CoDi®):\n"
    "  * El pago principal se realiza mediante CoDi® sin comisiones de boletera. El comprador tiene 10 minutos de vigencia.\n"
    "  * Se puede pagar de 2 formas: 1) Escaneando el código QR CoDi® desde la app de su banco (BBVA, Banorte, Santander, Azteca, etc.), o 2) Abriendo el enlace directo en su celular que conecta con su app bancaria.\n"
    "  * Si el usuario no sabe qué es CoDi® o cómo activarlo en su banca móvil, explícale que es oficial de Banco de México, 100% seguro y gratuito, y compártele el enlace oficial de Banxico con tutoriales paso a paso: https://www.banxico.org.mx/sistemas-de-pago/codi-cobro-digital-banco-me.html\n"
    "- Alternativa de respaldo (Transferencia SPEI si CoDi® no funciona o el usuario no desea CoDi):\n"
    "  * Si el usuario no tiene CoDi®, no lo quiere usar o tiene dificultades técnicas, tranquilízalo con empatía: dile que sus lugares están protegidos y que con mucho gusto puede realizar una transferencia bancaria SPEI directa. Si tenemos configurada una CLABE oficial compártela con banco, CLABE, titular y concepto, o indícale que escriba 'asesor' para brindarle los datos bancarios y registrar su comprobante de inmediato.\n"
    "- Entrega de accesos: En cuanto el pago se confirma en segundos, el sistema envía su boleto digital con código QR de acceso único directo a este chat de WhatsApp.\n\n"
    "# REGLAS Y LÍMITES (GUARDRAILS)\n"
    "- Usa EXCLUSIVAMENTE los datos verificados del CONTEXTO RAG DEL EVENTO.\n"
    "- NUNCA inventes precios, promociones, recintos ni políticas que no estén en tu información oficial.\n"
    "- Si el usuario reporta un problema que requiere revisión manual, discúlpate sinceramente y ofrece comunicarlo con un especialista indicando que escriba 'asesor'."
)


def _norm_text(s: str) -> str:
    decomposed = unicodedata.normalize("NFD", (s or "").lower().strip())
    return "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn")


_EVENT_KB_FILE = os.path.join(os.path.dirname(__file__), "event_knowledge_base.json")


def _load_event_kb() -> List[RetrievedSource]:
    sources: List[RetrievedSource] = []
    if os.path.exists(_EVENT_KB_FILE):
        try:
            with open(_EVENT_KB_FILE, "r", encoding="utf-8") as f:
                raw_items = json.load(f)
                for item in raw_items:
                    kws = " ".join(item.get("keywords") or [])
                    sources.append(
                        RetrievedSource(
                            id=item.get("id") or "RAG-KB",
                            category=item.get("category") or "Información del Evento",
                            question=f"{item.get('question')} {kws}".strip(),
                            answer=item.get("answer") or "",
                            score=0.95,
                        )
                    )
        except Exception as exc:
            print(f"[EventKB Load Warning] {exc!r}", flush=True)
    return sources


def _build_event_rag_chunks(client: CoreApi, session: "TicketSession") -> List[RetrievedSource]:
    chunks: List[RetrievedSource] = []
    if not _is_mock_client(client):
        chunks.extend(_load_event_kb())
    if not chunks:
        chunks = [
            RetrievedSource(
                id="RAG-CODI-RULE-HOLD",
                category="Reglas de Pago CoDi",
                question="¿Cuánto tiempo tengo para pagar el QR CoDi o enlace y cuánto dura el apartado?",
                answer="El cobro CoDi y el apartado de tus entradas duran 10 minutos a partir de que se genera el QR.",
                score=0.95,
            ),
            RetrievedSource(
                id="RAG-CODI-RULE-DOOR",
                category="Acceso y Puerta",
                question="¿Cómo funciona el QR de entrada en la puerta del evento?",
                answer="El QR de entrada es estilo cine y admite un solo ingreso grupal en puerta para todas las entradas de tu orden.",
                score=0.95,
            ),
        ]
    events_list = list(session.events or [])
    if not events_list:
        try:
            events_list = client.get("/api/v1/events") or []
        except Exception:
            events_list = []
    for ev in events_list:
        event_id = str(ev.get("id") or "evt")
        event_name = str(ev.get("name") or "Evento")
        event_venue = str(ev.get("venue") or "")
        event_description = str(ev.get("description") or "")
        event_starts = _format_iso_cdmx(ev.get("startsAt"))
        tickets_desc = ", ".join(
            f"{t.get('name')}: ${t.get('currentPrice')} MXN"
            for t in (ev.get("ticketTypes") or [])
        )
        content = (
            f"Evento: {event_name}. Lugar/Sede/Dirección: {event_venue}. "
            f"Fecha/Hora de inicio: {event_starts}. "
            f"Boletos disponibles: {tickets_desc}. "
            f"Detalles, flyer, reglas y programa del evento: {event_description}"
        )
        chunks.append(
            RetrievedSource(
                id=f"RAG-EVENT-{event_id[:8]}",
                category=f"Ficha del Evento: {event_name}",
                question=f"Información general, detalles, flyer, DJs, reglas, boletos, sede, dirección, ubicación, horarios de {event_name}",
                answer=content,
                score=0.92,
            )
        )
    if session.event_description and not any(session.event_description in c.answer for c in chunks):
        chunks.append(
            RetrievedSource(
                id="RAG-ACTIVE-EVENT",
                category=f"Ficha del Evento: {session.event_name or 'Actual'}",
                question=f"Detalles específicos de {session.event_name or 'evento'}",
                answer=session.event_description,
                score=0.95,
            )
        )
    return chunks


_SYNONYM_MAP: Dict[str, List[str]] = {
    "donde": ["direccion", "ubicacion", "sede", "lugar", "berraca", "reforma"],
    "dónde": ["direccion", "ubicacion", "sede", "lugar", "berraca", "reforma"],
    "direccion": ["ubicacion", "sede", "lugar", "berraca", "reforma", "llegar", "mapa"],
    "dirección": ["ubicacion", "sede", "lugar", "berraca", "reforma", "llegar", "mapa"],
    "ubicacion": ["direccion", "sede", "lugar", "berraca", "reforma", "llegar", "mapa"],
    "ubicación": ["direccion", "sede", "lugar", "berraca", "reforma", "llegar", "mapa"],
    "alcohol": ["bebidas", "descorche", "cerveza", "tragos", "barra"],
    "alchool": ["bebidas", "descorche", "cerveza", "tragos", "barra"],
    "descorche": ["bebidas", "alcohol", "botellas", "alimentos", "ingresar"],
    "cerveza": ["bebidas", "alcohol", "barra", "chelas"],
    "tragos": ["bebidas", "alcohol", "barra", "cocteleria"],
    "estacionamiento": ["parking", "valet", "coche", "auto", "pension"],
    "estacionar": ["estacionamiento", "parking", "valet", "coche", "auto"],
    "clases": ["clase", "taller", "baile", "aprender"],
    "clase": ["clases", "taller", "baile", "aprender"],
    "taller": ["clase", "clases", "baile", "aprender"],
    "termina": ["fin", "cierre", "horario", "duracion", "noche"],
    "fin": ["termina", "cierre", "horario", "duracion"],
    "cierre": ["termina", "fin", "horario", "duracion"],
    "empieza": ["inicio", "horario", "apertura", "comienza"],
    "inicio": ["empieza", "horario", "apertura", "comienza"],
    "horarios": ["horario", "hora", "empieza", "inicio", "termina", "fin", "apertura", "cierre", "duracion"],
    "horario": ["horarios", "hora", "empieza", "inicio", "termina", "fin", "apertura", "cierre", "duracion"],
    "precios": ["precio", "costo", "boletos", "entradas", "valor", "tarifa", "boleto", "general"],
    "precio": ["precios", "costo", "boletos", "entradas", "valor", "tarifa", "boleto", "general"],
    "costo": ["precio", "precios", "boletos", "entradas", "valor", "tarifa"],
    "cuanto": ["precio", "precios", "costo", "boletos", "entradas"],
    "cuánto": ["precio", "precios", "costo", "boletos", "entradas"],
}


def _retrieve_relevant_chunks(
    query: str,
    chunks: List[RetrievedSource],
    session: Optional["TicketSession"] = None,
) -> List[RetrievedSource]:
    norm_q = _norm_text(query)
    raw_tokens = [tok for tok in re.findall(r"[a-z0-9]{3,}", norm_q) if tok not in {"hay", "que", "como", "para", "del", "las", "los", "una", "con", "por"}]
    if not raw_tokens:
        return []
    q_tokens = set(raw_tokens)
    for tok in raw_tokens:
        if tok in _SYNONYM_MAP:
            for syn in _SYNONYM_MAP[tok]:
                q_tokens.add(_norm_text(syn))

    matched: List[Tuple[float, RetrievedSource]] = []
    active_ev_id = session.event_id if session else None
    active_ev_name = _norm_text(session.event_name or "") if session else ""

    for ch in chunks:
        haystack = _norm_text(f"{ch.category} {ch.question} {ch.answer}")
        hits = sum(1 for tok in q_tokens if tok in haystack)
        if hits > 0:
            score = round(min(0.99, 0.5 + (hits / len(q_tokens)) * 0.49), 3)
            if active_ev_id and active_ev_id[:8] in ch.id:
                score = min(0.99, score + 0.15)
            elif active_ev_name and active_ev_name in _norm_text(ch.category):
                score = min(0.99, score + 0.10)
            matched.append((score, RetrievedSource(
                id=ch.id,
                category=ch.category,
                question=ch.question,
                answer=ch.answer,
                score=score,
            )))
    matched.sort(key=lambda x: x[0], reverse=True)
    return [m[1] for m in matched[:4]]


def _call_ticket_llm(
    user_text: str,
    session: "TicketSession",
    sources: List[RetrievedSource],
    step_instruction: str,
    is_mock_test: bool = False,
) -> Optional[Tuple[str, str]]:
    if is_mock_test or os.getenv("DISABLE_TICKET_LLM", "false").lower() == "true":
        return None
    groq_key = os.getenv("GROQ_API_KEY", "").strip()
    llm_url = os.getenv("LLM_BASE_URL", "").strip()
    if not llm_url:
        if groq_key:
            llm_url = "https://api.groq.com/openai/v1/chat/completions"
        else:
            llm_url = "http://localhost:11434/v1/chat/completions"

    model_name = os.getenv("GROQ_MODEL") or os.getenv("LLM_MODEL") or ("openai/gpt-oss-20b" if groq_key else "llama3.1:8b")
    headers: Dict[str, str] = {"Content-Type": "application/json"}
    if groq_key and ("groq.com" in llm_url or not llm_url):
        headers["Authorization"] = f"Bearer {groq_key}"

    rag_block = "\n\n".join(f"[{s.id}] ({s.category}): {s.answer}" for s in sources) if sources else "Sin datos adicionales."
    messages: List[Dict[str, str]] = [
        {"role": "system", "content": _TICKET_SYSTEM_PROMPT},
        {
            "role": "system",
            "content": (
                f"CLIENTE: {session.customer_name or session.profile_name or 'Invitado'}\n"
                f"PASO ACTUAL DEL FLUJO: {session.step}\n"
                f"CONTEXTO RAG VERIFICADO:\n{rag_block}\n\n"
                f"INSTRUCCIÓN DE ESTE TURNO:\n{step_instruction}"
            ),
        },
    ]
    for h in session.history[-4:]:
        messages.append(h)
    messages.append({"role": "user", "content": user_text})

    try:
        resp = httpx.post(
            llm_url,
            headers=headers,
            json={
                "model": model_name,
                "messages": messages,
                "temperature": 0.35,
                "max_tokens": 180,
            },
            timeout=12.0,
        )
        if resp.status_code == 200:
            content = ((resp.json().get("choices") or [{}])[0].get("message") or {}).get("content", "").strip()
            if content:
                print(f"[TicketFlow LLM+RAG] model={model_name} sources={[s.id for s in sources]} reply={content!r}", flush=True)
                return content, model_name
    except Exception as exc:
        print(f"[TicketFlow LLM Fallback] primary err={exc!r}", flush=True)

    if groq_key and model_name != "qwen/qwen3.8-27b":
        try:
            resp2 = httpx.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {groq_key}", "Content-Type": "application/json"},
                json={
                    "model": "qwen/qwen3.8-27b",
                    "messages": messages,
                    "temperature": 0.35,
                    "max_tokens": 120,
                },
                timeout=8.0,
            )
            if resp2.status_code == 200:
                content2 = ((resp2.json().get("choices") or [{}])[0].get("message") or {}).get("content", "").strip()
                if content2:
                    print(f"[TicketFlow LLM+RAG] model=groq/qwen sources={[s.id for s in sources]} reply={content2!r}", flush=True)
                    return content2, "qwen/qwen3.8-27b"
        except Exception as exc2:
            print(f"[TicketFlow LLM Fallback] secondary err={exc2!r}", flush=True)
    return None


def clear_sessions() -> None:
    _SESSIONS.clear()


def _is_greeting(lowered: str) -> bool:
    return any(g in lowered for g in (
        "hola", "buen dia", "buenos dias", "buenas tardes", "buenas noches",
        "que tal", "qué tal", "como estas", "cómo estás", "como te va", "cómo te va",
        "que onda", "qué onda"
    ))


def _is_help(lowered: str) -> bool:
    return any(p in lowered for p in (
        "ayuda", "necesito ayuda", "como funciona", "cómo funciona",
        "de que trata", "de qué trata", "como compro", "cómo compro",
        "que es esto", "qué es esto", "que es codi", "qué es codi"
    ))


def _is_event_discovery(lowered: str) -> bool:
    return any(p in lowered for p in (
        "que eventos", "qué eventos", "que hay", "qué hay",
        "que tienen", "qué tienen", "cartelera", "que fiesta", "qué fiesta",
        "proximos eventos", "próximos eventos", "eventos disponibles",
        "quiero ir", "tienen eventos", "hay eventos"
    ))


def _looks_like_question(lowered: str) -> bool:
    if _is_greeting(lowered) or _is_help(lowered) or _is_event_discovery(lowered):
        return False
    if lowered.startswith((
        "hay ", "donde ", "dónde ", "a que hora", "a qué hora", "cuanto ", "cuánto ",
        "quien ", "quién ", "puedo ", "entran ", "que ", "qué ", "como ", "cómo ",
        "cual ", "cuál ", "tienen ", "se puede ", "duda", "tienes ", "venden ",
        "habra ", "habrá ", "tendra ", "tendrá ", "venderan ", "venderán ", "pueden ",
        "se podra ", "se podrá ", "se permite ", "cuentan con ",
    )):
        return True
    faq_keywords = (
        "estacionamiento", "estacionar", "valet", "parking", "coche", "coches", "auto", "autos", "carros",
        "ubicacion", "ubicación", "direccion", "dirección", "lugar", "sede", "llegar", "mapa",
        "horario", "horarios", "empieza", "inicio", "comienza", "termina", "fin", "cierre",
        "clase", "clases", "taller", "aprender",
        "descorche", "alcohol", "alchool", "cerveza", "trago", "tragos", "bebida", "bebidas", "tomar", "barra",
        "comida", "alimento", "alimentos", "cenar", "botana",
        "cumple", "cumpleaños", "cumpleañero", "cumpleañera",
        "vestimenta", "ropa", "zapatos",
        "cover", "dj", "djs", "vigencia", "expira", "puerta", "acceso",
        "spei", "transferir", "transferencia", "clabe", "deposito", "depósito",
    )
    pattern = r"\b(" + "|".join(re.escape(k) for k in faq_keywords) + r")\b"
    return bool(re.search(pattern, lowered))


def _is_conversational_follow_up(lowered: str, session: TicketSession) -> bool:
    if not session.history:
        return False
    norm = _norm_text(lowered)
    affirmatives = {
        "si", "si por favor", "si porfa", "si porfavor", "por favor", "porfa", "porfavor",
        "claro", "claro que si", "adelante", "va", "dale",
        "me gustaria", "me gustaria saber", "me gustaria conocer", "me interesa",
        "quiero saber", "dime", "cuentame", "a ver", "ambos", "ambas",
        "los dos", "los horarios", "los precios", "horarios", "precios",
        "horario", "precio", "si ambos", "si los dos", "si quiero", "si me gustaria",
        "los dos por favor", "ambos por favor", "los horarios por favor", "los precios por favor",
    }
    if norm in affirmatives:
        return True
    if any(norm.startswith(a) or norm.endswith(a) for a in (
        "si por favor", "por favor", "me gustaria", "quiero saber", "claro que si", "los horarios", "los precios"
    )):
        return True
    last_assistant = next((h["content"] for h in reversed(session.history) if h.get("role") == "assistant"), "")
    if ("?" in last_assistant or "¿" in last_assistant) and norm in {
        "si", "va", "dale", "ok", "bueno", "de acuerdo", "sale", "simon", "sii", "si!"
    }:
        return True
    return False


def _step_back(client: CoreApi, session: TicketSession) -> ChatResponse:
    if session.step in ("confirm", "name", "paid") and session.ticket_id:
        return _ask_quantity(session, session.ticket_id)
    if session.step == "quantity" and session.event_id:
        return _show_tickets(client, session, session.event_id, send_flyer=False)
    if session.step == "tickets" and session.date:
        return _show_events(client, session, session.date)
    session.step = "dates"
    return _show_dates(client, session, "boletos")


def _try_fast_forward(client: CoreApi, session: TicketSession, text: str) -> Optional[ChatResponse]:
    norm = _norm_text(text)
    if norm in _TRIGGERS or len(norm) < 6:
        return None
    stop_tokens = {
        "de", "del", "la", "el", "en", "los", "las", "para", "por", "un", "una",
        "evento", "eventos", "boleto", "boletos", "entrada", "entradas", "quiero",
    }
    try:
        all_events = client.get("/api/v1/events") or []
    except Exception:
        all_events = []
    for ev in all_events:
        event_name_norm = _norm_text(str(ev.get("name") or ""))
        event_tokens = [t for t in re.findall(r"[a-z0-9]{4,}", event_name_norm) if t not in stop_tokens]
        if event_tokens and any(tok in norm for tok in event_tokens):
            session.events = all_events
            return _show_tickets(client, session, str(ev["id"]), send_flyer=True)

    try:
        dates = (client.get("/api/v1/events/upcoming-dates") or {}).get("dates") or []
    except Exception:
        dates = []
    for day_iso in dates:
        try:
            day_num = int(str(day_iso)[-2:])
        except Exception:
            continue
        if re.search(rf"\b(?:el|del|dia|para el)\s+0?{day_num}\b", norm) or re.search(rf"\b0?{day_num}\s+de\b", norm):
            return _show_events(client, session, str(day_iso))
    return None


def _extract_quantity(text: str) -> Optional[int]:
    cleaned = text.strip().lower()
    if cleaned.isdigit():
        return int(cleaned)
    nums = re.findall(r"\b(\d+)\b", cleaned)
    if nums:
        return int(nums[-1])
    words = [w for w in re.findall(r"[a-záéíóúñ]+", cleaned) if w in _WORD_NUMBERS]
    if words:
        return _WORD_NUMBERS[words[-1]]
    return None


def _clean_customer_name(text: str) -> str:
    cleaned = text.strip().strip("\"'").strip().rstrip(".")
    lowered = cleaned.lower()
    prefixes = [
        "dejalo a nombre de ", "déjalo a nombre de ", "ponlo a nombre de ",
        "a nombre de ", "a nombre ", "nombre de ", "para ",
        "me llamo ", "mi nombre es ", "seria a nombre de ", "sería a nombre de ",
        "seria para ", "sería para ", "seria ", "sería ", "ponle ",
        "nombre: ", "titular: ", "a mi nombre ",
    ]
    for p in prefixes:
        if lowered.startswith(p):
            cleaned = cleaned[len(p):].strip().rstrip(".")
            lowered = cleaned.lower()
    return cleaned.title() if cleaned.islower() else cleaned


def maybe_handle_ticket(phone: str, text: str, profile_name: str = "", api: Optional[CoreApi] = None) -> Optional[ChatResponse]:
    cleaned = (text or "").strip()
    if not cleaned:
        return None
    lowered = cleaned.lower()
    # Banking inquiries when not in an active ticket checkout
    if any(w in lowered for w in ("saldo disponible", "mi saldo", "estado de cuenta", "prestamo", "préstamo", "mi credito")) and phone not in _SESSIONS:
        return None
    if lowered in {"salir", "asesor", "humano"}:
        _SESSIONS.pop(phone, None)
        name_part = f", {profile_name}" if profile_name else ""
        return _reply(
            f"¡Listo{name_part}! Cerramos el asistente de boletos por ahora. 😊 Si necesitas hablar con alguien del equipo, escríbenos tu duda aquí abajo y con gusto te atendemos.",
            "CODI-TICKETS-EXIT",
        )
    if any(p in lowered for p in ("ya no quiero nada", "no quiero nada", "cancelar", "olvidalo", "olvídalo", "mejor ya no")):
        _SESSIONS.pop(phone, None)
        name_part = f", {profile_name}" if profile_name else ""
        return _reply(
            f"¡No te preocupes{name_part}! Cancelamos el apartado por ahora. 😊 Cuando quieras volver a ver fechas o apartar entradas, solo escríbeme *boletos*.",
            "CODI-TICKETS-EXIT",
        )
    return handle_ticket_message(phone, cleaned, profile_name, api or CoreApi())


def handle_ticket_message(phone: str, text: str, profile_name: str = "", api: Optional[CoreApi] = None) -> ChatResponse:
    client = api or CoreApi()
    session = _SESSIONS.setdefault(phone, TicketSession(profile_name=profile_name or None))
    if profile_name and not session.profile_name:
        session.profile_name = profile_name
    display_name = session.customer_name or session.profile_name
    try:
        lowered = text.lower().strip()
        if lowered in {"salir", "asesor", "humano"}:
            _SESSIONS.pop(phone, None)
            name_part = f", {display_name}" if display_name else ""
            return _reply(
                f"¡Listo{name_part}! Cerramos el asistente de boletos por ahora. 😊 Si necesitas hablar con alguien del equipo, escríbenos tu duda aquí abajo y con gusto te atendemos.",
                "CODI-TICKETS-EXIT",
            )
        if any(p in lowered for p in ("ya no quiero nada", "no quiero nada", "cancelar", "olvidalo", "olvídalo", "mejor ya no")):
            _SESSIONS.pop(phone, None)
            name_part = f", {display_name}" if display_name else ""
            return _reply(
                f"¡No te preocupes{name_part}! Cancelamos el apartado por ahora. 😊 Cuando quieras volver a ver fechas o apartar entradas, solo escríbeme *boletos*.",
                "CODI-TICKETS-EXIT",
            )
        if lowered in _THANKS:
            name_part = f", {display_name}" if display_name else ""
            return _reply(
                f"¡Con muchísimo gusto{name_part}! 😊✨ Cuando quieras ver más fechas o apartar otros boletos, solo escríbeme *boletos*. ¡Nos vemos pronto!",
                "CODI-TICKETS-THANKS",
            )
        if text == "codi:restart":
            session.step = "dates"
            return _show_dates(client, session, text)
        if text.startswith("codi:date:"):
            return _show_events(client, session, text.split(":", 2)[2])
        if text.startswith("codi:event:"):
            return _show_tickets(client, session, text.split(":", 2)[2])
        if text.startswith("codi:ticket:"):
            return _ask_quantity(session, text.split(":", 2)[2])
        if text == "codi:confirm:no":
            session.step = "tickets"
            return _show_tickets(client, session, session.event_id or "", send_flyer=False)
        if text == "codi:confirm:qty":
            session.step = "quantity"
            return _reply(
                "¡Claro! 😊 ¿Cuántas entradas te aparto en total? Escríbeme el número aquí abajo (por ejemplo: *1*, *2* o *4*):",
                "CODI-TICKETS-QUANTITY",
            )
        if text == "codi:confirm:name":
            session.step = "name"
            return _reply(
                "¡Claro! 😊 Escribe por favor el *nombre y apellido* de la persona a nombre de quien saldrán los boletos:",
                "CODI-TICKETS-NAME",
            )
        if text == "codi:confirm:yes":
            return _charge(client, session, phone)
        if session.step == "name" and not text.startswith("codi:") and "?" not in text:
            if not _looks_like_question(lowered):
                if lowered in {"yo", "a mi nombre", "a mi", "el mio", "el mío", "mi nombre"} and session.profile_name:
                    name = session.profile_name.strip()
                else:
                    name = _clean_customer_name(text)
                if len(name) >= 2:
                    session.customer_name = name
                    session.step = "confirm"
                    return _confirmation_card(session, phone)
                return _reply(
                    "Por favor escribe un *nombre y apellido* válido para emitir tus accesos: 😊",
                    "CODI-TICKETS-NAME",
                )
        if session.step == "confirm" and lowered in {
            "si", "sí", "confirmo", "confirmar", "si confirmo", "sí confirmo", "adelante", "va", "listo", "generar cobro"
        }:
            return _charge(client, session, phone)
        if lowered in {"atras", "atrás", "regresar", "volver", "anterior"}:
            return _step_back(client, session)
        if any(w in lowered for w in ("cambiar evento", "otro evento", "me equivoque de evento", "me equivoqué de evento")) and session.date:
            return _show_events(client, session, session.date)
        if any(w in lowered for w in ("cambiar fecha", "otra fecha", "me equivoque de fecha", "me equivoqué de fecha")):
            session.step = "dates"
            return _show_dates(client, session, text)
        if any(w in lowered for w in ("cambiar boleto", "otro boleto", "cambiar acceso")) and session.event_id:
            session.step = "tickets"
            return _show_tickets(client, session, session.event_id, send_flyer=False)
        if session.step in ("quantity", "confirm", "paid") and "?" not in text and not _looks_like_question(lowered):
            qty = _extract_quantity(text)
            if qty is not None:
                return _ask_confirmation(session, phone, qty)
        if session.step in ("confirm", "paid", "quantity") and not text.startswith("codi:") and "?" not in text:
            if any(w in lowered for w in ("cambiar nombre", "otro nombre", "cambiar titular", "otro titular")) or ("nombre" in lowered and session.step == "confirm"):
                session.step = "name"
                return _reply(
                    "¡Claro! 😊 Escribe por favor el *nombre y apellido* de la persona a nombre de quien saldrán los boletos:",
                    "CODI-TICKETS-NAME",
                )
            if lowered in {"no", "cambiar", "cambiar cantidad", "otra cantidad", "modificar", "me equivoque", "me equivoqué"} or "cantidad" in lowered:
                session.step = "quantity"
                return _reply(
                    "¡No te preocupes! 😊 ¿Cuántas entradas te aparto en total? Escríbeme el número aquí abajo (por ejemplo: *1*, *2* o *4*):",
                    "CODI-TICKETS-QUANTITY",
                )
        if _is_greeting(lowered) or _is_help(lowered) or _is_event_discovery(lowered):
            has_domain_faq = any(k in lowered for k in (
                "horario", "horarios", "precio", "precios", "donde", "dónde", "estacionamiento",
                "alcohol", "clase", "clases", "cumple", "vestimenta", "spei", "transferencia"
            ))
            if not has_domain_faq:
                ff = _try_fast_forward(client, session, text)
                if ff is not None:
                    return ff
                session.step = "dates"
                return _show_dates(client, session, text)
        if _looks_like_question(lowered) or "?" in text or _is_conversational_follow_up(lowered, session):
            return _faq(client, session, text)
        if lowered in _TRIGGERS or (any(w in lowered for w in _TRIGGERS) and not text.startswith("codi:")):
            ff = _try_fast_forward(client, session, text)
            if ff is not None:
                return ff
            session.step = "dates"
            return _show_dates(client, session, text)
        if session.step == "name":
            return _reply(
                "Por favor escribe el *nombre y apellido completo* para emitir tus accesos oficiales: 😊",
                "CODI-TICKETS-NAME",
            )
        if session.step == "quantity":
            return _reply(
                "¿Me podrías decir con un número cuántas entradas te aparto? 😊 Por ejemplo, escribe *1*, *2* o *4*.",
                "CODI-TICKETS-QUANTITY",
            )
        ff = _try_fast_forward(client, session, text)
        if ff is not None:
            return ff
        return _show_dates(client, session, text)
    except Exception as exc:
        print(f"[TicketFlow Exception] phone={phone} text={text!r} step={session.step} err={exc!r}", flush=True)
        return _reply(
            "¡Uy, perdón! 🙏 Tuve un pequeño hipo conectando con el sistema. ¿Me ayudas intentando de nuevo en unos segundos?",
            "CODI-TICKETS-ERROR",
        )


from datetime import datetime
from zoneinfo import ZoneInfo

_CDMX_TZ = ZoneInfo("America/Mexico_City")
_MONTHS_ES = ("Ene", "Feb", "Mar", "Abr", "May", "Jun", "Jul", "Ago", "Sep", "Oct", "Nov", "Dic")
_DAYS_ES = ("Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom")


def format_mexico_city_day(iso_day: str) -> str:
    try:
        dt = datetime.strptime(iso_day[:10], "%Y-%m-%d")
        return f"{_DAYS_ES[dt.weekday()]} {dt.day:02d} {_MONTHS_ES[dt.month - 1]} {dt.year}"
    except Exception:
        return iso_day


def _format_iso_cdmx(iso_ts: Any) -> str:
    if not iso_ts:
        return ""
    raw = str(iso_ts)
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(_CDMX_TZ)
        return f"{_DAYS_ES[dt.weekday()]} {dt.day:02d} {_MONTHS_ES[dt.month - 1]} · {dt.strftime('%H:%M')} hrs (CDMX)"
    except Exception:
        return raw


def _is_mock_client(client: CoreApi) -> bool:
    return "core.test" in getattr(client, "base_url", "")


def _show_dates(client: CoreApi, session: TicketSession, user_text: str = "boletos") -> ChatResponse:
    payload = client.get("/api/v1/events/upcoming-dates")
    dates = payload.get("dates") or []
    if not dates:
        return _reply("Por ahora no tenemos fechas con boletos disponibles, pero muy pronto abriremos nuevas fechas. ✨", "CODI-TICKETS-DATES")
    rows = [
        {"id": f"codi:date:{day}", "title": format_mexico_city_day(day)[:24], "description": f"Ver cartelera del {format_mexico_city_day(day)}"[:72]}
        for day in dates[:10]
    ]
    display_name = session.customer_name or session.profile_name
    greeting = f"¡Hola, *{display_name}*! 👋" if display_name else "¡Hola! 👋"
    body = (
        f"{greeting} Soy *Valeria*, tu asesora en CoDi Eventos. ¡Qué gusto saludarte! ✨\n\n"
        f"Con mucho gusto te acompaño para apartar tus accesos oficiales sin comisiones extra. "
        f"¿Qué día te gustaría acompañarnos? Toca el botón de abajo para ver las fechas disponibles:"
    )
    rag_chunks = _build_event_rag_chunks(client, session)
    event_chunks = _retrieve_relevant_chunks(user_text, rag_chunks) or [
        c for c in rag_chunks if c.id.startswith("RAG-EVENT-")
    ][:3]
    dates_human = ", ".join(f"{format_mexico_city_day(d)} (día {d[-2:].lstrip('0')})" for d in dates[:10])
    if user_text == "codi:restart":
        session.history.clear()
    elif len(session.history) > 10:
        session.history = session.history[-6:]
    lowered_q = user_text.lower().strip()
    if _is_greeting(lowered_q):
        instruction = (
            f"El cliente acaba de saludarte diciendo: '{user_text}'. "
            f"Como Valeria, salúdalo por su nombre ({display_name or 'amigo'}) con muchísima calidez y amabilidad, preséntate brevemente como Valeria de CoDi Eventos "
            f"y dile que será un verdadero placer ayudarle con sus boletos. "
            f"Invítalo con amabilidad a tocar el botón de abajo para explorar las próximas fechas disponibles ({dates_human})."
        )
    elif _is_help(lowered_q):
        instruction = (
            f"El cliente necesita orientación y escribió: '{user_text}'. "
            f"Como Valeria, salúdalo ({display_name or 'amigo'}) con mucha calidez y tranquilidad, y explícale con sencillez que aquí puede apartar sus boletos oficiales "
            f"y pagar de forma 100% segura con CoDi® desde su banca móvil sin comisiones abusivas de boletera. "
            f"Invítalo con una sonrisa a tocar el botón de abajo para ver las fechas disponibles."
        )
    elif _is_event_discovery(lowered_q):
        instruction = (
            f"El cliente pregunta por los eventos o cartelera: '{user_text}'. "
            f"Como Valeria, salúdalo ({display_name or 'amigo'}) con entusiasmo y cuéntale de las opciones disponibles en cartelera "
            f"e invítalo a tocar el botón de abajo para elegir fecha y ver detalles y flyers de cada evento."
        )
    else:
        instruction = (
            f"El cliente acaba de escribir: '{user_text}'. "
            f"Las FECHAS OFICIALES DISPONIBLES en el botón interactivo son exactamente: {dates_human}. "
            f"Como Valeria, salúdalo por su nombre ({display_name or 'amigo'}) con mucha calidez y naturalidad, "
            f"confirma con entusiasmo la fecha si pidió alguna de las disponibles (como el día 15, 17 o 21), "
            f"NUNCA digas que hay un error en la fecha ni menciones el día 14, "
            f"e invítalo a elegir la fecha en el botón de abajo."
        )
    llm_res = _call_ticket_llm(
        user_text,
        session,
        event_chunks,
        instruction,
        is_mock_test=_is_mock_client(client),
    )
    model_used = "codi-tickets"
    if llm_res:
        body, model_used = llm_res
    session.history.append({"role": "user", "content": user_text})
    session.history.append({"role": "assistant", "content": body})
    return _menu(
        body,
        "Próximas fechas",
        rows,
        "CODI-TICKETS-DATES",
        button_label="Ver fechas",
        model_name=model_used,
        sources=event_chunks,
    )


def _show_events(client: CoreApi, session: TicketSession, day: str) -> ChatResponse:
    session.date = day
    session.events = client.get("/api/v1/events", {"date": day})
    if not session.events:
        return _reply(f"Para el {format_mexico_city_day(day)} ya no nos quedan lugares disponibles. ¿Te gustaría revisar otra fecha?", "CODI-TICKETS-EVENTS")
    rows = []
    for event in session.events[:10]:
        rows.append({
            "id": f"codi:event:{event['id']}",
            "title": str(event.get("name") or "Evento")[:24],
            "description": f"📍 {event.get('venue') or 'CDMX'}"[:72],
        })
    session.step = "events"
    display_name = session.customer_name or session.profile_name
    name_part = f", *{display_name}*" if display_name else ""
    return _menu(
        (
            f"¡Súper bien{name_part}! 🙌 Para el *{format_mexico_city_day(day)}* tenemos estos eventos listos para ti.\n\n"
            f"¿A cuál te gustaría asistir? Elíbelo aquí abajo y te comparto el flyer con todos los detalles:"
        ),
        "Cartelera",
        rows,
        "CODI-TICKETS-EVENTS",
        button_label="Ver eventos",
    )


def _extract_flyer(raw_flyer: Optional[str]) -> Optional[ Dict[str, str] ]:
    if not raw_flyer or not raw_flyer.strip():
        return None
    s = raw_flyer.strip()
    mime = "image/jpeg"
    b64 = s
    if s.startswith("data:") and "," in s:
        header, b64 = s.split(",", 1)
        if ";" in header:
            mime = header[5:].split(";", 1)[0] or "image/jpeg"
    return {"b64": b64.strip(), "mime": mime}


def _show_tickets(client: CoreApi, session: TicketSession, event_id: str, send_flyer: bool = True) -> ChatResponse:
    event = next((item for item in session.events if item.get("id") == event_id), None)
    if event is None:
        return _reply("No encontré ese evento en la lista, perdón. ¿Me ayudas eligiendo la fecha de nuevo?", "CODI-TICKETS-EVENTS")
    session.event_id = event_id
    session.event_name = event.get("name")
    session.event_description = event.get("description") or ""
    rows = []
    for ticket in event.get("ticketTypes") or []:
        available = ticket.get("available")
        if available == 0:
            continue
        price = ticket.get("currentPrice")
        rows.append({
            "id": f"codi:ticket:{ticket['id']}",
            "title": str(ticket.get("name") or "Boleto")[:24],
            "description": f"${price} MXN"[:72],
        })
    if not rows:
        return _reply("¡Uy! Ese evento se acaba de agotar. 😔 Escribe *boletos* si quieres ver otros eventos.", "CODI-TICKETS-TICKETS")
    session.step = "tickets"
    venue = event.get("venue") or ""
    flyer_info = _extract_flyer(event.get("flyerImage")) if send_flyer else None
    tool_out: Optional[Dict[str, Any]] = None
    if flyer_info:
        tool_out = {
            "event_flyer_b64": flyer_info["b64"],
            "event_flyer_mime": flyer_info["mime"],
            "event_promo_text": session.event_description or f"✨ *{session.event_name}* · {venue}",
        }
    menu_body = (
        f"¡Excelente elección! 💃🕺✨ Se va a poner buenísimo *{session.event_name}* en *{venue}*.\n\n"
        f"Aquí abajo puedes elegir tu tipo de boleto para apartar tus lugares (o si tienes alguna duda del evento, pregúntame con confianza):"
    )
    rag_chunks = _build_event_rag_chunks(client, session)
    display_name = session.customer_name or session.profile_name
    llm_res = _call_ticket_llm(
        f"Elegí el evento {session.event_name}",
        session,
        rag_chunks[:2],
        (
            f"El cliente {display_name or ''} acaba de elegir el evento '{session.event_name}' en '{venue}'. "
            f"Ya le enviamos arriba la imagen del flyer. Haz un comentario breve, cálido y entusiasta de 2 frases "
            f"sobre este evento usando algún detalle real del contexto RAG (música, ambiente o sede) e invítalo a "
            f"tocar el botón 'Elegir boleto' de aquí abajo o preguntarte cualquier duda."
        ),
        is_mock_test=_is_mock_client(client),
    )
    model_used = "codi-tickets"
    if llm_res:
        menu_body, model_used = llm_res
    return _menu(
        menu_body,
        "Tipos de boleto",
        rows,
        "CODI-TICKETS-TICKETS",
        tool_out,
        button_label="Elegir boleto",
        model_name=model_used,
        sources=rag_chunks[:2],
    )


def _ask_quantity(session: TicketSession, ticket_id: str) -> ChatResponse:
    ticket = _ticket(session, ticket_id)
    if ticket is None:
        return _reply("No encontré ese tipo de acceso. ¿Me ayudas eligiéndolo de nuevo en la lista?", "CODI-TICKETS-TICKETS")
    session.ticket_id = ticket_id
    session.ticket_name = ticket.get("name")
    session.unit_price = str(ticket.get("currentPrice"))
    session.step = "quantity"
    session.customer_name = None
    display_name = session.customer_name or session.profile_name
    name_part = f", *{display_name}*" if display_name else ""
    return _reply(
        (
            f"¡Perfecto{name_part}! 🎟️ El boleto *{session.ticket_name}* está en *`${session.unit_price} MXN`* por persona.\n\n"
            f"¿Cuántas entradas te aparto? Solo escríbeme el número aquí abajo (por ejemplo: *1*, *2* o *4*). 😊"
        ),
        "CODI-TICKETS-QUANTITY",
    )


def _ask_confirmation(session: TicketSession, phone: str, quantity: int) -> ChatResponse:
    if quantity < 1:
        return _reply("Por favor dime al menos *1* entrada para poder apartarla. 😊", "CODI-TICKETS-QUANTITY")
    ticket = _ticket(session, session.ticket_id or "")
    avail = ticket.get("available") if ticket else None
    if avail is not None and quantity > int(avail):
        return _reply(
            f"¡Uy! Esa cantidad supera los lugares disponibles para *{session.ticket_name}* en este momento. 🙏 "
            f"¿Me indicas un número menor de entradas?",
            "CODI-TICKETS-QUANTITY",
        )
    session.quantity = quantity
    if not session.customer_name:
        session.step = "name"
        qty_label = f"*{quantity}* boleto" if quantity == 1 else f"*{quantity}* boletos"
        return _reply(
            (
                f"¡Súper! 🙌 Apartamos {qty_label} para *{session.event_name}*.\n\n"
                f"¿A nombre de quién dejamos tus entradas? Escribe por favor tu *nombre y apellido completo* para tu acceso oficial en puerta:"
            ),
            "CODI-TICKETS-NAME",
        )
    session.step = "confirm"
    return _confirmation_card(session, phone)


def _confirmation_card(session: TicketSession, phone: str) -> ChatResponse:
    try:
        total = f"{float(session.unit_price or 0) * int(session.quantity or 1):.2f}"
    except Exception:
        total = str(session.unit_price)
    return _menu(
        (
            f"📋 *Resumen de tu pedido*\n"
            f"• *Evento:* {session.event_name}\n"
            f"• *Boletos:* {session.quantity} × {session.ticket_name} (`${session.unit_price} MXN` c/u)\n"
            f"• *Total a pagar:* *`${total} MXN`*\n"
            f"• *Titular:* {session.customer_name}\n"
            f"• *Teléfono:* {phone}\n\n"
            f"¿Confirmas apartar tus lugares y generar tu cobro CoDi®?"
        ),
        "Confirmar",
        [
            {"id": "codi:confirm:yes", "title": "Sí, generar cobro", "description": "Recibir QR CoDi y enlace"},
            {"id": "codi:confirm:qty", "title": "Cambiar cantidad", "description": "Modificar número de entradas"},
            {"id": "codi:confirm:name", "title": "Cambiar mi nombre", "description": "Poner los boletos a otro nombre"},
            {"id": "codi:confirm:no", "title": "No, volver", "description": "Elegir otro boleto"},
        ],
        "CODI-TICKETS-CONFIRM",
        button_label="Confirmar",
    )


def _charge(client: CoreApi, session: TicketSession, phone: str) -> ChatResponse:
    if not session.ticket_id or not session.quantity or not session.customer_name:
        return _reply("Se me perdió un dato de tu reservación, perdón. 🙏 Escribe *boletos* para elegir tus entradas rapidísimo.", "CODI-TICKETS-CONFIRM")
    charged = client.post("/api/v1/orders", {
        "ticketTypeId": session.ticket_id,
        "quantity": session.quantity,
        "customerName": session.customer_name,
        "customerPhone": phone,
    })
    png = render_qr_png(str(charged["qrString"]))
    import base64

    session.step = "paid"
    link = charged.get("paymentLinkUrl")
    exp_str = _format_iso_cdmx(charged.get("expiresAt"))
    reply = (
        f"💳 *Cobro CoDi® generado — Total: `${charged.get('amount')} MXN`*\n"
        f"🎟️ *Apartado:* {charged.get('quantity')} entrada(s) · *{session.event_name or 'Evento'}*\n"
        f"⏳ *Vence:* {exp_str}\n\n"
        f"*¿Cómo pagar?*\n"
        f"Abre la app de tu banco, entra a *CoDi® → Pagar / Escanear QR* y escanea esta imagen."
    )
    return _reply(reply, "CODI-TICKETS-CHARGE", {
        "codi_qr_png_b64": base64.b64encode(png).decode("ascii"),
        "qrString": charged.get("qrString"),
        "paymentLinkUrl": link,
        "amount": charged.get("amount"),
        "quantity": charged.get("quantity"),
    })


def _step_hint(session: TicketSession) -> str:
    if session.step == "name":
        return "\n\n👉 *Para seguir con tus boletos:* escribe tu *nombre y apellido completo* para el titular de la entrada."
    if session.step == "quantity" and session.ticket_name:
        return f"\n\n👉 *Para seguir con tu apartado de {session.ticket_name}:* escríbeme cuántas entradas quieres (por ejemplo: *1*, *2* o *4*)."
    if session.step == "confirm" and session.quantity and session.ticket_name:
        return f"\n\n👉 *Tu resumen sigue listo ({session.quantity} × {session.ticket_name}):* escribe *sí* para generar tu QR CoDi®, o escríbeme otro número si quieres cambiar la cantidad."
    if session.step == "tickets" and session.event_name:
        return f"\n\n👉 *Para continuar:* toca el botón *Elegir boleto* de arriba para *{session.event_name}*."
    return ""


def _faq(client: CoreApi, session: TicketSession, question: str) -> ChatResponse:
    folded = question.lower()
    last_assistant = next((h["content"] for h in reversed(session.history) if h.get("role") == "assistant"), "")
    is_follow_up = _is_conversational_follow_up(folded, session)
    retrieval_query = f"{last_assistant} {question}" if is_follow_up and last_assistant else question
    all_chunks = _build_event_rag_chunks(client, session)
    matched_sources = _retrieve_relevant_chunks(retrieval_query, all_chunks, session)
    hint = "" if _is_mock_client(client) else _step_hint(session)

    for words, answer in _CHECKOUT_RULES:
        if any(word in folded for word in words):
            if "spei" in words:
                spei_details = _spei_account_info()
                if spei_details:
                    answer = (
                        f"¡No te preocupes! Si prefieres o se te complica usar CoDi®, con mucho gusto puedes realizar tu pago mediante una *transferencia bancaria SPEI* directa:\n\n"
                        f"{spei_details}\n\n"
                        f"En cuanto hagas tu transferencia, mándanos tu comprobante por aquí para confirmarte tus boletos al instante. 😊"
                    )
            llm_rule = _call_ticket_llm(
                question,
                session,
                matched_sources or all_chunks[:2],
                f"Responde con calidez a la duda del usuario sobre reglas de pago/acceso usando exactamente este dato verificado: {answer}",
                is_mock_test=_is_mock_client(client),
            )
            ans_body = (llm_rule[0] if llm_rule else answer)
            session.history.append({"role": "user", "content": question})
            session.history.append({"role": "assistant", "content": ans_body})
            if llm_rule:
                return _reply(ans_body + hint, "CODI-TICKETS-FAQ", model_name=llm_rule[1], sources=matched_sources)
            return _reply(ans_body + hint, "CODI-TICKETS-FAQ", sources=matched_sources)

    description = session.event_description or ""
    tokens = re.findall(r"[a-záéíóúñ0-9]{4,}", folded)
    has_desc_match = bool(description and any(token in description.lower() for token in tokens))

    if has_desc_match or matched_sources or is_follow_up:
        if is_follow_up and last_assistant:
            instruction = (
                f"El cliente respondió '{question}' a tu mensaje previo ('{last_assistant}'). "
                "Como Valeria, responde de forma sumamente amable y clara proporcionando la información que el cliente solicitó "
                "(por ejemplo, horarios de apertura a las 20:00 hrs, clase muestra de 20:00 a 21:00 hrs, cierre a las 03:00 hrs, y precios de boletos en $0.01 MXN según las fichas RAG). "
                "Invítalo amablemente a continuar con su compra cuando esté listo."
            )
        else:
            instruction = (
                "El usuario hizo una pregunta sobre el evento. Responde de forma cálida, natural y breve "
                "usando únicamente los datos de las fichas RAG proporcionadas. No inventes información."
            )
        llm_ans = _call_ticket_llm(
            question,
            session,
            matched_sources or all_chunks,
            instruction,
            is_mock_test=_is_mock_client(client),
        )
        if llm_ans:
            reply_text = llm_ans[0]
            session.history.append({"role": "user", "content": question})
            session.history.append({"role": "assistant", "content": reply_text})
            return _reply(reply_text + hint, "CODI-TICKETS-FAQ", model_name=llm_ans[1], sources=matched_sources)
        if has_desc_match:
            reply_text = f"Según la ficha del evento: {description}"
            session.history.append({"role": "user", "content": question})
            session.history.append({"role": "assistant", "content": reply_text})
            return _reply(reply_text + hint, "CODI-TICKETS-FAQ", sources=matched_sources)
        if matched_sources:
            reply_text = matched_sources[0].answer
            session.history.append({"role": "user", "content": question})
            session.history.append({"role": "assistant", "content": reply_text})
            return _reply(reply_text + hint, "CODI-TICKETS-FAQ", sources=matched_sources)

    no_data_reply = "No tengo ese dato en la ficha del evento. Puedo pasarte con una persona si escribes asesor."
    session.history.append({"role": "user", "content": question})
    session.history.append({"role": "assistant", "content": no_data_reply})
    return _reply(
        no_data_reply + hint,
        "CODI-TICKETS-FAQ",
    )


def _ticket(session: TicketSession, ticket_id: str) -> Optional[Dict[str, Any]]:
    for event in session.events:
        for ticket in event.get("ticketTypes") or []:
            if ticket.get("id") == ticket_id:
                return ticket
    return None


def _menu(
    body: str,
    section: str,
    rows: List[Dict[str, str]],
    use_case: str,
    tool_output: Optional[Dict[str, Any]] = None,
    button_label: str = "Ver opciones",
    model_name: str = "codi-tickets",
    sources: Optional[List[RetrievedSource]] = None,
) -> ChatResponse:
    return ChatResponse(
        reply=body,
        model=model_name,
        sources=sources or [],
        active_use_case=use_case,
        tool_executed="mex-transfer-api",
        tool_output=tool_output,
        interactive_menu={
            "type": "list",
            "body": {"text": body[:1020]},
            "action": {"button": button_label[:20], "sections": [{"title": section[:24], "rows": rows}]},
        },
    )


def _reply(
    body: str,
    use_case: str,
    tool_output: Optional[Dict[str, Any]] = None,
    model_name: str = "codi-tickets",
    sources: Optional[List[RetrievedSource]] = None,
) -> ChatResponse:
    return ChatResponse(
        reply=body,
        model=model_name,
        sources=sources or [],
        active_use_case=use_case,
        tool_executed="mex-transfer-api" if tool_output else None,
        tool_output=tool_output,
    )


def core_from_env() -> CoreApi:
    return CoreApi(os.getenv("CODI_CORE_API_URL"), os.getenv("INTERNAL_API_TOKEN", ""))
