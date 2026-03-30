"""
=============================================================================
HƯỚNG DẪN TÍCH HỢP VÀO bot.py HIỆN TẠI
=============================================================================

Thêm các import này vào đầu bot.py:
─────────────────────────────────────────────────────────────────────────────

from booking_flow import get_admin_booking_handler
from guest_ai_flow import handle_guest_message, handle_guest_booking_callback
from config import ADMIN_CHAT_ID


─────────────────────────────────────────────────────────────────────────────
Trong hàm setup() hoặc main(), sau khi khởi tạo Application, thêm:
─────────────────────────────────────────────────────────────────────────────

# --- Booking flows ---
application.add_handler(get_admin_booking_handler())

application.add_handler(
    CallbackQueryHandler(
        handle_guest_booking_callback,
        pattern="^guestbook_"
    )
)

# --- Guest AI handler (phải thêm SAU tất cả handler khác) ---
application.add_handler(
    MessageHandler(
        filters.TEXT & ~filters.COMMAND & ~filters.Chat(ADMIN_CHAT_ID),
        handle_guest_message
    )
)


─────────────────────────────────────────────────────────────────────────────
Thêm vào requirements.txt:
─────────────────────────────────────────────────────────────────────────────

anthropic>=0.25.0
firebase-admin>=6.5.0
reportlab>=4.0.0


─────────────────────────────────────────────────────────────────────────────
Environment variables cần thêm trên Render:
─────────────────────────────────────────────────────────────────────────────

ADMIN_CHAT_ID          ← Telegram chat ID của Hiếu (lấy từ @userinfobot)
ANTHROPIC_API_KEY      ← Claude API key
FIREBASE_PROJECT_ID    ← Project ID trên Firebase Console
FIREBASE_CRED_PATH     ← Đường dẫn đến serviceAccountKey.json
                          (upload file lên Render dưới dạng Secret File)


─────────────────────────────────────────────────────────────────────────────
Cấu trúc file cuối cùng:
─────────────────────────────────────────────────────────────────────────────

lead_recovery_render/
├── bot.py                   ← file chính (đã có, thêm imports + handlers)
├── booking_flow.py          ← Luồng 1: Hiếu nhập tay
├── guest_ai_flow.py         ← Luồng 2: Khách + Claude AI
├── firestore_service.py     ← Firestore CRUD
├── pdf_service.py           ← PDF confirmation
├── config.py                ← Room data, constants
├── serviceAccountKey.json   ← Firebase credentials (Secret File trên Render)
└── requirements.txt         ← Updated deps


─────────────────────────────────────────────────────────────────────────────
Lấy ADMIN_CHAT_ID:
─────────────────────────────────────────────────────────────────────────────

1. Mở Telegram
2. Nhắn bất kỳ gì cho @userinfobot
3. Bot trả về chat ID — copy số đó

=============================================================================
"""

# Đây là file hướng dẫn — không cần chạy trực tiếp.
