"""
ai_booking_handler.py — AI extract booking intent từ tin nhắn tự nhiên
Khi Hiếu forward/gõ tin của khách → Claude phân tích → Hiếu duyệt → Tạo booking
"""

import logging
import json
import re
from datetime import datetime, date, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)
import anthropic
import os
from firebase_client import (
    ROOMS, SOURCES, get_room_name, get_room_price,
    format_currency, format_date_vn,
    create_booking, check_room_availability, _calc_nights,
)

logger = logging.getLogger(__name__)

# States cho AI booking flow
AI_REVIEW = 10
AI_EDIT_FIELD = 11

# Anthropic client
_anthropic_client = None


def _get_anthropic():
    global _anthropic_client
    if not _anthropic_client:
        _anthropic_client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))
    return _anthropic_client


EXTRACT_SYSTEM = """Bạn là trợ lý của Hello Dalat Hostel ở Đà Lạt.
Nhiệm vụ: Đọc tin nhắn của khách (có thể là tiếng Việt, tiếng Anh, viết tắt, thiếu dấu)
và trích xuất thông tin đặt phòng.

Trả về JSON hợp lệ với các trường sau (null nếu không tìm được):
{
  "guest_name": "tên khách",
  "phone": "số điện thoại (chuỗi)",
  "check_in": "YYYY-MM-DD hoặc null",
  "check_out": "YYYY-MM-DD hoặc null",
  "nights": số đêm (int hoặc null),
  "room_type_hint": "mô tả loại phòng khách muốn (hoặc null)",
  "room_id": "số phòng cụ thể nếu đặt phòng cố định (hoặc null)",
  "source": "nguồn (Booking.com/Facebook/Gọi điện/Zalo/Walk-in...)",
  "note": "ghi chú thêm",
  "num_guests": số người (int hoặc null),
  "confidence": "high/medium/low",
  "missing_fields": ["danh sách trường còn thiếu"]
}

Lưu ý:
- Ngày hôm nay là {today}. Tính check_out từ nights nếu có.
- Nếu chỉ có "N đêm" mà không có check_out → tính check_out = check_in + N ngày
- Phòng Hello Dalat: 101 (Family 4 người), 102/202 (Single), 301/302 (Std Double), 103/203 (Dlx Double), 201 (Dlx Queen 2m)
- Chỉ trả về JSON, không giải thích gì thêm."""


async def extract_booking_from_text(text: str) -> dict | None:
    """Gọi Claude để extract thông tin booking từ text."""
    today = date.today().strftime("%Y-%m-%d")
    system = EXTRACT_SYSTEM.replace("{today}", today)

    try:
        client = _get_anthropic()
        msg = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1000,
            messages=[{"role": "user", "content": f"Tin nhắn khách:\n{text}"}],
            system=system,
        )
        raw = msg.content[0].text.strip()

        # Strip markdown code blocks nếu có
        if raw.startswith("```"):
            raw = re.sub(r"```[a-z]*\n?", "", raw).strip()

        data = json.loads(raw)
        return data
    except json.JSONDecodeError as e:
        logger.error(f"JSON decode lỗi: {e}")
        return None
    except Exception as e:
        logger.error(f"Claude extract lỗi: {e}")
        return None


def _suggest_room(extracted: dict) -> str | None:
    """Gợi ý phòng dựa trên thông tin extract được."""
    if extracted.get("room_id"):
        from firebase_client import ROOM_BY_ID
        if extracted["room_id"] in ROOM_BY_ID:
            return extracted["room_id"]

    num_guests = extracted.get("num_guests") or 2
    hint = (extracted.get("room_type_hint") or "").lower()

    if num_guests >= 4 or "family" in hint or "4 người" in hint:
        return "101"
    if num_guests == 1 or "single" in hint or "1 người" in hint:
        return "102"
    if "queen" in hint or "2m" in hint:
        return "201"
    if "deluxe" in hint or "dlx" in hint or "vip" in hint:
        return "103"
    # Default: Standard Double
    return "301"


def _build_review_text(extracted: dict, suggested_room: str) -> str:
    """Tạo text review để Hiếu xem và duyệt."""
    ci = extracted.get("check_in")
    co = extracted.get("check_out")

    # Tính check_out nếu chỉ có nights
    if ci and not co and extracted.get("nights"):
        try:
            d = datetime.strptime(ci, "%Y-%m-%d")
            co = (d + timedelta(days=extracted["nights"])).strftime("%Y-%m-%d")
            extracted["check_out"] = co
        except Exception:
            pass

    nights = _calc_nights(ci, co) if ci and co else "?"
    price = get_room_price(suggested_room) if suggested_room else 0
    total = price * (nights if isinstance(nights, int) else 0)

    confidence_emoji = {"high": "🟢", "medium": "🟡", "low": "🔴"}.get(
        extracted.get("confidence", "medium"), "🟡"
    )

    missing = extracted.get("missing_fields", [])
    missing_text = f"\n⚠️ Thiếu: {', '.join(missing)}" if missing else ""

    return (
        f"🤖 *AI đã phân tích tin khách*\n"
        f"{confidence_emoji} Độ chắc chắn: {extracted.get('confidence', '?')}\n"
        f"─────────────────────\n"
        f"🛏 Phòng đề xuất: *{get_room_name(suggested_room)}*\n"
        f"📅 Check-in: *{format_date_vn(ci) if ci else '❓ Chưa có'}*\n"
        f"📅 Check-out: *{format_date_vn(co) if co else '❓ Chưa có'}*\n"
        f"🌙 Số đêm: {nights}\n"
        f"💵 Tổng: {format_currency(total) if total else '—'}\n"
        f"─────────────────────\n"
        f"👤 Tên: *{extracted.get('guest_name') or '❓'}*\n"
        f"📱 SĐT: {extracted.get('phone') or '❓'}\n"
        f"👥 Số khách: {extracted.get('num_guests') or '?'}\n"
        f"🔗 Nguồn: {extracted.get('source') or '?'}\n"
        f"📝 Ghi chú: {extracted.get('note') or '—'}\n"
        f"{missing_text}\n"
        f"─────────────────────\n"
        f"Hiếu có muốn tạo booking này không?"
    )


def _review_keyboard(has_missing: bool) -> InlineKeyboardMarkup:
    buttons = [
        [
            InlineKeyboardButton("✅ Tạo booking", callback_data="ai_confirm"),
            InlineKeyboardButton("❌ Bỏ qua", callback_data="ai_skip"),
        ],
        [InlineKeyboardButton("✏️ Sửa → /book", callback_data="ai_edit")],
    ]
    return InlineKeyboardMarkup(buttons)


# ── Handler chính: nhận tin nhắn để extract ──────────────────────────────────

async def handle_ai_extract(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Được gọi khi Hiếu nhắn /ai hoặc forward tin khách.
    Sử dụng như một non-conversation handler (simple command).
    """
    text = update.message.text or ""

    # Bỏ /ai prefix nếu có
    if text.startswith("/ai"):
        text = text[3:].strip()

    if not text:
        await update.message.reply_text(
            "Cách dùng: Gõ `/ai [nội dung tin khách]`\n\n"
            "VD: `/ai Cho mình đặt phòng đôi từ 25/7 đến 27/7, 2 người, tên Linh SĐT 0912345678`",
            parse_mode="Markdown",
        )
        return

    processing_msg = await update.message.reply_text("🤖 Đang phân tích...")

    extracted = await extract_booking_from_text(text)
    await processing_msg.delete()

    if not extracted:
        await update.message.reply_text(
            "❌ Không thể phân tích. Hãy dùng /book để nhập tay."
        )
        return

    suggested_room = _suggest_room(extracted)
    review_text = _build_review_text(extracted, suggested_room)

    # Lưu vào context để xử lý tiếp
    ctx.user_data["ai_extracted"] = extracted
    ctx.user_data["ai_suggested_room"] = suggested_room

    has_missing = bool(extracted.get("missing_fields"))
    await update.message.reply_text(
        review_text,
        parse_mode="Markdown",
        reply_markup=_review_keyboard(has_missing),
    )


async def handle_ai_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Khi Hiếu bấm Xác nhận tạo booking từ AI extract."""
    query = update.callback_query
    await query.answer()

    extracted = ctx.user_data.get("ai_extracted", {})
    suggested_room = ctx.user_data.get("ai_suggested_room", "301")

    ci = extracted.get("check_in")
    co = extracted.get("check_out")
    name = extracted.get("guest_name", "Khách")
    phone = extracted.get("phone", "")
    source = extracted.get("source", "Vãng lai (Walk-in)")
    note = extracted.get("note", "")

    if not ci or not co:
        await query.edit_message_text(
            "❌ Thiếu ngày check-in hoặc check-out.\n"
            "Dùng /book để nhập đầy đủ."
        )
        return

    # Kiểm tra phòng trống
    if not check_room_availability(suggested_room, ci, co):
        await query.edit_message_text(
            f"⚠️ Phòng *{suggested_room}* đã có khách!\n"
            f"Dùng /book để chọn phòng khác.",
            parse_mode="Markdown",
        )
        return

    msg = await query.message.reply_text("⏳ Đang tạo booking...")

    result = create_booking(
        room_id=suggested_room,
        check_in=ci,
        check_out=co,
        guest_name=name,
        phone=phone,
        source=source,
        note=note,
    )

    await msg.delete()

    if result:
        nights = result["nights"]
        await query.edit_message_text(
            f"✅ *Booking tạo thành công!*\n\n"
            f"🛏 {get_room_name(suggested_room)}\n"
            f"📅 {format_date_vn(ci)} → {format_date_vn(co)} ({nights} đêm)\n"
            f"👤 {name} · {phone or '—'}\n"
            f"💵 {format_currency(result['total'])}\n\n"
            f"_Dashboard đang cập nhật..._",
            parse_mode="Markdown",
        )
    else:
        await query.edit_message_text(
            "❌ Lỗi tạo booking. Thử lại hoặc dùng /book."
        )

    ctx.user_data.pop("ai_extracted", None)
    ctx.user_data.pop("ai_suggested_room", None)


async def handle_ai_skip(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("⏭️ Đã bỏ qua.")
    ctx.user_data.pop("ai_extracted", None)
    ctx.user_data.pop("ai_suggested_room", None)


async def handle_ai_edit(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "✏️ Dùng /book để nhập thông tin đầy đủ theo từng bước."
    )
    ctx.user_data.pop("ai_extracted", None)
    ctx.user_data.pop("ai_suggested_room", None)
