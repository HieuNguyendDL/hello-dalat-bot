"""
Hello Dalat Hostel — Bot Configuration
"""

import os
from zoneinfo import ZoneInfo

# ── Timezone ────────────────────────────────────────────────────────────────
VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")

# ── Env vars ─────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
FIREBASE_CRED_PATH = os.environ.get("FIREBASE_CRED_PATH", "serviceAccountKey.json")
FIREBASE_PROJECT_ID = os.environ.get("FIREBASE_PROJECT_ID", "")

# Telegram chat ID của Hiếu (admin) — set trong env
ADMIN_CHAT_ID = int(os.environ.get("ADMIN_CHAT_ID", "0"))

# ── Hostel info ───────────────────────────────────────────────────────────────
HOSTEL_NAME = "Hello Dalat Hostel"
HOSTEL_ADDRESS = "18/2 Hẻm 33 Phan Đình Phùng, Phường 1, Đà Lạt"
HOSTEL_PHONE = "+84 969 975 935"
HOSTEL_EMAIL = "hellodalathostel@gmail.com"
CHECKIN_TIME = "14:00"
CHECKOUT_TIME = "12:00"

# ── Room catalog ──────────────────────────────────────────────────────────────
ROOMS = {
    "101": {
        "name": "Family Room",
        "bed": "2 beds",
        "capacity": 4,
        "price_normal": 450_000,
        "description": "Phòng gia đình, 2 giường, tối đa 4 khách",
    },
    "102": {
        "name": "Single",
        "bed": "1.4m",
        "capacity": 1,
        "price_normal": 180_000,
        "description": "Phòng đơn, giường 1.4m",
    },
    "103": {
        "name": "Deluxe Double",
        "bed": "1.6m",
        "capacity": 2,
        "price_normal": 300_000,
        "description": "Phòng đôi deluxe, giường 1.6m",
    },
    "201": {
        "name": "Deluxe Queen",
        "bed": "2m×2m",
        "capacity": 2,
        "price_normal": 400_000,
        "description": "Phòng queen deluxe, giường 2m×2m",
    },
    "202": {
        "name": "Single",
        "bed": "1.4m",
        "capacity": 1,
        "price_normal": 180_000,
        "description": "Phòng đơn, giường 1.4m",
    },
    "203": {
        "name": "Deluxe Double",
        "bed": "1.6m",
        "capacity": 2,
        "price_normal": 300_000,
        "description": "Phòng đôi deluxe, giường 1.6m",
    },
    "301": {
        "name": "Standard Double",
        "bed": "1.6m",
        "capacity": 2,
        "price_normal": 250_000,
        "description": "Phòng đôi standard, giường 1.6m",
    },
    "302": {
        "name": "Standard Double",
        "bed": "1.6m",
        "capacity": 2,
        "price_normal": 250_000,
        "description": "Phòng đôi standard, giường 1.6m",
    },
}

# ── Seasonal pricing multipliers ─────────────────────────────────────────────
# Oct–Feb: peak (×1.30) | Mar–Apr: low (×0.88) | May–Sep: normal (×1.00)
# Weekend (Fri–Sat night): +18% on top of season price
SEASON_MULTIPLIERS = {
    "peak": 1.30,
    "low": 0.88,
    "normal": 1.00,
}
WEEKEND_MULTIPLIER = 1.18

PEAK_MONTHS = {10, 11, 12, 1, 2}
LOW_MONTHS = {3, 4}

# ── Booking sources ───────────────────────────────────────────────────────────
SOURCES = {
    "direct_telegram": "Telegram trực tiếp",
    "booking_com": "Booking.com",
    "walk_in": "Walk-in",
    "zalo": "Zalo",
    "facebook": "Facebook",
    "phone": "Điện thoại",
    "other": "Khác",
}

# ── Room list for display ─────────────────────────────────────────────────────
def get_room_list_text() -> str:
    lines = ["*Danh sách phòng:*\n"]
    for room_id, r in ROOMS.items():
        lines.append(
            f"  *{room_id}* — {r['name']} | {r['bed']} | "
            f"≤{r['capacity']} khách | {r['price_normal']:,}đ/đêm"
        )
    return "\n".join(lines)


def get_season(month: int) -> str:
    if month in PEAK_MONTHS:
        return "peak"
    if month in LOW_MONTHS:
        return "low"
    return "normal"


def calc_room_price(room_id: str, checkin_date) -> int:
    """Tính giá 1 đêm theo mùa + cuối tuần."""
    import datetime
    room = ROOMS.get(room_id)
    if not room:
        return 0
    base = room["price_normal"]
    season = get_season(checkin_date.month)
    price = int(base * SEASON_MULTIPLIERS[season])
    # weekend: Friday(4) or Saturday(5) night
    if checkin_date.weekday() in (4, 5):
        price = int(price * WEEKEND_MULTIPLIER)
    return price
