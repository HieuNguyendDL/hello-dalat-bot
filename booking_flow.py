"""
booking_flow.py — Hello Dalat Hostel
Auto Booking Flow: nhận booking từ Hiếu (admin) hoặc khách (guest)
Tích hợp vào bot.py hiện có.
"""

import os
import io
import re
import asyncio
from datetime import datetime, date
from zoneinfo import ZoneInfo
from typing import Optional

import anthropic
import firebase_admin
from firebase_admin import credentials, firestore
from reportlab.lib.pagesizes import A5
from reportlab.lib import colors
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas as rl_canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ContextTypes,
    ConversationHandler,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)

# ─── CONFIG ──────────────────────────────────────────────────────────────────

VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")

ROOMS = {
    "101": {"name": "Family 101",        "type": "Family",         "beds": "2 giường", "capacity": 4, "price": 450_000},
    "102": {"name": "Single 102",        "type": "Single",         "beds": "1.4m",     "capacity": 1, "price": 180_000},
    "103": {"name": "Deluxe Double 103", "type": "Deluxe Double",  "beds": "1.6m",     "capacity": 2, "price": 300_000},
    "201": {"name": "Deluxe Queen 201",  "type": "Deluxe Queen",   "beds": "2m×2m",    "capacity": 2, "price": 400_000},
    "202": {"name": "Single 202",        "type": "Single",         "beds": "1.4m",     "capacity": 1, "price": 180_000},
    "203": {"name": "Deluxe Double 203", "type": "Deluxe Double",  "beds": "1.6m",     "capacity": 2, "price": 300_000},
    "301": {"name": "Standard Double 301","type": "Standard Double","beds": "1.6m",    "capacity": 2, "price": 250_000},
    "302": {"name": "Standard Double 302","type": "Standard Double","beds": "1.6m",    "capacity": 2, "price": 250_000},
}

HOSTEL = {
    "name":    "Hello Dalat Hostel",
    "address": "18/2 Hẻm 33 Phan Đình Phùng, Phường 1, Đà Lạt",
    "phone":   "+84 969 975 935",
    "email":   "hellodalathostel@gmail.com",
    "checkin":  "14:00",
    "checkout": "12:00",
}

# Conversation states — admin flow
(
    ADM_NAME, ADM_CHECKIN, ADM_CHECKOUT,
    ADM_ROOM, ADM_GUESTS, ADM_SOURCE, ADM_CONFIRM
) = range(10, 17)

# Conversation states — guest flow
(
    GST_NAME, GST_CHECKIN, GST_CHECKOUT,
    GST_ROOM_TYPE, GST_GUESTS, GST_CONFIRM
) = range(20, 26)

# ─── FIRESTORE ───────────────────────────────────────────────────────────────

_db: Optional[object] = None

def get_db():
    global _db
    if _db is None:
        if not firebase_admin._apps:
            # Dùng GOOGLE_APPLICATION_CREDENTIALS hoặc service account JSON
            cred_path = os.getenv("FIREBASE_CREDENTIALS")
            if cred_path and os.path.exists(cred_path):
                cred = credentials.Certificate(cred_path)
            else:
                cred = credentials.ApplicationDefault()
            firebase_admin.initialize_app(cred)
        _db = firestore.client()
    return _db

def next_booking_id() -> str:
    """Tạo Booking ID tăng dần: B001, B002, ..."""
    db = get_db()
    ref = db.collection("meta").document("booking_counter")
    @firestore.transactional
    def increment(transaction):
        snap = ref.get(transaction=transaction)
        current = snap.get("value") if snap.exists else 0
        new_val = current + 1
        transaction.set(ref, {"value": new_val})
        return new_val
    n = increment(db.transaction())
    return f"B{n:03d}"

def save_booking(data: dict) -> str:
    """Lưu booking vào Firestore, trả về booking_id."""
    db = get_db()
    booking_id = next_booking_id()
    nights = (data["checkout"] - data["checkin"]).days
    room = ROOMS[data["room_id"]]
    grand_total = room["price"] * nights

    doc = {
        "bookingId":     booking_id,
        "guestName":     data["guest_name"],
        "roomId":        data["room_id"],
        "roomName":      room["name"],
        "roomType":      room["type"],
        "checkIn":       data["checkin"].isoformat(),
        "checkOut":      data["checkout"].isoformat(),
        "nights":        nights,
        "guests":        data.get("guests", 1),
        "pricePerNight": room["price"],
        "grandTotal":    grand_total,
        "source":        data.get("source", "direct"),
        "status":        "confirmed",
        "paymentMethod": data.get("payment", ""),
        "notes":         data.get("notes", ""),
        "createdAt":     firestore.SERVER_TIMESTAMP,
        "createdBy":     data.get("created_by", "admin"),
    }
    db.collection("bookings").document(booking_id).set(doc)
    return booking_id

def get_booked_rooms(checkin: date, checkout: date) -> set:
    """Trả về set room_id đã có booking conflict."""
    db = get_db()
    docs = db.collection("bookings").where("status", "in", ["confirmed", "checkin"]).stream()
    booked = set()
    for doc in docs:
        d = doc.to_dict()
        existing_in  = date.fromisoformat(d["checkIn"])
        existing_out = date.fromisoformat(d["checkOut"])
        # Overlap: checkin < existing_out AND checkout > existing_in
        if checkin < existing_out and checkout > existing_in:
            booked.add(d["roomId"])
    return booked

def available_rooms(checkin: date, checkout: date) -> list[dict]:
    """Trả về list phòng còn trống."""
    booked = get_booked_rooms(checkin, checkout)
    return [
        {"id": rid, **info}
        for rid, info in ROOMS.items()
        if rid not in booked
    ]

# ─── PDF CONFIRMATION ─────────────────────────────────────────────────────────

def generate_confirmation_pdf(booking_id: str, data: dict) -> bytes:
    """
    Tạo PDF confirmation A5, trả về bytes.
    Brand: forest green #2D5016, clean layout.
    """
    room     = ROOMS[data["room_id"]]
    nights   = (data["checkout"] - data["checkin"]).days
    total    = room["price"] * nights
    checkin  = data["checkin"].strftime("%d/%m/%Y")
    checkout = data["checkout"].strftime("%d/%m/%Y")

    buf = io.BytesIO()
    W, H = A5  # 148mm × 210mm → 419.5 × 595.3 pt
    c = rl_canvas.Canvas(buf, pagesize=A5)

    GREEN  = colors.HexColor("#2D5016")
    LIGHT  = colors.HexColor("#F5F7F2")
    WHITE  = colors.white
    GRAY   = colors.HexColor("#666666")
    BLACK  = colors.HexColor("#1A1A1A")

    # ── Header bar ──────────────────────────────────────────────
    c.setFillColor(GREEN)
    c.rect(0, H - 72, W, 72, fill=1, stroke=0)

    c.setFillColor(WHITE)
    c.setFont("Helvetica-Bold", 15)
    c.drawCentredString(W / 2, H - 28, HOSTEL["name"].upper())
    c.setFont("Helvetica", 8)
    c.drawCentredString(W / 2, H - 44, HOSTEL["address"])
    c.drawCentredString(W / 2, H - 56, f"{HOSTEL['phone']}  •  {HOSTEL['email']}")

    # ── Booking ID badge ─────────────────────────────────────────
    c.setFillColor(LIGHT)
    c.roundRect(20, H - 110, W - 40, 28, 4, fill=1, stroke=0)
    c.setFillColor(GREEN)
    c.setFont("Helvetica-Bold", 9)
    c.drawCentredString(W / 2, H - 88, "BOOKING CONFIRMATION")
    c.setFont("Helvetica-Bold", 14)
    c.drawCentredString(W / 2, H - 103, booking_id)

    # ── Guest info section ───────────────────────────────────────
    y = H - 130
    def section_title(label, y_pos):
        c.setFillColor(GREEN)
        c.setFont("Helvetica-Bold", 8)
        c.drawString(20, y_pos, label.upper())
        c.setStrokeColor(GREEN)
        c.setLineWidth(0.5)
        c.line(20, y_pos - 3, W - 20, y_pos - 3)

    def row(label, value, y_pos, bold_val=False):
        c.setFillColor(GRAY)
        c.setFont("Helvetica", 8)
        c.drawString(20, y_pos, label)
        c.setFillColor(BLACK)
        c.setFont("Helvetica-Bold" if bold_val else "Helvetica", 8)
        c.drawRightString(W - 20, y_pos, str(value))

    section_title("Guest", y)
    y -= 14
    row("Name",   data["guest_name"],           y); y -= 13
    row("Guests", f"{data.get('guests', 1)} person(s)", y); y -= 13
    row("Source", data.get("source", "Direct"), y); y -= 18

    section_title("Stay", y)
    y -= 14
    row("Check-in",  f"{checkin}  (from {HOSTEL['checkin']})",   y); y -= 13
    row("Check-out", f"{checkout}  (by {HOSTEL['checkout']})",   y); y -= 13
    row("Duration",  f"{nights} night(s)",                       y); y -= 18

    section_title("Room", y)
    y -= 14
    row("Room",      room["name"],               y); y -= 13
    row("Bed",       room["beds"],               y); y -= 13
    row("Price/night", f"{room['price']:,.0f} VND", y); y -= 18

    # ── Total box ────────────────────────────────────────────────
    c.setFillColor(GREEN)
    c.roundRect(20, y - 22, W - 40, 30, 4, fill=1, stroke=0)
    c.setFillColor(WHITE)
    c.setFont("Helvetica", 9)
    c.drawString(30, y - 8, "TOTAL")
    c.setFont("Helvetica-Bold", 12)
    c.drawRightString(W - 30, y - 8, f"{total:,.0f} VND")
    c.setFont("Helvetica", 7)
    c.drawCentredString(W / 2, y - 19, f"{nights} night(s) × {room['price']:,.0f} VND")
    y -= 36

    # ── Policies ─────────────────────────────────────────────────
    y -= 6
    section_title("Policies", y)
    y -= 14
    policies = [
        "• Free cancellation up to 7 days before check-in",
        "• Quiet hours after 22:00",
        "• Payment: cash or card (+4% surcharge)",
        "• WiFi: HelloDalat / hellodalat",
    ]
    c.setFillColor(GRAY)
    c.setFont("Helvetica", 7.5)
    for pol in policies:
        c.drawString(20, y, pol)
        y -= 11

    # ── Footer ───────────────────────────────────────────────────
    c.setFillColor(LIGHT)
    c.rect(0, 0, W, 28, fill=1, stroke=0)
    c.setFillColor(GRAY)
    c.setFont("Helvetica", 7)
    issued = datetime.now(VN_TZ).strftime("%d/%m/%Y %H:%M")
    c.drawCentredString(W / 2, 17, "Thank you for choosing Hello Dalat Hostel!")
    c.drawCentredString(W / 2, 7, f"Issued: {issued}  •  maps.app.goo.gl/9Bc9d8LBcbr86XaUA")

    c.save()
    return buf.getvalue()

# ─── AI EXTRACT (guest flow) ─────────────────────────────────────────────────

def ai_parse_date(text: str) -> Optional[date]:
    """Dùng regex đơn giản để parse ngày từ tin nhắn khách."""
    now = datetime.now(VN_TZ).date()
    text = text.lower().strip()

    # dd/mm hoặc dd/mm/yyyy
    m = re.search(r'(\d{1,2})[/\-](\d{1,2})(?:[/\-](\d{4}))?', text)
    if m:
        day, month = int(m.group(1)), int(m.group(2))
        year = int(m.group(3)) if m.group(3) else now.year
        try:
            d = date(year, month, day)
            if d < now and not m.group(3):
                d = date(year + 1, month, day)
            return d
        except ValueError:
            pass

    # "ngày mai", "tomorrow"
    if any(w in text for w in ["mai", "tomorrow"]):
        from datetime import timedelta
        return now + timedelta(days=1)

    return None

def ai_suggest_room(checkin: date, checkout: date, guests: int, pref: str) -> Optional[dict]:
    """
    Gợi ý phòng phù hợp nhất dựa trên số khách và preference.
    pref: "rẻ", "đôi", "gia đình", "queen", "single"...
    """
    avail = available_rooms(checkin, checkout)
    if not avail:
        return None
    pref = pref.lower()

    # Filter theo capacity
    avail = [r for r in avail if r["capacity"] >= guests] or avail

    # Ưu tiên theo keyword
    keyword_map = {
        "gia đình": "Family", "family": "Family",
        "queen":    "Deluxe Queen",
        "single":   "Single",
        "deluxe":   "Deluxe",
        "rẻ":       None,
    }
    for kw, rtype in keyword_map.items():
        if kw in pref and rtype:
            matched = [r for r in avail if rtype.lower() in r["type"].lower()]
            if matched:
                return matched[0]

    # Mặc định: rẻ nhất phù hợp
    return sorted(avail, key=lambda r: r["price"])[0]

# ─── SHARED HELPERS ───────────────────────────────────────────────────────────

def fmt_vnd(amount: int) -> str:
    return f"{amount:,.0f}đ".replace(",", ".")

def booking_summary_text(booking_id: str, data: dict) -> str:
    room   = ROOMS[data["room_id"]]
    nights = (data["checkout"] - data["checkin"]).days
    total  = room["price"] * nights
    return (
        f"📋 *{booking_id} — {data['guest_name']}*\n"
        f"🏠 {room['name']}  ({room['beds']})\n"
        f"📅 {data['checkin'].strftime('%d/%m')} → {data['checkout'].strftime('%d/%m/%Y')}  "
        f"({nights} đêm)\n"
        f"👥 {data.get('guests', 1)} khách\n"
        f"💰 {fmt_vnd(total)}  ({fmt_vnd(room['price'])}/đêm)"
    )

async def finalize_booking(
    chat_id: int,
    data: dict,
    context: ContextTypes.DEFAULT_TYPE,
    notify_admin: bool = False,
    admin_chat_id: int = None,
):
    """
    Bước cuối: lưu Firestore → tạo PDF → gửi Telegram.
    notify_admin=True: gửi thêm cho Hiếu khi booking từ khách.
    """
    # 1. Lưu Firestore
    booking_id = save_booking(data)

    # 2. Tạo PDF
    pdf_bytes = generate_confirmation_pdf(booking_id, data)
    pdf_file  = io.BytesIO(pdf_bytes)
    pdf_file.name = f"HelloDalat_{booking_id}.pdf"

    summary = booking_summary_text(booking_id, data)

    # 3. Gửi cho người đang chat
    await context.bot.send_message(
        chat_id=chat_id,
        text=f"✅ *Booking đã xác nhận!*\n\n{summary}",
        parse_mode="Markdown",
    )
    pdf_file.seek(0)
    await context.bot.send_document(
        chat_id=chat_id,
        document=pdf_file,
        filename=pdf_file.name,
        caption=f"📄 Confirmation — {booking_id}",
    )

    # 4. Notify Hiếu nếu booking từ khách
    if notify_admin and admin_chat_id and admin_chat_id != chat_id:
        pdf_file.seek(0)
        await context.bot.send_message(
            chat_id=admin_chat_id,
            text=f"🔔 *Booking mới từ khách!*\n\n{summary}",
            parse_mode="Markdown",
        )
        pdf_file.seek(0)
        await context.bot.send_document(
            chat_id=admin_chat_id,
            document=pdf_file,
            filename=pdf_file.name,
            caption=f"📄 {booking_id}",
        )

    return booking_id

# ─── ADMIN FLOW (/newbooking) ─────────────────────────────────────────────────

async def cmd_newbooking(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text(
        "➕ *Tạo booking mới*\n\n"
        "👤 Tên khách?",
        parse_mode="Markdown",
    )
    return ADM_NAME

async def adm_get_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["guest_name"] = update.message.text.strip()
    await update.message.reply_text(
        "📅 Check-in?\n_(ví dụ: 2/4, 15/5/2026)_",
        parse_mode="Markdown",
    )
    return ADM_CHECKIN

async def adm_get_checkin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    d = ai_parse_date(update.message.text)
    if not d:
        await update.message.reply_text("❌ Không nhận ra ngày. Thử lại: `2/4` hoặc `15/5/2026`", parse_mode="Markdown")
        return ADM_CHECKIN
    context.user_data["checkin"] = d
    await update.message.reply_text(
        "📅 Check-out?\n_(ví dụ: 5/4)_",
        parse_mode="Markdown",
    )
    return ADM_CHECKOUT

async def adm_get_checkout(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    d = ai_parse_date(update.message.text)
    if not d or d <= context.user_data["checkin"]:
        await update.message.reply_text("❌ Check-out phải sau check-in. Thử lại:", parse_mode="Markdown")
        return ADM_CHECKOUT
    context.user_data["checkout"] = d

    # Hiển thị phòng trống
    checkin, checkout = context.user_data["checkin"], d
    avail = available_rooms(checkin, checkout)

    if not avail:
        await update.message.reply_text("⚠️ Không còn phòng trống cho khoảng thời gian này.")
        return ConversationHandler.END

    keyboard = [
        [InlineKeyboardButton(
            f"{r['name']} — {fmt_vnd(r['price'])}/đêm ({r['beds']}, {r['capacity']} khách)",
            callback_data=f"adm_room_{r['id']}"
        )]
        for r in avail
    ]
    await update.message.reply_text(
        "🏠 Chọn phòng:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return ADM_ROOM

async def adm_get_room(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    room_id = query.data.replace("adm_room_", "")
    context.user_data["room_id"] = room_id
    await query.edit_message_text(f"✅ Đã chọn: *{ROOMS[room_id]['name']}*", parse_mode="Markdown")
    await query.message.reply_text("👥 Số khách?")
    return ADM_GUESTS

async def adm_get_guests(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        n = int(re.search(r'\d+', update.message.text).group())
    except (AttributeError, ValueError):
        await update.message.reply_text("❌ Nhập số, ví dụ: `2`", parse_mode="Markdown")
        return ADM_GUESTS
    context.user_data["guests"] = n

    keyboard = [
        [InlineKeyboardButton("Booking.com", callback_data="adm_src_booking"),
         InlineKeyboardButton("Direct / Zalo", callback_data="adm_src_direct")],
        [InlineKeyboardButton("Walk-in", callback_data="adm_src_walkin"),
         InlineKeyboardButton("Khác", callback_data="adm_src_other")],
    ]
    await update.message.reply_text("📌 Nguồn booking?", reply_markup=InlineKeyboardMarkup(keyboard))
    return ADM_SOURCE

async def adm_get_source(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    source_map = {
        "adm_src_booking": "Booking.com",
        "adm_src_direct":  "Direct",
        "adm_src_walkin":  "Walk-in",
        "adm_src_other":   "Other",
    }
    context.user_data["source"] = source_map.get(query.data, "Direct")
    context.user_data["created_by"] = "admin"

    data = context.user_data
    summary = booking_summary_text("(preview)", data)
    keyboard = [
        [InlineKeyboardButton("✅ Xác nhận", callback_data="adm_confirm_yes"),
         InlineKeyboardButton("❌ Huỷ", callback_data="adm_confirm_no")],
    ]
    await query.edit_message_text(
        f"📋 *Xác nhận booking?*\n\n{summary}\n📌 Nguồn: {data['source']}",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return ADM_CONFIRM

async def adm_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    if query.data == "adm_confirm_no":
        await query.edit_message_text("❌ Đã huỷ.")
        return ConversationHandler.END

    await query.edit_message_text("⏳ Đang tạo booking...")
    try:
        booking_id = await finalize_booking(
            chat_id=query.message.chat_id,
            data=context.user_data,
            context=context,
        )
        await query.edit_message_text(f"✅ *{booking_id}* đã tạo xong.", parse_mode="Markdown")
    except Exception as e:
        await query.edit_message_text(f"❌ Lỗi: {e}")

    return ConversationHandler.END

# ─── GUEST FLOW (/book) ───────────────────────────────────────────────────────

ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "0"))  # Set trong .env

async def cmd_book(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    context.user_data["created_by"] = "guest"
    await update.message.reply_text(
        "👋 Welcome to Hello Dalat Hostel!\n\n"
        "Let's get your booking done. What's your name?"
    )
    return GST_NAME

async def gst_get_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["guest_name"] = update.message.text.strip()
    await update.message.reply_text(
        f"Nice to meet you, {context.user_data['guest_name']}! 😊\n\n"
        "📅 When are you checking in?\n_(e.g. 2/4 or April 2)_"
    )
    return GST_CHECKIN

async def gst_get_checkin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    d = ai_parse_date(update.message.text)
    if not d:
        await update.message.reply_text("Sorry, I didn't catch that date. Try: `2/4` or `15/5`")
        return GST_CHECKIN
    context.user_data["checkin"] = d
    await update.message.reply_text("📅 And check-out date?")
    return GST_CHECKOUT

async def gst_get_checkout(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    d = ai_parse_date(update.message.text)
    if not d or d <= context.user_data["checkin"]:
        await update.message.reply_text("Check-out must be after check-in. Please try again:")
        return GST_CHECKOUT
    context.user_data["checkout"] = d

    nights = (d - context.user_data["checkin"]).days
    await update.message.reply_text(
        f"Great — {nights} night(s).\n\n👥 How many guests?"
    )
    return GST_GUESTS

async def gst_get_guests(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        n = int(re.search(r'\d+', update.message.text).group())
    except (AttributeError, ValueError):
        await update.message.reply_text("Please enter a number, e.g. `2`")
        return GST_GUESTS
    context.user_data["guests"] = n

    checkin, checkout = context.user_data["checkin"], context.user_data["checkout"]
    avail = available_rooms(checkin, checkout)

    if not avail:
        await update.message.reply_text(
            "Sorry, no rooms available for those dates. "
            "Please contact us directly: +84 969 975 935"
        )
        return ConversationHandler.END

    # Hiển thị phòng còn trống
    keyboard = [
        [InlineKeyboardButton(
            f"{r['name']} — {fmt_vnd(r['price'])}/night ({r['beds']})",
            callback_data=f"gst_room_{r['id']}"
        )]
        for r in avail if r["capacity"] >= n
    ] or [
        [InlineKeyboardButton(
            f"{r['name']} — {fmt_vnd(r['price'])}/night",
            callback_data=f"gst_room_{r['id']}"
        )]
        for r in avail
    ]

    await update.message.reply_text(
        "🏠 Available rooms for your dates:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return GST_ROOM_TYPE

async def gst_get_room(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    room_id = query.data.replace("gst_room_", "")
    context.user_data["room_id"] = room_id
    context.user_data["source"]  = "Direct/Telegram"

    data    = context.user_data
    room    = ROOMS[room_id]
    nights  = (data["checkout"] - data["checkin"]).days
    total   = room["price"] * nights

    await query.edit_message_text(
        f"Perfect! Here's your booking summary:\n\n"
        f"🏠 {room['name']}  ({room['beds']})\n"
        f"📅 {data['checkin'].strftime('%d/%m')} → {data['checkout'].strftime('%d/%m/%Y')}  ({nights} night(s))\n"
        f"👥 {data['guests']} guest(s)\n"
        f"💰 Total: {fmt_vnd(total)}\n\n"
        f"Shall I confirm this booking?",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Confirm", callback_data="gst_confirm_yes"),
             InlineKeyboardButton("❌ Cancel",  callback_data="gst_confirm_no")],
        ]),
    )
    return GST_CONFIRM

async def gst_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    if query.data == "gst_confirm_no":
        await query.edit_message_text("Booking cancelled. Feel free to start again with /book.")
        return ConversationHandler.END

    await query.edit_message_text("⏳ Confirming your booking...")
    try:
        await finalize_booking(
            chat_id=query.message.chat_id,
            data=context.user_data,
            context=context,
            notify_admin=True,
            admin_chat_id=ADMIN_CHAT_ID,
        )
    except Exception as e:
        await query.edit_message_text(
            f"Something went wrong. Please contact us directly:\n"
            f"📞 {HOSTEL['phone']}\n\nError: {e}"
        )

    return ConversationHandler.END

# ─── CANCEL ──────────────────────────────────────────────────────────────────

async def booking_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text("❌ Đã huỷ.")
    return ConversationHandler.END

# ─── HANDLERS để import vào bot.py ───────────────────────────────────────────

# ─── CÁC HÀM CHẶN TEXT KHI ĐANG CHỜ BẤM NÚT ──────────────────────────────────
async def adm_wrong_room(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("⚠️ Vui lòng bấm vào nút chọn phòng ở trên, hoặc gõ /cancel để huỷ.")
    return ADM_ROOM

async def adm_wrong_source(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("⚠️ Vui lòng bấm chọn nguồn booking ở menu trên.")
    return ADM_SOURCE

async def adm_wrong_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("⚠️ Vui lòng bấm Xác nhận hoặc Huỷ ở menu trên.")
    return ADM_CONFIRM

async def gst_wrong_room(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("⚠️ Please click a room button above, or type /cancel.")
    return GST_ROOM_TYPE

async def gst_wrong_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("⚠️ Please click Confirm or Cancel above.")
    return GST_CONFIRM

# ─── HANDLERS để import vào bot.py ───────────────────────────────────────────

def get_admin_booking_handler() -> ConversationHandler:
    """ConversationHandler cho luồng admin — thêm vào application."""
    return ConversationHandler(
        entry_points=[CommandHandler("newbooking", cmd_newbooking)],
        states={
            ADM_NAME:    [MessageHandler(filters.TEXT & ~filters.COMMAND, adm_get_name)],
            ADM_CHECKIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, adm_get_checkin)],
            ADM_CHECKOUT:[MessageHandler(filters.TEXT & ~filters.COMMAND, adm_get_checkout)],
            ADM_ROOM:    [
                CallbackQueryHandler(adm_get_room, pattern=r"^adm_room_"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, adm_wrong_room) # Bẫy chặn text
            ],
            ADM_GUESTS:  [MessageHandler(filters.TEXT & ~filters.COMMAND, adm_get_guests)],
            ADM_SOURCE:  [
                CallbackQueryHandler(adm_get_source, pattern=r"^adm_src_"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, adm_wrong_source) # Bẫy chặn text
            ],
            ADM_CONFIRM: [
                CallbackQueryHandler(adm_confirm, pattern=r"^adm_confirm_"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, adm_wrong_confirm) # Bẫy chặn text
            ],
        },
        fallbacks=[CommandHandler("cancel", booking_cancel)],
        name="admin_booking",
        per_user=True,
    )

def get_guest_booking_handler() -> ConversationHandler:
    """ConversationHandler cho luồng khách — thêm vào application."""
    return ConversationHandler(
        entry_points=[CommandHandler("book", cmd_book)],
        states={
            GST_NAME:     [MessageHandler(filters.TEXT & ~filters.COMMAND, gst_get_name)],
            GST_CHECKIN:  [MessageHandler(filters.TEXT & ~filters.COMMAND, gst_get_checkin)],
            GST_CHECKOUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, gst_get_checkout)],
            GST_GUESTS:   [MessageHandler(filters.TEXT & ~filters.COMMAND, gst_get_guests)],
            GST_ROOM_TYPE:[
                CallbackQueryHandler(gst_get_room, pattern=r"^gst_room_"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, gst_wrong_room) # Bẫy chặn text
            ],
            GST_CONFIRM:  [
                CallbackQueryHandler(gst_confirm, pattern=r"^gst_confirm_"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, gst_wrong_confirm) # Bẫy chặn text
            ],
        },
        fallbacks=[CommandHandler("cancel", booking_cancel)],
        name="guest_booking",
        per_user=True,
    )

def register_booking_callbacks(application) -> None:
    """Đăng ký callback patterns cho booking vào application."""
    application.add_handler(CallbackQueryHandler(adm_get_room,    pattern=r"^adm_room_"))
    application.add_handler(CallbackQueryHandler(adm_get_source,  pattern=r"^adm_src_"))
    application.add_handler(CallbackQueryHandler(adm_confirm,     pattern=r"^adm_confirm_"))
    application.add_handler(CallbackQueryHandler(gst_get_room,    pattern=r"^gst_room_"))
    application.add_handler(CallbackQueryHandler(gst_confirm,     pattern=r"^gst_confirm_"))
