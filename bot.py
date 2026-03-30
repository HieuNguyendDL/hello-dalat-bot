import os
import asyncio
import anthropic
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    MessageHandler,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    filters,
)

# Tải các biến môi trường từ file .env (nếu chạy local)
load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
RENDER_URL = os.getenv("RENDER_URL")
VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")

# Trạng thái của ConversationHandler
WAITING_TIME = 1

# Biến toàn cục lưu trữ dữ liệu
leads = {}
lead_counter = 0
pending_content = {}

# Khởi tạo client Anthropic
anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

HOSTEL_INFO = """
Bạn là trợ lý của Hello Dalat Hostel — hostel tại Đà Lạt.
Thông tin hostel:
- Địa chỉ: 33/18/2 Phan Đình Phùng, Phường 1, Đà Lạt
- SĐT: 0969 975 935
- Email: hellodalathostel@gmail.com

Danh sách phòng và giá:
- Family 101: 450.000đ/đêm
- Deluxe Queen 201: 400.000đ/đêm
- Single 102 & 202: 180.000đ/đêm
- Standard Double 301 & 302: 250.000đ/đêm
- Deluxe Double 103 & 203: 300.000đ/đêm

Nhiệm vụ: Soạn 1 tin nhắn follow-up ngắn gọn, thân thiện, tự nhiên bằng tiếng Việt.
Mục tiêu: Nhắc nhẹ khách quan tâm, gợi mở để khách reply.
Yêu cầu:
- Ngắn gọn (3–5 câu)
- Không quá sales, không spam
- Tự nhiên như người thật nhắn
- Có thể đề cập phòng / ngày khách hỏi nếu có trong context
- Kết thúc bằng câu hỏi mở để khách dễ reply
- Không dùng emoji quá nhiều (1–2 cái là đủ)
"""

def generate_followup(lead_content: str) -> str:
    # Đã cập nhật model hợp lệ của Anthropic
    response = anthropic_client.messages.create(
        model="claude-3-5-sonnet-20240620",
        max_tokens=300,
        system=HOSTEL_INFO,
        messages=[{"role": "user", "content": f"Khách đã nhắn: '{lead_content}'\n\nSoạn tin follow-up phù hợp."}],
    )
    return response.content[0].text

def parse_datetime(text: str) -> datetime | None:
    import re
    now = datetime.now(VN_TZ)
    text = text.strip().lower()

    text = re.sub(r'(\d+)h(\d+)', r'\1:\2', text)
    text = re.sub(r'(\d+)h\b', r'\1:00', text)

    time_match = re.search(r'(\d{1,2}):(\d{2})', text)
    hour = int(time_match.group(1)) if time_match else None
    minute = int(time_match.group(2)) if time_match else 0

    date_match = re.search(r'(\d{1,2})/(\d{1,2})', text)

    if date_match:
        day = int(date_match.group(1))
        month = int(date_match.group(2))
        year = now.year
        if month < now.month or (month == now.month and day < now.day):
            year += 1
    elif 'ngày mai' in text or 'mai' in text:
        tomorrow = now + timedelta(days=1)
        day, month, year = tomorrow.day, tomorrow.month, tomorrow.year
        if hour is None:
            if 'sáng' in text: hour, minute = 9, 0
            elif 'chiều' in text: hour, minute = 14, 0
            elif 'tối' in text: hour, minute = 20, 0
    else:
        day, month, year = now.day, now.month, now.year

    if hour is None:
        return None

    try:
        dt = datetime(year, month, day, hour, minute, tzinfo=VN_TZ)
        if dt <= now:
            return None
        return dt
    except ValueError:
        return None

async def send_followup_reminder(context: ContextTypes.DEFAULT_TYPE):
    job_data = context.job.data
    lead_id = job_data["lead_id"]
    chat_id = job_data["chat_id"]

    if lead_id not in leads or leads[lead_id]["status"] == "closed":
        return

    lead = leads[lead_id]
    content_preview = lead["content"][:80] + "..." if len(lead["content"]) > 80 else lead["content"]
    time_str = lead["time"].strftime("%H:%M %d/%m")

    try:
        followup_text = generate_followup(lead["content"])
    except Exception:
        followup_text = None

    keyboard = [
        [
            InlineKeyboardButton("🔄 Soạn lại", callback_data=f"compose_{lead_id}"),
            InlineKeyboardButton("⏭️ Bỏ qua", callback_data=f"skip_{lead_id}"),
        ],
        [InlineKeyboardButton("✅ Đã chốt rồi", callback_data=f"close_{lead_id}")],
    ]

    if followup_text:
        text = (
            f"🔔 *Nhắc follow-up — {lead_id}*\n\n"
            f"📩 Khách nhắn lúc *{time_str}*:\n"
            f"_{content_preview}_\n\n"
            f"✍️ *Tin gợi ý — copy & gửi luôn:*\n"
            f"```\n{followup_text}\n```"
        )
    else:
        text = (
            f"🔔 *Nhắc follow-up — {lead_id}*\n\n"
            f"📩 Khách nhắn lúc *{time_str}*:\n"
            f"_{content_preview}_\n\n"
            f"⚠️ Không soạn được tin tự động. Bấm *Soạn lại* để thử."
        )

    await context.bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )

# ─── CONVERSATION HANDLER ───────────────────────────────

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    message = update.message
    if not message or not message.text:
        return ConversationHandler.END

    content = message.text
    chat_id = message.chat_id
    pending_content[chat_id] = content

    await message.reply_text(
        f"📩 Đã nhận:\n_{content}_\n\n"
        f"⏰ Nhắc lúc mấy giờ, ngày nào?\n\n"
        f"Ví dụ:\n"
        f"• `14:00 ngày 2/4`\n"
        f"• `ngày mai 9h`\n"
        f"• `chiều mai 14h`\n"
        f"• `20:00` (hôm nay)",
        parse_mode="Markdown",
    )
    return WAITING_TIME

async def handle_time_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    global lead_counter

    message = update.message
    chat_id = message.chat_id
    text = message.text

    remind_dt = parse_datetime(text)

    if not remind_dt:
        await message.reply_text(
            "❌ Không nhận ra định dạng giờ. Thử lại:\n"
            "• `14:00 ngày 2/4`\n"
            "• `ngày mai 9h`\n"
            "• `20:00` (hôm nay)",
            parse_mode="Markdown",
        )
        return WAITING_TIME

    content = pending_content.get(chat_id, "")
    lead_counter += 1
    lead_id = f"L{lead_counter:03d}"
    now = datetime.now(VN_TZ)
    delay = remind_dt - now

    job = context.job_queue.run_once(
        send_followup_reminder,
        when=delay,
        data={"lead_id": lead_id, "chat_id": chat_id},
        name=lead_id,
    )

    leads[lead_id] = {
        "content": content,
        "time": now,
        "remind_at": remind_dt,
        "status": "pending",
        "job": job,
        "chat_id": chat_id,
    }

    del pending_content[chat_id]

    remind_str = remind_dt.strftime("%H:%M — %d/%m/%Y")

    await message.reply_text(
        f"✅ *Lead {lead_id} đã lưu*\n"
        f"⏰ Sẽ nhắc lúc *{remind_str}*\n\n"
        f"Nếu khách chốt trước, gõ `/close {lead_id}` để tắt nhắc.",
        parse_mode="Markdown",
    )
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.message.chat_id
    pending_content.pop(chat_id, None)
    await update.message.reply_text("❌ Đã huỷ.")
    return ConversationHandler.END

# ─── CALLBACK HANDLER ───────────────────────────────────────

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    action, lead_id = query.data.split("_", 1)

    if lead_id not in leads:
        await query.edit_message_text("⚠️ Lead này không còn trong hệ thống.")
        return

    lead = leads[lead_id]

    if action == "compose":
        await query.edit_message_text(f"⏳ Đang soạn tin follow-up cho *{lead_id}*...", parse_mode="Markdown")
        try:
            followup_text = generate_followup(lead["content"])
            await query.edit_message_text(
                f"✍️ *Tin follow-up gợi ý — {lead_id}:*\n\n```\n{followup_text}\n```\n\n👆 Copy và gửi cho khách.",
                parse_mode="Markdown",
            )
        except Exception as e:
            await query.edit_message_text(f"❌ Lỗi: {str(e)}")
    elif action == "skip":
        leads[lead_id]["status"] = "skipped"
        await query.edit_message_text(f"⏭️ *{lead_id}* — Đã bỏ qua.", parse_mode="Markdown")
    elif action == "close":
        leads[lead_id]["status"] = "closed"
        job = leads[lead_id].get("job")
        if job:
            job.schedule_removal()
        await query.edit_message_text(f"✅ *{lead_id}* — Đã chốt. Không nhắc nữa.", parse_mode="Markdown")

# ─── LỆNH BOT ───────────────────────────────────────────────

async def cmd_close(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Dùng: /close L001")
        return
    lead_id = context.args[0].upper()
    if lead_id not in leads:
        await update.message.reply_text(f"❌ Không tìm thấy lead {lead_id}.")
        return
    leads[lead_id]["status"] = "closed"
    job = leads[lead_id].get("job")
    if job:
        job.schedule_removal()
    await update.message.reply_text(f"✅ *{lead_id}* — Đã chốt. Nhắc follow-up đã tắt.", parse_mode="Markdown")

async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not leads:
        await update.message.reply_text("Chưa có lead nào.")
        return
    lines = ["📋 *Danh sách lead:*\n"]
    for lid, lead in leads.items():
        icon = {"pending": "⏳", "closed": "✅", "skipped": "⏭️"}.get(lead["status"], "❓")
        preview = lead["content"][:40] + "..." if len(lead["content"]) > 40 else lead["content"]
        remind_str = lead["remind_at"].strftime("%H:%M %d/%m") if "remind_at" in lead else "?"
        lines.append(f"{icon} *{lid}* (nhắc {remind_str}): {preview}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 *Hello Dalat Lead Recovery Bot*\n\n"
        "Cách dùng:\n"
        "• *Forward* hoặc *gõ tóm tắt* tin nhắn khách\n"
        "• Bot hỏi lại giờ nhắc — bạn trả lời tự nhiên\n\n"
        "Ví dụ giờ nhắc:\n"
        "• `14:00 ngày 2/4`\n"
        "• `ngày mai 9h`\n"
        "• `chiều mai 14h`\n\n"
        "Lệnh:\n"
        "/list — Xem danh sách lead\n"
        "/close L001 — Đánh dấu đã chốt\n"
        "/cancel — Huỷ thao tác hiện tại",
        parse_mode="Markdown",
    )

# ─── KHỞI CHẠY BOT VỚI WEBHOOK ──────────────────────────────

def main():
    # Khởi tạo Application
    application = Application.builder().token(TELEGRAM_TOKEN).build()

    # Đăng ký các Handler
    conv_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)],
        states={
            WAITING_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_time_input)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    application.add_handler(conv_handler)
    application.add_handler(CallbackQueryHandler(handle_callback))
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("close", cmd_close))
    application.add_handler(CommandHandler("list", cmd_list))

    # Lấy PORT từ Render (mặc định 8080 nếu không có)
    port = int(os.environ.get("PORT", 8080))
    
    # Định dạng lại URL webhook chuẩn
    render_url_clean = RENDER_URL.rstrip('/') # Đảm bảo không có dấu / ở cuối URL
    webhook_url = f"{render_url_clean}/webhook/{TELEGRAM_TOKEN}"

    print(f"🚀 Đang khởi động Bot...")
    print(f"🔗 Webhook URL: {webhook_url}")

    # Chạy Webhook nội bộ thay vì dùng Flask
    application.run_webhook(
        listen="0.0.0.0",
        port=port,
        webhook_url=webhook_url
    )

if __name__ == "__main__":
    main()
        system=HOSTEL_INFO,
        messages=[{"role": "user", "content": f"Khách đã nhắn: '{lead_content}'\n\nSoạn tin follow-up phù hợp."}],
    )
    return response.content[0].text

def parse_datetime(text: str) -> datetime | None:
    """
    Nhận diện các định dạng:
    - "14:00 ngày 2/4"
    - "2/4 14:00"
    - "14h ngày 2/4"
    - "2/4 14h"
    - "14:00" hoặc "14h" (hôm nay)
    - "ngày mai 9h"
    - "sáng mai 9h", "chiều mai 14h", "tối mai 20h"
    """
    import re
    now = datetime.now(VN_TZ)
    text = text.strip().lower()

    # Chuẩn hóa: "14h" → "14:00", "9h30" → "9:30"
    text = re.sub(r'(\d+)h(\d+)', r'\1:\2', text)
    text = re.sub(r'(\d+)h\b', r'\1:00', text)

    # Lấy giờ:phút
    time_match = re.search(r'(\d{1,2}):(\d{2})', text)
    hour = int(time_match.group(1)) if time_match else None
    minute = int(time_match.group(2)) if time_match else 0

    # Lấy ngày/tháng
    date_match = re.search(r'(\d{1,2})/(\d{1,2})', text)

    # Xác định ngày
    if date_match:
        day = int(date_match.group(1))
        month = int(date_match.group(2))
        year = now.year
        if month < now.month or (month == now.month and day < now.day):
            year += 1
    elif 'ngày mai' in text or 'mai' in text:
        tomorrow = now + timedelta(days=1)
        day, month, year = tomorrow.day, tomorrow.month, tomorrow.year
        # Gợi ý giờ theo buổi nếu không có giờ cụ thể
        if hour is None:
            if 'sáng' in text: hour, minute = 9, 0
            elif 'chiều' in text: hour, minute = 14, 0
            elif 'tối' in text: hour, minute = 20, 0
    else:
        day, month, year = now.day, now.month, now.year

    if hour is None:
        return None

    try:
        dt = datetime(year, month, day, hour, minute, tzinfo=VN_TZ)
        if dt <= now:
            return None
        return dt
    except ValueError:
        return None

async def send_followup_reminder(context: ContextTypes.DEFAULT_TYPE):
    job_data = context.job.data
    lead_id = job_data["lead_id"]
    chat_id = job_data["chat_id"]

    if lead_id not in leads or leads[lead_id]["status"] == "closed":
        return

    lead = leads[lead_id]
    content_preview = lead["content"][:80] + "..." if len(lead["content"]) > 80 else lead["content"]
    time_str = lead["time"].strftime("%H:%M %d/%m")

    # Soạn tin follow-up trước
    try:
        followup_text = generate_followup(lead["content"])
    except Exception:
        followup_text = None

    keyboard = [
        [
            InlineKeyboardButton("🔄 Soạn lại", callback_data=f"compose_{lead_id}"),
            InlineKeyboardButton("⏭️ Bỏ qua", callback_data=f"skip_{lead_id}"),
        ],
        [InlineKeyboardButton("✅ Đã chốt rồi", callback_data=f"close_{lead_id}")],
    ]

    if followup_text:
        text = (
            f"🔔 *Nhắc follow-up — {lead_id}*\n\n"
            f"📩 Khách nhắn lúc *{time_str}*:\n"
            f"_{content_preview}_\n\n"
            f"✍️ *Tin gợi ý — copy & gửi luôn:*\n"
            f"```\n{followup_text}\n```"
        )
    else:
        text = (
            f"🔔 *Nhắc follow-up — {lead_id}*\n\n"
            f"📩 Khách nhắn lúc *{time_str}*:\n"
            f"_{content_preview}_\n\n"
            f"⚠️ Không soạn được tin tự động. Bấm *Soạn lại* để thử."
        )

    await context.bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )

# ─── CONVERSATION HANDLER: nhận tin → hỏi giờ ───────────────────────────────

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    message = update.message
    if not message or not message.text:
        return ConversationHandler.END

    content = message.text
    chat_id = message.chat_id
    pending_content[chat_id] = content

    await message.reply_text(
        f"📩 Đã nhận:\n_{content}_\n\n"
        f"⏰ Nhắc lúc mấy giờ, ngày nào?\n\n"
        f"Ví dụ:\n"
        f"• `14:00 ngày 2/4`\n"
        f"• `ngày mai 9h`\n"
        f"• `chiều mai 14h`\n"
        f"• `20:00` (hôm nay)",
        parse_mode="Markdown",
    )
    return WAITING_TIME

async def handle_time_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    global lead_counter

    message = update.message
    chat_id = message.chat_id
    text = message.text

    remind_dt = parse_datetime(text)

    if not remind_dt:
        await message.reply_text(
            "❌ Không nhận ra định dạng giờ. Thử lại:\n"
            "• `14:00 ngày 2/4`\n"
            "• `ngày mai 9h`\n"
            "• `20:00` (hôm nay)",
            parse_mode="Markdown",
        )
        return WAITING_TIME

    content = pending_content.get(chat_id, "")
    lead_counter += 1
    lead_id = f"L{lead_counter:03d}"
    now = datetime.now(VN_TZ)
    delay = remind_dt - now

    job = context.job_queue.run_once(
        send_followup_reminder,
        when=delay,
        data={"lead_id": lead_id, "chat_id": chat_id},
        name=lead_id,
    )

    leads[lead_id] = {
        "content": content,
        "time": now,
        "remind_at": remind_dt,
        "status": "pending",
        "job": job,
        "chat_id": chat_id,
    }

    del pending_content[chat_id]

    remind_str = remind_dt.strftime("%H:%M — %d/%m/%Y")

    await message.reply_text(
        f"✅ *Lead {lead_id} đã lưu*\n"
        f"⏰ Sẽ nhắc lúc *{remind_str}*\n\n"
        f"Nếu khách chốt trước, gõ `/close {lead_id}` để tắt nhắc.",
        parse_mode="Markdown",
    )
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.message.chat_id
    pending_content.pop(chat_id, None)
    await update.message.reply_text("❌ Đã huỷ.")
    return ConversationHandler.END

# ─── CALLBACK ────────────────────────────────────────────────────────────────

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    action, lead_id = query.data.split("_", 1)

    if lead_id not in leads:
        await query.edit_message_text("⚠️ Lead này không còn trong hệ thống.")
        return

    lead = leads[lead_id]

    if action == "compose":
        await query.edit_message_text(f"⏳ Đang soạn tin follow-up cho *{lead_id}*...", parse_mode="Markdown")
        try:
            followup_text = generate_followup(lead["content"])
            await query.edit_message_text(
                f"✍️ *Tin follow-up gợi ý — {lead_id}:*\n\n```\n{followup_text}\n```\n\n👆 Copy và gửi cho khách trên Messenger/Zalo.",
                parse_mode="Markdown",
            )
        except Exception as e:
            await query.edit_message_text(f"❌ Lỗi: {str(e)}")
    elif action == "skip":
        leads[lead_id]["status"] = "skipped"
        await query.edit_message_text(f"⏭️ *{lead_id}* — Đã bỏ qua.", parse_mode="Markdown")
    elif action == "close":
        leads[lead_id]["status"] = "closed"
        job = leads[lead_id].get("job")
        if job:
            job.schedule_removal()
        await query.edit_message_text(f"✅ *{lead_id}* — Đã chốt. Không nhắc nữa.", parse_mode="Markdown")

# ─── LỆNH ────────────────────────────────────────────────────────────────────

async def cmd_close(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Dùng: /close L001")
        return
    lead_id = context.args[0].upper()
    if lead_id not in leads:
        await update.message.reply_text(f"❌ Không tìm thấy lead {lead_id}.")
        return
    leads[lead_id]["status"] = "closed"
    job = leads[lead_id].get("job")
    if job:
        job.schedule_removal()
    await update.message.reply_text(f"✅ *{lead_id}* — Đã chốt. Nhắc follow-up đã tắt.", parse_mode="Markdown")

async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not leads:
        await update.message.reply_text("Chưa có lead nào.")
        return
    lines = ["📋 *Danh sách lead:*\n"]
    for lid, lead in leads.items():
        icon = {"pending": "⏳", "closed": "✅", "skipped": "⏭️"}.get(lead["status"], "❓")
        preview = lead["content"][:40] + "..." if len(lead["content"]) > 40 else lead["content"]
        remind_str = lead["remind_at"].strftime("%H:%M %d/%m") if "remind_at" in lead else "?"
        lines.append(f"{icon} *{lid}* (nhắc {remind_str}): {preview}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 *Hello Dalat Lead Recovery Bot*\n\n"
        "Cách dùng:\n"
        "• *Forward* hoặc *gõ tóm tắt* tin nhắn khách\n"
        "• Bot hỏi lại giờ nhắc — bạn trả lời tự nhiên\n\n"
        "Ví dụ giờ nhắc:\n"
        "• `14:00 ngày 2/4`\n"
        "• `ngày mai 9h`\n"
        "• `chiều mai 14h`\n\n"
        "Lệnh:\n"
        "/list — Xem danh sách lead\n"
        "/close L001 — Đánh dấu đã chốt\n"
        "/cancel — Huỷ thao tác hiện tại",
        parse_mode="Markdown",
    )

# ─── FLASK ───────────────────────────────────────────────────────────────────

@flask_app.route("/")
def index():
    return "Hello Dalat Bot đang chạy ✅"

@flask_app.route(f"/webhook/{TELEGRAM_TOKEN}", methods=["POST"])
def webhook():
    data = request.get_json()
    loop.run_until_complete(process_update(data))
    return "OK"

async def process_update(data: dict):
    update = Update.de_json(data, application.bot)
    await application.process_update(update)

# ─── SETUP ───────────────────────────────────────────────────────────────────

async def setup():
    global application

    application = Application.builder().token(TELEGRAM_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)],
        states={
            WAITING_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_time_input)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    application.add_handler(conv_handler)
    application.add_handler(CallbackQueryHandler(handle_callback))
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("close", cmd_close))
    application.add_handler(CommandHandler("list", cmd_list))

    await application.initialize()
    await application.start()
    await application.bot.set_webhook(url=f"{RENDER_URL}/webhook/{TELEGRAM_TOKEN}")
    print(f"✅ Webhook đã set: {RENDER_URL}/webhook/{TELEGRAM_TOKEN}")

if __name__ == "__main__":
    loop.run_until_complete(setup())
    port = int(os.environ.get("PORT", 8080))
    flask_app.run(host="0.0.0.0", port=port)
