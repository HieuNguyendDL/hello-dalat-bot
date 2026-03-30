import os
import re
import asyncio
import anthropic
import firebase_admin
from firebase_admin import credentials, db
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

load_dotenv()

TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
RENDER_URL       = os.environ.get("RENDER_URL", "")
FIREBASE_DB_URL  = os.environ.get("FIREBASE_DB_URL", "")

VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")

# ── Firebase RTDB ─────────────────────────────────────────────────────────────
if not firebase_admin._apps:
    try:
        firebase_key_path = "/etc/secrets/firebase-adminsdk.json"
        if not os.path.exists(firebase_key_path):
            firebase_key_path = os.environ.get("FIREBASE_CRED_PATH", "firebase-adminsdk.json")
        cred = credentials.Certificate(firebase_key_path)
        firebase_admin.initialize_app(cred, {"databaseURL": FIREBASE_DB_URL})
        print("✅ Firebase RTDB kết nối thành công!")
    except Exception as e:
        print(f"⚠️ Lỗi kết nối Firebase: {e}")

# ── Lead Recovery state ───────────────────────────────────────────────────────
WAITING_TIME = 1
leads: dict        = {}
lead_counter: int  = 0
pending_content: dict = {}
anthropic_client   = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

HOSTEL_INFO = """
Bạn là trợ lý của Hello Dalat Hostel — hostel tại Đà Lạt.
Thông tin hostel:
- Địa chỉ: 18/2 Hẻm 33 Phan Đình Phùng, Phường 1, Đà Lạt
- SĐT: 0969 975 935
- Email: hellodalathostel@gmail.com

Nhiệm vụ: Soạn 1 tin nhắn follow-up ngắn gọn, thân thiện, tự nhiên bằng tiếng Việt.
Mục tiêu: Nhắc nhẹ khách quan tâm, gợi mở để khách reply. Không quá sales, không spam.
"""

def generate_followup(lead_content: str) -> str:
    response = anthropic_client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=300,
        system=HOSTEL_INFO,
        messages=[{"role": "user", "content": f"Khách đã nhắn: '{lead_content}'\n\nSoạn tin follow-up phù hợp."}],
    )
    return response.content[0].text

def parse_datetime(text: str) -> datetime | None:
    now  = datetime.now(VN_TZ)
    text = text.strip().lower()
    text = re.sub(r"(\d+)h(\d+)", r"\1:\2", text)
    text = re.sub(r"(\d+)h\b",    r"\1:00", text)

    time_match = re.search(r"(\d{1,2}):(\d{2})", text)
    hour   = int(time_match.group(1)) if time_match else None
    minute = int(time_match.group(2)) if time_match else 0

    date_match = re.search(r"(\d{1,2})/(\d{1,2})", text)
    if date_match:
        day, month = int(date_match.group(1)), int(date_match.group(2))
        year = now.year + 1 if month < now.month or (month == now.month and day < now.day) else now.year
    elif "mai" in text:
        tomorrow = now + timedelta(days=1)
        day, month, year = tomorrow.day, tomorrow.month, tomorrow.year
        if hour is None:
            hour = 9 if "sáng" in text else 14 if "chiều" in text else 20 if "tối" in text else None
    else:
        day, month, year = now.day, now.month, now.year

    if hour is None:
        return None
    try:
        dt = datetime(year, month, day, hour, minute, tzinfo=VN_TZ)
        return dt if dt > now else None
    except ValueError:
        return None

def restore_leads_from_firebase(application: Application):
    global lead_counter
    try:
        ref  = db.reference("bot_leads")
        data = ref.get()
        if not data:
            return

        now = datetime.now(VN_TZ)
        restored = 0
        for lead_id, info in data.items():
            num = int(lead_id.replace("L", ""))
            if num > lead_counter:
                lead_counter = num

            remind_at = datetime.fromisoformat(info["remind_at"])
            leads[lead_id] = {
                "content":   info["content"],
                "time":      datetime.fromisoformat(info["time"]),
                "remind_at": remind_at,
                "status":    info.get("status"),
                "chat_id":   info["chat_id"],
                "job":       None,
            }

            if info.get("status") == "pending" and remind_at > now:
                job = application.job_queue.run_once(
                    send_followup_reminder,
                    when=(remind_at - now),
                    data={"lead_id": lead_id, "chat_id": info["chat_id"]},
                    name=lead_id,
                )
                leads[lead_id]["job"] = job
                restored += 1

        print(f"🔄 Khôi phục {restored} lịch nhắc từ Firebase")
    except Exception as e:
        print(f"⚠️ Lỗi khôi phục lead: {e}")

# ── Reminder callback ─────────────────────────────────────────────────────────
async def send_followup_reminder(context: ContextTypes.DEFAULT_TYPE):
    job_data = context.job.data
    lead_id  = job_data["lead_id"]
    chat_id  = job_data["chat_id"]

    if lead_id not in leads or leads[lead_id]["status"] != "pending":
        return

    lead = leads[lead_id]
    try:
        followup_text = generate_followup(lead["content"])
    except Exception as e:
        print(f"Lỗi AI: {e}")
        followup_text = "Dạ chào bạn, mình là lễ tân bên Hello Dalat Hostel. Không biết bạn đã chọn được phòng ưng ý chưa ạ? Cần tư vấn thêm cứ nhắn mình nhé!"

    kb = [
        [InlineKeyboardButton("🔄 Soạn lại", callback_data=f"compose_{lead_id}"),
         InlineKeyboardButton("⏭️ Bỏ qua",   callback_data=f"skip_{lead_id}")],
        [InlineKeyboardButton("✅ Đã chốt",   callback_data=f"close_{lead_id}")],
    ]
    text = (
        f"🔔 *Nhắc follow-up — {lead_id}*\n\n"
        f"📩 _{lead['content']}_\n\n"
        f"✍️ *Tin gợi ý:*\n```\n{followup_text}\n```"
    )
    await context.bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(kb),
    )

# ── Conversation: nhập lead + thời gian ──────────────────────────────────────
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    pending_content[update.message.chat_id] = update.message.text
    await update.message.reply_text("⏰ Nhắc lúc mấy giờ? (vd: 14h ngày mai, 20:00)")
    return WAITING_TIME

async def handle_time_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    global lead_counter
    chat_id   = update.message.chat_id
    remind_dt = parse_datetime(update.message.text)

    if not remind_dt:
        await update.message.reply_text("❌ Không hiểu giờ. Thử lại (vd: 14:00 ngày mai):")
        return WAITING_TIME

    content      = pending_content.pop(chat_id, "")
    lead_counter += 1
    lead_id      = f"L{lead_counter:03d}"
    now          = datetime.now(VN_TZ)

    job = context.job_queue.run_once(
        send_followup_reminder,
        when=(remind_dt - now),
        data={"lead_id": lead_id, "chat_id": chat_id},
        name=lead_id,
    )
    leads[lead_id] = {
        "content":   content,
        "time":      now,
        "remind_at": remind_dt,
        "status":    "pending",
        "chat_id":   chat_id,
        "job":       job,
    }

    try:
        db.reference(f"bot_leads/{lead_id}").set({
            "content":   content,
            "time":      now.isoformat(),
            "remind_at": remind_dt.isoformat(),
            "status":    "pending",
            "chat_id":   chat_id,
        })
    except Exception as e:
        print(f"Lỗi lưu Firebase: {e}")

    await update.message.reply_text(
        f"✅ Đã lưu *{lead_id}* — nhắc lúc *{remind_dt.strftime('%H:%M %d/%m')}*",
        parse_mode="Markdown",
    )
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    pending_content.pop(update.message.chat_id, None)
    await update.message.reply_text("❌ Đã huỷ.")
    return ConversationHandler.END

# ── Inline button callbacks ───────────────────────────────────────────────────
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query  = update.callback_query
    await query.answer()

    action, lead_id = query.data.split("_", 1)
    if lead_id not in leads:
        return

    if action == "compose":
        await query.edit_message_text("⏳ Đang soạn lại...")
        try:
            txt = generate_followup(leads[lead_id]["content"])
            await query.edit_message_text(
                f"✍️ *Gợi ý mới:*\n```\n{txt}\n```", parse_mode="Markdown"
            )
        except Exception as e:
            print(f"Lỗi AI: {e}")
            await query.edit_message_text("❌ AI lỗi, thử lại sau.")

    elif action in ("skip", "close"):
        new_status = "skipped" if action == "skip" else "closed"
        leads[lead_id]["status"] = new_status
        if leads[lead_id].get("job"):
            try:
                leads[lead_id]["job"].schedule_removal()
            except Exception:
                pass
        try:
            db.reference(f"bot_leads/{lead_id}/status").set(new_status)
        except Exception as e:
            print(f"Lỗi cập nhật Firebase: {e}")
        label = "bỏ qua" if action == "skip" else "chốt"
        await query.edit_message_text(
            f"✅ *{lead_id}* — Đã {label}.", parse_mode="Markdown"
        )

# ── Commands ──────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 *Hello Dalat Assistant*\n\n"
        "• *Forward* hoặc *gõ tóm tắt* tin nhắn khách → lên lịch nhắc follow-up\n"
        "• `/list` — Xem danh sách lead\n"
        "• `/close L001` — Đánh dấu đã chốt\n"
        "• `/cancel` — Huỷ thao tác đang làm",
        parse_mode="Markdown",
    )

async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not leads:
        return await update.message.reply_text("Chưa có lead nào.")
    lines = ["📋 *Danh sách Lead:*"]
    for lid, ld in leads.items():
        icon = {"pending": "⏳", "closed": "✅", "skipped": "⏭️"}.get(ld["status"], "❓")
        snippet = ld["content"][:35] + ("…" if len(ld["content"]) > 35 else "")
        lines.append(f"{icon} *{lid}* ({ld['remind_at'].strftime('%H:%M %d/%m')}): {snippet}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def cmd_close(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        return await update.message.reply_text("Dùng: /close L001")
    lead_id = context.args[0].upper()
    if lead_id not in leads:
        return await update.message.reply_text(f"❌ Không tìm thấy {lead_id}")
    leads[lead_id]["status"] = "closed"
    if leads[lead_id].get("job"):
        try:
            leads[lead_id]["job"].schedule_removal()
        except Exception:
            pass
    try:
        db.reference(f"bot_leads/{lead_id}/status").set("closed")
    except Exception as e:
        print(f"Lỗi Firebase: {e}")
    await update.message.reply_text(f"✅ *{lead_id}* đã chốt.", parse_mode="Markdown")

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    application = Application.builder().token(TELEGRAM_TOKEN).build()

    restore_leads_from_firebase(application)

    # Lead Recovery conversation
    lead_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)],
        states={WAITING_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_time_input)]},
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    application.add_handler(lead_conv)
    application.add_handler(CallbackQueryHandler(handle_callback, pattern="^(compose|skip|close)_"))

    application.add_handler(CommandHandler("start",  cmd_start))
    application.add_handler(CommandHandler("list",   cmd_list))
    application.add_handler(CommandHandler("close",  cmd_close))

    port        = int(os.environ.get("PORT", 8080))
    webhook_url = f"{RENDER_URL.rstrip('/')}/{TELEGRAM_TOKEN}"

    print("🚀 Bot khởi động (Webhook mode)…")
    application.run_webhook(
        listen="0.0.0.0",
        port=port,
        url_path=TELEGRAM_TOKEN,
        webhook_url=webhook_url,
    )

if __name__ == "__main__":
    main()
