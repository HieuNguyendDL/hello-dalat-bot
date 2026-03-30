import os
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

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
RENDER_URL = os.getenv("RENDER_URL")
FIREBASE_DB_URL = os.getenv("FIREBASE_DB_URL")
VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")

# ─── KHỞI TẠO FIREBASE ───────────────────────────────────────────────────────
if not firebase_admin._apps:
    try:
        cred = credentials.Certificate('firebase-adminsdk.json')
        firebase_admin.initialize_app(cred, {
            'databaseURL': FIREBASE_DB_URL
        })
        print("✅ Đã kết nối Firebase thành công!")
    except Exception as e:
        print(f"⚠️ Lỗi kết nối Firebase (Kiểm tra lại Secret File & DB_URL): {e}")

# Trạng thái Conversation
WAITING_TIME = 1

# Bộ nhớ tạm (vẫn giữ local để chạy, nhưng sẽ đồng bộ Firebase)
leads = {}
lead_counter = 0
pending_content = {}

anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

HOSTEL_INFO = """
Bạn là trợ lý của Hello Dalat Hostel — hostel tại Đà Lạt.
Nhiệm vụ: Soạn 1 tin nhắn follow-up ngắn gọn, thân thiện, tự nhiên bằng tiếng Việt.
Mục tiêu: Nhắc nhẹ khách quan tâm, gợi mở để khách reply. Không quá sales, không spam.
"""

def generate_followup(lead_content: str) -> str:
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
        day, month = int(date_match.group(1)), int(date_match.group(2))
        year = now.year + 1 if month < now.month or (month == now.month and day < now.day) else now.year
    elif 'mai' in text:
        tomorrow = now + timedelta(days=1)
        day, month, year = tomorrow.day, tomorrow.month, tomorrow.year
        if hour is None:
            hour, minute = (9, 0) if 'sáng' in text else (14, 0) if 'chiều' in text else (20, 0) if 'tối' in text else (None, 0)
    else:
        day, month, year = now.day, now.month, now.year

    if hour is None: return None
    try:
        dt = datetime(year, month, day, hour, minute, tzinfo=VN_TZ)
        return dt if dt > now else None
    except ValueError:
        return None

# ─── HÀM KHÔI PHỤC DỮ LIỆU TỪ FIREBASE KHI KHỞI ĐỘNG ─────────────────────────
def restore_leads_from_firebase(application: Application):
    global lead_counter
    try:
        ref = db.reference('bot_leads')
        all_leads = ref.get()
        if not all_leads:
            print("Chưa có dữ liệu lead nào trên Firebase.")
            return

        now = datetime.now(VN_TZ)
        restored_count = 0
        
        # Tìm số lead_counter lớn nhất để đặt lại
        for lead_id, data in all_leads.items():
            num = int(lead_id.replace('L', ''))
            if num > lead_counter:
                lead_counter = num
                
            status = data.get('status')
            remind_at = datetime.fromisoformat(data['remind_at'])
            
            # Khôi phục vào bộ nhớ RAM
            leads[lead_id] = {
                "content": data["content"],
                "time": datetime.fromisoformat(data["time"]),
                "remind_at": remind_at,
                "status": status,
                "chat_id": data["chat_id"],
                "job": None
            }
            
            # Chỉ đặt lại đồng hồ báo thức cho những lead chưa hoàn thành và giờ nhắc ở tương lai
            if status == "pending" and remind_at > now:
                delay = remind_at - now
                job = application.job_queue.run_once(
                    send_followup_reminder,
                    when=delay,
                    data={"lead_id": lead_id, "chat_id": data["chat_id"]},
                    name=lead_id,
                )
                leads[lead_id]["job"] = job
                restored_count += 1
                
        print(f"🔄 Khôi phục thành công: {restored_count} lịch nhắc nhở từ Firebase!")
    except Exception as e:
        print(f"⚠️ Lỗi khôi phục Firebase: {e}")

# ─── CÁC XỬ LÝ CHÍNH CỦA BOT ────────────────────────────────────────────────
async def send_followup_reminder(context: ContextTypes.DEFAULT_TYPE):
    job_data = context.job.data
    lead_id = job_data["lead_id"]
    chat_id = job_data["chat_id"]

    if lead_id not in leads or leads[lead_id]["status"] != "pending": return
    lead = leads[lead_id]
    
    try: followup_text = generate_followup(lead["content"])
    except Exception: followup_text = None

    kb = [
        [InlineKeyboardButton("🔄 Soạn lại", callback_data=f"compose_{lead_id}"), InlineKeyboardButton("⏭️ Bỏ qua", callback_data=f"skip_{lead_id}")],
        [InlineKeyboardButton("✅ Đã chốt", callback_data=f"close_{lead_id}")],
    ]
    
    text = (
        f"🔔 *Nhắc follow-up — {lead_id}*\n\n"
        f"📩 _{lead['content']}_\n\n"
        f"✍️ *Tin gợi ý:*\n```\n{followup_text}\n```" if followup_text else "⚠️ Lỗi soạn tin AI."
    )
    await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    content = update.message.text
    pending_content[update.message.chat_id] = content
    await update.message.reply_text("⏰ Nhắc lúc mấy giờ? (vd: 14h ngày mai, 20:00)")
    return WAITING_TIME

async def handle_time_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    global lead_counter
    chat_id = update.message.chat_id
    remind_dt = parse_datetime(update.message.text)

    if not remind_dt:
        await update.message.reply_text("❌ Không hiểu giờ. Thử lại (vd: 14:00 ngày mai):")
        return WAITING_TIME

    content = pending_content.pop(chat_id, "")
    lead_counter += 1
    lead_id = f"L{lead_counter:03d}"
    now = datetime.now(VN_TZ)

    job = context.job_queue.run_once(
        send_followup_reminder, when=(remind_dt - now), data={"lead_id": lead_id, "chat_id": chat_id}, name=lead_id
    )

    leads[lead_id] = {"content": content, "time": now, "remind_at": remind_dt, "status": "pending", "chat_id": chat_id, "job": job}

    # 🔥 LƯU LÊN FIREBASE
    try:
        db.reference(f'bot_leads/{lead_id}').set({
            "content": content,
            "time": now.isoformat(),
            "remind_at": remind_dt.isoformat(),
            "status": "pending",
            "chat_id": chat_id
        })
    except Exception as e:
        print(f"Lỗi lưu Firebase: {e}")

    await update.message.reply_text(f"✅ Đã lưu *{lead_id}* nhắc lúc *{remind_dt.strftime('%H:%M %d/%m')}*", parse_mode="Markdown")
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    pending_content.pop(update.message.chat_id, None)
    await update.message.reply_text("❌ Đã huỷ.")
    return ConversationHandler.END

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    action, lead_id = query.data.split("_", 1)

    if lead_id not in leads: return

    if action == "compose":
        await query.edit_message_text("⏳ Đang soạn lại...")
        try:
            txt = generate_followup(leads[lead_id]["content"])
            await query.edit_message_text(f"✍️ *Gợi ý mới:*\n```\n{txt}\n```", parse_mode="Markdown")
        except Exception: pass
    elif action in ["skip", "close"]:
        leads[lead_id]["status"] = "skipped" if action == "skip" else "closed"
        if leads[lead_id].get("job"): leads[lead_id]["job"].schedule_removal()
        
        # 🔥 CẬP NHẬT TRẠNG THÁI TRÊN FIREBASE
        db.reference(f'bot_leads/{lead_id}/status').set(leads[lead_id]["status"])
        
        await query.edit_message_text(f"✅ *{lead_id}* — Đã {'bỏ qua' if action=='skip' else 'chốt'}.", parse_mode="Markdown")

async def cmd_close(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args: return await update.message.reply_text("Dùng: /close L001")
    lead_id = context.args[0].upper()
    if lead_id in leads:
        leads[lead_id]["status"] = "closed"
        if leads[lead_id].get("job"): leads[lead_id]["job"].schedule_removal()
        db.reference(f'bot_leads/{lead_id}/status').set("closed")
        await update.message.reply_text(f"✅ *{lead_id}* đã chốt.", parse_mode="Markdown")

async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not leads: return await update.message.reply_text("Trống.")
    lines = ["📋 *Danh sách:*"]
    for lid, ld in leads.items():
        icon = {"pending": "⏳", "closed": "✅", "skipped": "⏭️"}.get(ld["status"], "❓")
        lines.append(f"{icon} *{lid}* ({ld['remind_at'].strftime('%H:%M %d/%m')}): {ld['content'][:30]}...")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 Hello Dalat Bot sẵn sàng!")

def main():
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    
    # Khôi phục Job từ Firebase trước khi chạy
    restore_leads_from_firebase(application)

    conv_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)],
        states={WAITING_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_time_input)]},
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    application.add_handler(conv_handler)
    application.add_handler(CallbackQueryHandler(handle_callback))
    application.add_handler(CommandHandler(["start", "close", "list"], lambda u, c: globals()[f"cmd_{u.message.text.split()[0][1:]}"](u, c)))

    port = int(os.environ.get("PORT", 8080))
    webhook_url = f"{RENDER_URL.rstrip('/')}/{TELEGRAM_TOKEN}"

    print(f"🚀 Bot đang khởi động với CSDL Firebase!")
    application.run_webhook(listen="0.0.0.0", port=port, url_path=TELEGRAM_TOKEN, webhook_url=webhook_url)

if __name__ == "__main__":
    import asyncio
    try: loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    main()
