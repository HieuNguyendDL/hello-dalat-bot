"""
Hello Dalat Hostel — Guest AI Flow (Luồng 2)
Khách nhắn tự do → Claude AI hiểu ý định → hỏi thiếu thông tin →
khi đủ data → Hiếu xác nhận → tạo booking + gửi confirmation.
"""

import json
import logging
import re
from datetime import datetime

import anthropic
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from config import (
    ROOMS, SOURCES, ANTHROPIC_API_KEY, ADMIN_CHAT_ID,
    VN_TZ, HOSTEL_NAME, HOSTEL_PHONE, CHECKIN_TIME, CHECKOUT_TIME,
    calc_room_price, get_room_list_text
)
from firestore_service import create_booking, get_available_rooms
from pdf_service import generate_confirmation_pdf

logger = logging.getLogger(__name__)

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# ── System prompt ─────────────────────────────────────────────────────────────
SYSTEM_PROMPT = f"""You are a friendly front desk assistant for Hello Dalat Hostel in Đà Lạt, Vietnam.
You help guests inquire about rooms and make bookings.

HOSTEL INFO:
- Name: {HOSTEL_NAME}
- Address: 18/2 Hẻm 33 Phan Đình Phùng, Phường 1, Đà Lạt
- Phone: {HOSTEL_PHONE}
- Check-in: after {CHECKIN_TIME} | Check-out: by {CHECKOUT_TIME}
- WiFi: HelloDalat / hellodalat
- Breakfast: 7:00–9:30 AM (complimentary for Booking.com guests, 35,000đ for others)
- Quiet hours: after 22:00

ROOMS:
101 — Family Room | 2 beds | 4 guests | 450,000đ/night
102, 202 — Single | 1.4m bed | 1 guest | 180,000đ/night
103, 203 — Deluxe Double | 1.6m bed | 2 guests | 300,000đ/night
201 — Deluxe Queen | 2m×2m bed | 2 guests | 400,000đ/night
301, 302 — Standard Double | 1.6m bed | 2 guests | 250,000đ/night

SERVICES:
- Scooter rental: 130,000đ/day
- Laundry: 25,000đ
- Tour referrals (trekking, canyoning, Easy Rider)
- Free bus booking assistance

BOOKING INTENT DETECTION:
When a guest clearly wants to book a room, extract the following information and respond ONLY with this JSON block at the end of your message:

<booking_intent>
{{
  "intent": "book",
  "guestName": "...",
  "guestPhone": "...",
  "checkIn": "YYYY-MM-DD",
  "checkOut": "YYYY-MM-DD",
  "guests": 1,
  "roomPreference": "...",
  "notes": "...",
  "complete": true/false,
  "missing": ["field1", "field2"]
}}
</booking_intent>

Set "complete": true only when you have: guestName, guestPhone, checkIn, checkOut, guests.
If any are missing, set "complete": false and list them in "missing".

LANGUAGE: Respond in the same language the guest uses (Vietnamese or English).
TONE: Warm, helpful, concise. No excessive emojis.
"""


# ── Parse booking intent from AI response ─────────────────────────────────────
def _extract_intent(text: str) -> dict | None:
    match = re.search(r"<booking_intent>(.*?)</booking_intent>", text, re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(1).strip())
    except json.JSONDecodeError:
        return None


def _clean_response(text: str) -> str:
    """Remove the JSON block from the visible response."""
    return re.sub(r"<booking_intent>.*?</booking_intent>", "", text, flags=re.DOTALL).strip()


# ── Guest conversation handler ────────────────────────────────────────────────

# In-memory conversation store: {chat_id: [messages]}
_guest_conversations: dict[int, list] = {}


async def handle_guest_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Main handler for guest messages (non-admin users)."""
    chat_id = update.effective_chat.id
    user_text = update.message.text.strip()

    # Init conversation history
    if chat_id not in _guest_conversations:
        _guest_conversations[chat_id] = []

    history = _guest_conversations[chat_id]
    history.append({"role": "user", "content": user_text})

    # Keep last 20 messages
    if len(history) > 20:
        history = history[-20:]
        _guest_conversations[chat_id] = history

    # Call Claude
    await context.bot.send_chat_action(chat_id=chat_id, action="typing")

    try:
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=800,
            system=SYSTEM_PROMPT,
            messages=history,
        )
        ai_text = response.content[0].text
    except Exception as e:
        logger.error(f"Claude API error: {e}")
        await update.message.reply_text(
            "Sorry, I'm having trouble right now. Please contact us directly:\n"
            f"📞 {HOSTEL_PHONE}"
        )
        return

    # Append assistant response to history
    history.append({"role": "assistant", "content": ai_text})

    # Check for booking intent
    intent = _extract_intent(ai_text)
    clean_text = _clean_response(ai_text)

    # Send visible response
    await update.message.reply_text(clean_text)

    # If complete booking intent — notify admin
    if intent and intent.get("complete") and intent.get("intent") == "book":
        await _notify_admin_pending_booking(context, chat_id, intent, update.effective_user)


async def _notify_admin_pending_booking(
    context: ContextTypes.DEFAULT_TYPE,
    guest_chat_id: int,
    intent: dict,
    user
):
    """Send booking request to Hiếu for approval."""
    from config import ROOMS

    # Find suitable room
    checkin = intent.get("checkIn", "")
    checkout = intent.get("checkOut", "")
    guests_count = intent.get("guests", 1)

    available = get_available_rooms(checkin, checkout)
    pref = intent.get("roomPreference", "").lower()

    # Match preference or pick first available with enough capacity
    chosen_room = None
    for room_id in available:
        room = ROOMS[room_id]
        if room["capacity"] >= guests_count:
            if pref and (pref in room["name"].lower() or pref in room_id):
                chosen_room = room_id
                break
            elif chosen_room is None:
                chosen_room = room_id

    if not chosen_room:
        # No availability — inform admin anyway
        await context.bot.send_message(
            chat_id=ADMIN_CHAT_ID,
            text=(
                f"🔔 *Booking request — No availability*\n\n"
                f"Guest: {intent.get('guestName')}\n"
                f"Dates: {checkin} → {checkout}\n"
                f"Guests: {guests_count}\n\n"
                f"⚠️ No available rooms found for these dates."
            ),
            parse_mode="Markdown"
        )
        return

    room = ROOMS[chosen_room]
    price_per_night = calc_room_price(chosen_room, datetime.strptime(checkin, "%Y-%m-%d"))
    try:
        nights = (
            datetime.strptime(checkout, "%Y-%m-%d") -
            datetime.strptime(checkin, "%Y-%m-%d")
        ).days
    except Exception:
        nights = 1
    grand_total = price_per_night * nights

    # Store pending booking in context
    pending_key = f"pending_{guest_chat_id}"
    context.bot_data[pending_key] = {
        "guestName": intent.get("guestName", ""),
        "guestPhone": intent.get("guestPhone", ""),
        "guestTelegramId": guest_chat_id,
        "roomId": chosen_room,
        "roomName": room["name"],
        "checkIn": checkin,
        "checkOut": checkout,
        "nights": nights,
        "guests": guests_count,
        "pricePerNight": price_per_night,
        "grandTotal": grand_total,
        "source": "direct_telegram",
        "notes": intent.get("notes", ""),
        "createdBy": "guest_ai",
    }

    keyboard = [[
        InlineKeyboardButton("✅ Xác nhận", callback_data=f"guestbook_confirm_{guest_chat_id}"),
        InlineKeyboardButton("✏️ Sửa", callback_data=f"guestbook_edit_{guest_chat_id}"),
        InlineKeyboardButton("❌ Từ chối", callback_data=f"guestbook_reject_{guest_chat_id}"),
    ]]

    await context.bot.send_message(
        chat_id=ADMIN_CHAT_ID,
        text=(
            f"🔔 *Booking mới từ khách*\n\n"
            f"👤 {intent.get('guestName')} | 📱 {intent.get('guestPhone')}\n"
            f"🏨 Phòng *{chosen_room}* — {room['name']}\n"
            f"📅 {checkin} → {checkout} ({nights} đêm)\n"
            f"👥 {guests_count} khách\n"
            f"💵 {price_per_night:,}đ × {nights} = *{grand_total:,}đ*\n"
            f"📝 {intent.get('notes') or 'Không có ghi chú'}"
        ),
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def handle_guest_booking_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle admin's confirm/reject of a guest booking."""
    query = update.callback_query
    await query.answer()

    parts = query.data.split("_")
    action = parts[2]      # confirm | edit | reject
    guest_chat_id = int(parts[3])

    pending_key = f"pending_{guest_chat_id}"
    booking_data = context.bot_data.get(pending_key)

    if not booking_data:
        await query.edit_message_text("⚠️ Không tìm thấy booking draft.")
        return

    if action == "reject":
        del context.bot_data[pending_key]
        await query.edit_message_text("❌ Đã từ chối booking.")
        await context.bot.send_message(
            chat_id=guest_chat_id,
            text=(
                "Thank you for your interest in Hello Dalat Hostel. "
                "Unfortunately we're unable to confirm this booking right now. "
                f"Please contact us directly: {HOSTEL_PHONE}"
            )
        )
        return

    if action == "edit":
        await query.edit_message_text(
            "✏️ Dùng /newbooking để tạo lại với thông tin chính xác.\n"
            f"_(Draft đã lưu: {booking_data.get('guestName')} — {booking_data.get('checkIn')})_",
            parse_mode="Markdown"
        )
        return

    # Confirm — save to Firestore
    await query.edit_message_text("⏳ Đang lưu...")

    saved = create_booking(booking_data)
    booking_id = saved["bookingId"]

    # Generate PDF
    pdf_bytes = generate_confirmation_pdf(saved)

    # Notify admin
    await context.bot.send_message(
        chat_id=ADMIN_CHAT_ID,
        text=f"✅ *Booking đã xác nhận!* ID: `{booking_id}`",
        parse_mode="Markdown"
    )
    await context.bot.send_document(
        chat_id=ADMIN_CHAT_ID,
        document=pdf_bytes,
        filename=f"Confirmation_{booking_id}.pdf",
        caption=f"📄 {booking_data['guestName']} — {booking_id}"
    )

    # Send confirmation to guest
    checkin_fmt = datetime.strptime(booking_data["checkIn"], "%Y-%m-%d").strftime("%d %b %Y")
    checkout_fmt = datetime.strptime(booking_data["checkOut"], "%Y-%m-%d").strftime("%d %b %Y")

    await context.bot.send_message(
        chat_id=guest_chat_id,
        text=(
            f"✅ *Your booking is confirmed!*\n\n"
            f"📋 Booking ID: `{booking_id}`\n"
            f"🏨 Room {booking_data['roomId']} — {booking_data['roomName']}\n"
            f"📅 {checkin_fmt} → {checkout_fmt}\n"
            f"💵 Total: {booking_data['grandTotal']:,}đ\n\n"
            f"We look forward to welcoming you!\n"
            f"📍 18/2 Hẻm 33 Phan Đình Phùng, Đà Lạt\n"
            f"📞 {HOSTEL_PHONE}"
        ),
        parse_mode="Markdown"
    )
    await context.bot.send_document(
        chat_id=guest_chat_id,
        document=pdf_bytes,
        filename=f"HelloDalat_Confirmation_{booking_id}.pdf",
        caption="Your booking confirmation 🏨"
    )

    del context.bot_data[pending_key]
