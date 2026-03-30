"""
book_handler.py — /book command cho Hello Dalat Bot
ConversationHandler 6 bước: phòng → CI → CO → tên → SĐT → nguồn → confirm
"""

import logging
from datetime import datetime, date, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    ContextTypes,
    ConversationHandler,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)
from firebase_client import (
    ROOMS, SOURCES, get_room_name, get_room_price,
    format_currency, format_date_vn,
    create_booking, check_room_availability, _calc_nights,
)

logger = logging.getLogger(__name__)

# States
(
    BOOK_ROOM,
    BOOK_CHECK_IN,
    BOOK_CHECK_OUT,
    BOOK_GUEST_NAME,
    BOOK_PHONE,
    BOOK_SOURCE,
    BOOK_CONFIRM,
) = range(7)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _parse_date(text: str) -> date | None:
    """Parse ngày từ nhiều định dạng: dd/mm, dd/mm/yyyy, YYYY-MM-DD, +N"""
    text = text.strip()
    today = date.today()

    # +N days
    if text.startswith("+"):
        try:
            return today + timedelta(days=int(text[1:]))
        except Exception:
            return None

    # Thử các format
    for fmt in ("%d/%m/%Y", "%d/%m/%y", "%d/%m", "%Y-%m-%d", "%d-%m-%Y"):
        try:
            d = datetime.strptime(text, fmt)
            if fmt == "%d/%m":  # không có năm → dùng năm hiện tại hoặc sang năm
                d = d.replace(year=today.year)
                if d.date() < today:
                    d = d.replace(year=today.year + 1)
            return d.date()
        except ValueError:
            continue
    return None


def _room_keyboard() -> InlineKeyboardMarkup:
    buttons = []
    row = []
    for i, room in enumerate(ROOMS):
        price = format_currency(room["price"])
        btn = InlineKeyboardButton(
            f"P.{room['id']} {room['name']} ({price})",
            callback_data=f"room_{room['id']}",
        )
        row.append(btn)
        if len(row) == 2 or i == len(ROOMS) - 1:
            buttons.append(row)
            row = []
    return InlineKeyboardMarkup(buttons)


def _source_keyboard() -> InlineKeyboardMarkup:
    buttons = [[InlineKeyboardButton(s, callback_data=f"src_{i}")] for i, s in enumerate(SOURCES)]
    return InlineKeyboardMarkup(buttons)


def _summary(ctx: ContextTypes.DEFAULT_TYPE) -> str:
    d = ctx.user_data
    room_id = d.get("room_id", "?")
    ci = format_date_vn(d.get("check_in", ""))
    co = format_date_vn(d.get("check_out", ""))
    nights = _calc_nights(d.get("check_in", ""), d.get("check_out", ""))
    price = get_room_price(room_id)
    total = price * nights
    source = d.get("source", "?")
    name = d.get("guest_name", "?")
    phone = d.get("phone", "?")
    note = d.get("note", "")

    return (
        f"📋 *XÁC NHẬN BOOKING MỚI*\n"
        f"─────────────────────\n"
        f"🛏 Phòng: *{get_room_name(room_id)}*\n"
        f"📅 Check-in: *{ci}*\n"
        f"📅 Check-out: *{co}* ({nights} đêm)\n"
        f"💰 Đơn giá: {format_currency(price)}/đêm\n"
        f"💵 Tổng: *{format_currency(total)}*\n"
        f"─────────────────────\n"
        f"👤 Tên: *{name}*\n"
        f"📱 SĐT: {phone}\n"
        f"🔗 Nguồn: {source}\n"
        f"📝 Ghi chú: {note or '—'}\n"
        f"─────────────────────\n"
        f"Xác nhận tạo booking?"
    )


def _confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Xác nhận", callback_data="book_confirm"),
            InlineKeyboardButton("❌ Hủy", callback_data="book_cancel"),
        ]
    ])


# ── Entry point ────────────────────────────────────────────────────────────────

async def book_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    ctx.user_data.clear()
    await update.message.reply_text(
        "🏨 *Tạo Booking Mới*\n\nChọn phòng:",
        reply_markup=_room_keyboard(),
        parse_mode="Markdown",
    )
    return BOOK_ROOM


# ── Step 1: Chọn phòng ────────────────────────────────────────────────────────

async def book_room_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    room_id = query.data.replace("room_", "")
    ctx.user_data["room_id"] = room_id

    await query.edit_message_text(
        f"✅ Đã chọn: *{get_room_name(room_id)}*\n\n"
        f"📅 Nhập ngày *check-in*:\n"
        f"_(VD: 25/7 hoặc 25/07/2026 hoặc +1 cho ngày mai)_",
        parse_mode="Markdown",
    )
    return BOOK_CHECK_IN


# ── Step 2: Check-in date ──────────────────────────────────────────────────────

async def book_check_in(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    d = _parse_date(update.message.text)
    if not d:
        await update.message.reply_text("❌ Ngày không hợp lệ. Nhập lại (VD: 25/7):")
        return BOOK_CHECK_IN

    if d < date.today():
        await update.message.reply_text("❌ Ngày check-in không được ở quá khứ. Nhập lại:")
        return BOOK_CHECK_IN

    ctx.user_data["check_in"] = d.strftime("%Y-%m-%d")

    await update.message.reply_text(
        f"✅ Check-in: *{format_date_vn(ctx.user_data['check_in'])}*\n\n"
        f"📅 Nhập ngày *check-out*:\n"
        f"_(VD: 27/7 hoặc +2 để thêm 2 đêm)_",
        parse_mode="Markdown",
    )
    return BOOK_CHECK_OUT


# ── Step 3: Check-out date ─────────────────────────────────────────────────────

async def book_check_out(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    ci = date.fromisoformat(ctx.user_data["check_in"])

    # +N → cộng vào check-in
    if text.startswith("+"):
        try:
            co = ci + timedelta(days=int(text[1:]))
        except Exception:
            await update.message.reply_text("❌ Không hiểu. Nhập lại (VD: 27/7 hoặc +2):")
            return BOOK_CHECK_OUT
    else:
        co = _parse_date(text)

    if not co:
        await update.message.reply_text("❌ Ngày không hợp lệ. Nhập lại:")
        return BOOK_CHECK_OUT

    if co <= ci:
        await update.message.reply_text("❌ Check-out phải sau check-in. Nhập lại:")
        return BOOK_CHECK_OUT

    ctx.user_data["check_out"] = co.strftime("%Y-%m-%d")

    # Kiểm tra phòng trống
    room_id = ctx.user_data["room_id"]
    ci_str = ctx.user_data["check_in"]
    co_str = ctx.user_data["check_out"]
    nights = _calc_nights(ci_str, co_str)
    price = get_room_price(room_id)

    checking_msg = await update.message.reply_text("🔍 Đang kiểm tra phòng...")

    available = check_room_availability(room_id, ci_str, co_str)
    await checking_msg.delete()

    if not available:
        await update.message.reply_text(
            f"⚠️ Phòng *{room_id}* đã có khách trong khoảng đó!\n"
            f"Vui lòng chọn phòng khác hoặc đổi ngày.\n\n"
            f"Dùng /book để bắt đầu lại.",
            parse_mode="Markdown",
        )
        return ConversationHandler.END

    await update.message.reply_text(
        f"✅ Check-out: *{format_date_vn(co_str)}*\n"
        f"🌙 {nights} đêm · {format_currency(price * nights)}\n\n"
        f"👤 Nhập *tên khách*:",
        parse_mode="Markdown",
    )
    return BOOK_GUEST_NAME


# ── Step 4: Tên khách ─────────────────────────────────────────────────────────

async def book_guest_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    name = update.message.text.strip()
    if len(name) < 2:
        await update.message.reply_text("❌ Tên quá ngắn. Nhập lại:")
        return BOOK_GUEST_NAME

    ctx.user_data["guest_name"] = name

    await update.message.reply_text(
        f"✅ Tên: *{name}*\n\n📱 Nhập *số điện thoại* (hoặc gõ - nếu không có):",
        parse_mode="Markdown",
    )
    return BOOK_PHONE


# ── Step 5: SĐT ───────────────────────────────────────────────────────────────

async def book_phone(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    phone = update.message.text.strip()
    if phone == "-":
        phone = ""

    ctx.user_data["phone"] = phone

    await update.message.reply_text(
        f"✅ SĐT: {phone or '(không có)'}\n\n🔗 Chọn *nguồn khách*:",
        parse_mode="Markdown",
        reply_markup=_source_keyboard(),
    )
    return BOOK_SOURCE


# ── Step 6: Nguồn ─────────────────────────────────────────────────────────────

async def book_source_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    idx = int(query.data.replace("src_", ""))
    source = SOURCES[idx]
    ctx.user_data["source"] = source

    await query.edit_message_text(
        _summary(ctx),
        parse_mode="Markdown",
        reply_markup=_confirm_keyboard(),
    )
    return BOOK_CONFIRM


# ── Step 7: Xác nhận ──────────────────────────────────────────────────────────

async def book_confirm_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    if query.data == "book_cancel":
        await query.edit_message_text("❌ Đã hủy tạo booking.")
        ctx.user_data.clear()
        return ConversationHandler.END

    # Tạo booking
    d = ctx.user_data
    msg = await query.message.reply_text("⏳ Đang tạo booking...")

    result = create_booking(
        room_id=d["room_id"],
        check_in=d["check_in"],
        check_out=d["check_out"],
        guest_name=d["guest_name"],
        phone=d.get("phone", ""),
        source=d.get("source", "Vãng lai (Walk-in)"),
        note=d.get("note", ""),
        paid=0,
    )

    await msg.delete()

    if result:
        nights = result["nights"]
        total = result["total"]
        await query.edit_message_text(
            f"✅ *Booking đã tạo thành công!*\n\n"
            f"🛏 Phòng: *{get_room_name(d['room_id'])}*\n"
            f"📅 {format_date_vn(d['check_in'])} → {format_date_vn(d['check_out'])} ({nights} đêm)\n"
            f"👤 {d['guest_name']} · {d.get('phone') or '—'}\n"
            f"💵 Tổng: *{format_currency(total)}*\n"
            f"🆔 ID: `{result['bookingId'][:8]}...`\n\n"
            f"_Dashboard sẽ tự cập nhật trong vài giây._",
            parse_mode="Markdown",
        )
    else:
        await query.edit_message_text(
            "❌ Lỗi khi tạo booking. Vui lòng kiểm tra lại kết nối Firebase.\n"
            "Thử lại bằng /book"
        )

    ctx.user_data.clear()
    return ConversationHandler.END


# ── Cancel ────────────────────────────────────────────────────────────────────

async def book_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    ctx.user_data.clear()
    await update.message.reply_text("❌ Đã hủy. Dùng /book để bắt đầu lại.")
    return ConversationHandler.END


# ── ConversationHandler ───────────────────────────────────────────────────────

def get_book_handler() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CommandHandler("book", book_start)],
        states={
            BOOK_ROOM: [CallbackQueryHandler(book_room_callback, pattern=r"^room_")],
            BOOK_CHECK_IN: [MessageHandler(filters.TEXT & ~filters.COMMAND, book_check_in)],
            BOOK_CHECK_OUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, book_check_out)],
            BOOK_GUEST_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, book_guest_name)],
            BOOK_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, book_phone)],
            BOOK_SOURCE: [CallbackQueryHandler(book_source_callback, pattern=r"^src_")],
            BOOK_CONFIRM: [CallbackQueryHandler(book_confirm_callback, pattern=r"^book_(confirm|cancel)$")],
        },
        fallbacks=[CommandHandler("cancel", book_cancel)],
        name="book_conversation",
        persistent=False,
    )
