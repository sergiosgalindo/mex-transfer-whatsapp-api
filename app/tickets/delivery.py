import os
from typing import Any, Dict, List

import httpx

sent_images: List[Dict[str, Any]] = []


def deliver_image(phone: str, png: bytes, caption: str, mime_type: str = "image/png") -> Dict[str, Any]:
    token = (os.getenv("WHATSAPP_ACCESS_TOKEN") or os.getenv("WHATSAPP_TOKEN") or "").strip()
    phone_number_id = (os.getenv("PHONE_NUMBER_ID") or os.getenv("WHATSAPP_PHONE_NUMBER_ID") or "").strip()
    record: Dict[str, Any] = {"phone": phone, "png": png, "caption": caption, "mime_type": mime_type}
    if not token or not phone_number_id:
        record["simulated"] = True
        sent_images.append(record)
        return {"sent": False, "simulated": True, "to": phone}

    ext = "jpg" if "jpeg" in mime_type or "jpg" in mime_type else "png"
    version = os.getenv("WHATSAPP_API_VERSION", "v25.0").strip()
    upload = httpx.post(
        f"https://graph.facebook.com/{version}/{phone_number_id}/media",
        headers={"Authorization": f"Bearer {token}"},
        data={"messaging_product": "whatsapp"},
        files={"file": (f"media.{ext}", png, mime_type)},
        timeout=20.0,
    )
    media = upload.json() if upload.content else {}
    media_id = media.get("id")
    if not media_id:
        record["status_code"] = upload.status_code
        sent_images.append(record)
        print(f"[WhatsApp Media Upload Error] to={phone} status={upload.status_code} body={upload.text}", flush=True)
        return {"sent": False, "to": phone, "status_code": upload.status_code}

    response = httpx.post(
        f"https://graph.facebook.com/{version}/{phone_number_id}/messages",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={
            "messaging_product": "whatsapp",
            "to": phone,
            "type": "image",
            "image": {"id": media_id, "caption": caption[:1024]},
        },
        timeout=20.0,
    )
    print(f"[WhatsApp Media Outbound] to={phone} status={response.status_code} body={response.text}", flush=True)
    record["status_code"] = response.status_code
    sent_images.append(record)
    return {"sent": response.status_code in (200, 201), "to": phone, "status_code": response.status_code}
