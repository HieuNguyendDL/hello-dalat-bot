import os
import asyncio
import anthropic
from datetime import datetime, timedelta
from dotenv import load_dotenv
from flask import Flask, request
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    MessageHandler,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    filters,
)

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
RENDER_URL = os.getenv("RENDER_URL")
FOLLOW_UP_DELAY_HOURS = 2

flask_app = Flask(__name__)
leads = {}
lead_counter = 0
application = None
loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)

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
    response = anthropic_client.messages.create(
        model="claude-opus-4-5",
        max_tokens=300,
        system=HOSTEL_INFO,
        messages=[{"role": "user", "content": f"Khách đã nhắn: '{lead_content}'\n\nSoạn tin follow-up phù hợp."}],
    )
    return response.content[0].text

async def send_followup_reminder(context: ContextTypes.DEFAULT_TYPE):
    job_data = context.job.data
    lead_id = job_data["lead_id"]
    chat_id = job_data["chat_id"]

    if lead_id not in leads or leads[lead_id]["status"] == "closed":
        return

    lead = leads[lead_id]
    content_preview = lead["content"][:80] + "..." if len(lead["content"]) > 80 else lead["content"]
    time_str = lead["time"].strftime("%H:%M")

    keyboard = [
        [
            InlineKeyboardButton("✍️ Soạn tin follow-up", callback_data=f"compose_{lead_id}"),
            InlineKeyboardButton("⏭️ Bỏ qua", callback_data=f"skip_{lead_id}"),
        ],
        [InlineKeyboardButton("✅ Đã chốt rồi", callback_data=f"close_{lead_id}")],
    ]

    await context.bot.send_message(
        chat_id=chat_id,
        text=(f"🔔 *Nhắc follow-up*\n\n📩 Khách nhắn lúc *{time_str}*:\n_{content_preview}_\n\nChưa thấy chốt — bạn muốn làm gì?"),
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global lead_counter

    message = update.message
    if not message or not message.text:
        return

    chat_id = message.chat_id
    content = message.text
    lead_counter += 1
    lead_id = f"L{lead_counter:03d}"
    now = datetime.now()
    source = "forward" if message.forward_date else "manual"

    job = context.job_queue.run_once(
        send_followup_reminder,
        when=timedelta(hours=FOLLOW_UP_DELAY_HOURS),
        data={"lead_id": lead_id, "chat_id": chat_id},
        name=lead_id,
    )

    leads[lead_id] = {"content": content, "time": now, "status": "pending", "job": job, "chat_id": chat_id}

    source_label = "📨 Forward từ khách" if source == "forward" else "📝 Ghi chú thủ công"
    remind_time = (now + timedelta(hours=FOLLOW_UP_DELAY_HOURS)).strftime("%H:%M")

    await message.reply_text(
        f"✅ *Lead {lead_id} đã lưu*\n{source_label}\n⏰ Sẽ nhắc follow-up lúc *{remind_time}*\n\nNếu khách chốt trước, gõ `/close {lead_id}` để tắt nhắc.",
        parse_mode="Markdown",
    )

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
                f"✍️ *Tin follow-up gợi ý — {lead_id}:*\n\n```\n{followup_text}\n```\n\n👆 Copy đoạn trên và gửi cho khách trên Messenger/Zalo.",
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
        preview = lead["content"][:50] + "..." if len(lead["content"]) > 50 else lead["content"]
        lines.append(f"{icon} *{lid}* ({lead['time'].strftime('%H:%M')}): {preview}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 *Hello Dalat Lead Recovery Bot*\n\nCách dùng:\n• *Forward* tin nhắn khách vào đây\n• Hoặc *gõ tóm tắt* nội dung khách hỏi\n• Bot sẽ nhắc follow-up sau 2 tiếng nếu chưa chốt\n\nLệnh:\n/list — Xem danh sách lead\n/close L001 — Đánh dấu đã chốt",
        parse_mode="Markdown",
    )

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

async def setup():
    global application

    application = Application.builder().token(TELEGRAM_TOKEN).build()
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
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
    flask_app.run(host="0.0.0.0", port=port)- Địa chỉ: 33/18/2 Phan Đình Phùng, Phường 1, Đà Lạt
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


# ─── SOẠN TIN FOLLOW-UP ──────────────────────────────────────────────────────

def generate_followup(lead_content: str) -> str:
    response = anthropic_client.messages.create(
        model="claude-opus-4-5",
        max_tokens=300,
        system=HOSTEL_INFO,
        messages=[
            {
                "role": "user",
                "content": f"Khách đã nhắn: '{lead_content}'\n\nSoạn tin follow-up phù hợp.",
            }
        ],
    )
    return response.content[0].text


# ─── GỬI NHẮC FOLLOW-UP ──────────────────────────────────────────────────────

async def send_followup_reminder(context: ContextTypes.DEFAULT_TYPE):
    job_data = context.job.data
    lead_id = job_data["lead_id"]
    chat_id = job_data["chat_id"]

    if lead_id not in leads:
        return
    if leads[lead_id]["status"] == "closed":
        return

    lead = leads[lead_id]
    content_preview = lead["content"][:80] + "..." if len(lead["content"]) > 80 else lead["content"]
    time_str = lead["time"].strftime("%H:%M")

    keyboard = [
        [
            InlineKeyboardButton("✍️ Soạn tin follow-up", callback_data=f"compose_{lead_id}"),
            InlineKeyboardButton("⏭️ Bỏ qua", callback_data=f"skip_{lead_id}"),
        ],
        [
            InlineKeyboardButton("✅ Đã chốt rồi", callback_data=f"close_{lead_id}"),
        ],
    ]

    await context.bot.send_message(
        chat_id=chat_id,
        text=(
            f"🔔 *Nhắc follow-up*\n\n"
            f"📩 Khách nhắn lúc *{time_str}*:\n"
            f"_{content_preview}_\n\n"
            f"Chưa thấy chốt — bạn muốn làm gì?"
        ),
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


# ─── XỬ LÝ TIN NHẮN ──────────────────────────────────────────────────────────

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global lead_counter

    message = update.message
    chat_id = message.chat_id
    content = message.text or message.caption or "[Không có text]"

    if content.startswith("/"):
        return

    lead_counter += 1
    lead_id = f"L{lead_counter:03d}"
    now = datetime.now()
    source = "forward" if message.forward_date else "manual"

    job = context.job_queue.run_once(
        send_followup_reminder,
        when=timedelta(hours=FOLLOW_UP_DELAY_HOURS),
        data={"lead_id": lead_id, "chat_id": chat_id},
        name=lead_id,
    )

    leads[lead_id] = {
        "content": content,
        "time": now,
        "status": "pending",
        "job": job,
        "chat_id": chat_id,
    }

    source_label = "📨 Forward từ khách" if source == "forward" else "📝 Ghi chú thủ công"
    remind_time = (now + timedelta(hours=FOLLOW_UP_DELAY_HOURS)).strftime("%H:%M")

    await message.reply_text(
        f"✅ *Lead {lead_id} đã lưu*\n"
        f"{source_label}\n"
        f"⏰ Sẽ nhắc follow-up lúc *{remind_time}*\n\n"
        f"Nếu khách chốt trước, gõ `/close {lead_id}` để tắt nhắc.",
        parse_mode="Markdown",
    )


# ─── XỬ LÝ NÚT INLINE ────────────────────────────────────────────────────────

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    action, lead_id = query.data.split("_", 1)

    if lead_id not in leads:
        await query.edit_message_text("⚠️ Lead này không còn trong hệ thống.")
        return

    lead = leads[lead_id]

    if action == "compose":
        await query.edit_message_text(
            f"⏳ Đang soạn tin follow-up cho *{lead_id}*...",
            parse_mode="Markdown",
        )
        try:
            followup_text = generate_followup(lead["content"])
            await query.edit_message_text(
                f"✍️ *Tin follow-up gợi ý — {lead_id}:*\n\n"
                f"```\n{followup_text}\n```\n\n"
                f"👆 Copy đoạn trên và gửi cho khách trên Messenger/Zalo.",
                parse_mode="Markdown",
            )
        except Exception as e:
            await query.edit_message_text(f"❌ Lỗi: {str(e)}")

    elif action == "skip":
        leads[lead_id]["status"] = "skipped"
        await query.edit_message_text(
            f"⏭️ *{lead_id}* — Đã bỏ qua.", parse_mode="Markdown"
        )

    elif action == "close":
        leads[lead_id]["status"] = "closed"
        job = leads[lead_id].get("job")
        if job:
            job.schedule_removal()
        await query.edit_message_text(
            f"✅ *{lead_id}* — Đã chốt. Không nhắc nữa.", parse_mode="Markdown"
        )


# ─── LỆNH /close ─────────────────────────────────────────────────────────────

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

    await update.message.reply_text(
        f"✅ *{lead_id}* — Đã chốt. Nhắc follow-up đã tắt.",
        parse_mode="Markdown",
    )


# ─── LỆNH /list ──────────────────────────────────────────────────────────────

async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not leads:
        await update.message.reply_text("Chưa có lead nào.")
        return

    lines = ["📋 *Danh sách lead:*\n"]
    for lid, lead in leads.items():
        icon = {"pending": "⏳", "closed": "✅", "skipped": "⏭️"}.get(lead["status"], "❓")
        preview = lead["content"][:50] + "..." if len(lead["content"]) > 50 else lead["content"]
        lines.append(f"{icon} *{lid}* ({lead['time'].strftime('%H:%M')}): {preview}")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ─── LỆNH /start ─────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 *Hello Dalat Lead Recovery Bot*\n\n"
        "Cách dùng:\n"
        "• *Forward* tin nhắn khách vào đây\n"
        "• Hoặc *gõ tóm tắt* nội dung khách hỏi\n"
        "• Bot sẽ nhắc follow-up sau 2 tiếng nếu chưa chốt\n\n"
        "Lệnh:\n"
        "/list — Xem danh sách lead\n"
        "/close L001 — Đánh dấu đã chốt",
        parse_mode="Markdown",
    )


# ─── FLASK WEBHOOK ────────────────────────────────────────────────────────────

@flask_app.route("/")
def index():
    return "Hello Dalat Bot đang chạy ✅"

@flask_app.route(f"/webhook/{TELEGRAM_TOKEN}", methods=["POST"])
def webhook():
    data = request.get_json()
    asyncio.run(process_update(data))
    return "OK"

async def process_update(data: dict):
    update = Update.de_json(data, application.bot)
    await application.process_update(update)


# ─── KHỞI CHẠY ───────────────────────────────────────────────────────────────

async def setup():
    global application

    application = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .build()
    )

    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_handler(CallbackQueryHandler(handle_callback))
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("close", cmd_close))
    application.add_handler(CommandHandler("list", cmd_list))

    await application.initialize()
    await application.bot.set_webhook(
        url=f"{RENDER_URL}/webhook/{TELEGRAM_TOKEN}"
    )
    print(f"✅ Webhook đã set: {RENDER_URL}/webhook/{TELEGRAM_TOKEN}")

if __name__ == "__main__":
    asyncio.run(setup())
    port = int(os.environ.get("PORT", 8080))
    flask_app.run(host="0.0.0.0", port=port)
