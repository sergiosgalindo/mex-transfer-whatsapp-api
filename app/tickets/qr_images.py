import io

import qrcode
from PIL import Image, ImageDraw


def render_qr_png(payload: str) -> bytes:
    image = qrcode.make(payload).convert("RGB")
    return _png(image)


def render_entry_ticket(payload: str, event_name: str, access_code: str, quantity: int) -> bytes:
    code = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=8, border=2)
    code.add_data(payload)
    code.make(fit=True)
    qr_image = code.make_image(fill_color="black", back_color="white").convert("RGB")
    width = max(qr_image.width + 48, 360)
    canvas = Image.new("RGB", (width, qr_image.height + 132), "white")
    canvas.paste(qr_image, ((width - qr_image.width) // 2, 24))
    draw = ImageDraw.Draw(canvas)
    draw.rectangle((0, 0, width, 16), fill="black")
    draw.text((24, qr_image.height + 40), "BOLETO DE ENTRADA", fill="black")
    draw.text((24, qr_image.height + 64), (event_name or "Evento")[:42], fill="black")
    draw.text((24, qr_image.height + 88), f"Acceso {access_code}   x{quantity}", fill="black")
    return _png(canvas)


def _png(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()
