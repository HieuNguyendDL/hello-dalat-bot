"""
firebase_client.py — Firebase REST API client cho Hello Dalat Bot
Không cần Admin SDK, dùng email/password auth để lấy idToken.
"""

import os
import uuid
import time
import logging
import requests
from datetime import datetime

logger = logging.getLogger(__name__)

FIREBASE_API_KEY = os.environ.get("FIREBASE_API_KEY", "")
FIREBASE_DB_URL = os.environ.get("FIREBASE_DB_URL", "").rstrip("/")

# Token cache
_token_cache = {"token": None, "expires_at": 0}


def _get_id_token() -> str | None:
    """Sign in bằng email/password, trả về idToken. Cache 55 phút."""
    now = time.time()
    if _token_cache["token"] and now < _token_cache["expires_at"]:
        return _token_cache["token"]

    email = os.environ.get("FIREBASE_BOT_EMAIL", "")
    password = os.environ.get("FIREBASE_BOT_PASSWORD", "")

    if not email or not password or not FIREBASE_API_KEY:
        logger.error("Thiếu FIREBASE_BOT_EMAIL / FIREBASE_BOT_PASSWORD / FIREBASE_API_KEY")
        return None

    url = f"https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key={FIREBASE_API_KEY}"
    try:
        resp = requests.post(url, json={
            "email": email,
            "password": password,
            "returnSecureToken": True,
        }, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        token = data.get("idToken")
        if token:
            _token_cache["token"] = token
            _token_cache["expires_at"] = now + 55 * 60  # 55 phút
            logger.info("Firebase auth OK")
        return token
    except Exception as e:
        logger.error(f"Firebase auth lỗi: {e}")
        return None


def _db_request(method: str, path: str, data=None) -> dict | None:
    """Gửi request đến Firebase REST API."""
    token = _get_id_token()
    if not token:
        return None

    url = f"{FIREBASE_DB_URL}/{path}.json?auth={token}"
    try:
        if method == "GET":
            resp = requests.get(url, timeout=10)
        elif method == "PATCH":
            resp = requests.patch(url, json=data, timeout=10)
        elif method == "PUT":
            resp = requests.put(url, json=data, timeout=10)
        elif method == "DELETE":
            resp = requests.delete(url, timeout=10)
        else:
            return None

        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.error(f"Firebase DB lỗi [{method} {path}]: {e}")
        return None


# ────────────────────────────────────────────────
# Room helpers
# ────────────────────────────────────────────────

ROOMS = [
    {"id": "101", "name": "Family", "price": 450000, "capacity": 4},
    {"id": "102", "name": "Single", "price": 180000, "capacity": 1},
    {"id": "202", "name": "Single", "price": 180000, "capacity": 1},
    {"id": "301", "name": "Std Double", "price": 250000, "capacity": 2},
    {"id": "302", "name": "Std Double", "price": 250000, "capacity": 2},
    {"id": "103", "name": "Dlx Double", "price": 300000, "capacity": 2},
    {"id": "203", "name": "Dlx Double", "price": 300000, "capacity": 2},
    {"id": "201", "name": "Dlx Queen", "price": 400000, "capacity": 2},
]

ROOM_BY_ID = {r["id"]: r for r in ROOMS}

SOURCES = [
    "Vãng lai (Walk-in)",
    "Gọi điện/Zalo",
    "Booking.com",
    "Facebook",
    "Khách quen",
]


def get_room_price(room_id: str) -> int:
    return ROOM_BY_ID.get(room_id, {}).get("price", 0)


def get_room_name(room_id: str) -> str:
    r = ROOM_BY_ID.get(room_id, {})
    return f"{room_id} - {r.get('name', '?')}"


# ────────────────────────────────────────────────
# Booking creation
# ────────────────────────────────────────────────

def create_booking(
    room_id: str,
    check_in: str,   # YYYY-MM-DD
    check_out: str,  # YYYY-MM-DD
    guest_name: str,
    phone: str,
    source: str,
    note: str = "",
    paid: int = 0,
) -> dict | None:
    """
    Tạo booking mới trong Firebase (theo đúng schema useBookings.ts).
    Trả về dict {"bookingId": ..., "groupId": ...} hoặc None nếu lỗi.
    """
    ts = int(time.time() * 1000)
    group_id = str(uuid.uuid4())
    booking_id = str(uuid.uuid4())

    nights = _calc_nights(check_in, check_out)
    price = get_room_price(room_id)

    booking_entity = {
        "id": booking_id,
        "roomId": room_id,
        "groupId": group_id,
        "checkIn": check_in,
        "checkOut": check_out,
        "hasEarlyCheckIn": False,
        "hasLateCheckOut": False,
        "price": price,
        "status": "booked",
        "services": [],
        "discounts": [],
        "surcharge": 0,
        "createdAt": ts,
        "updatedAt": ts,
        "isDeleted": False,
    }

    group_entity = {
        "id": group_id,
        "customer": {
            "name": guest_name,
            "phone": phone,
            "otaBookingNumber": "",
            "source": source,
            "note": note,
        },
        "payment": {
            "paid": paid,
            "depositMethod": "cash",
            "transactionId": None,
        },
        "roomIds": {booking_id: room_id},
        "status": "active",
        "createdAt": ts,
        "updatedAt": ts,
    }

    # Atomic multi-location update
    updates = {
        f"bookings/{booking_id}": booking_entity,
        f"groups/{group_id}": group_entity,
    }

    result = _db_request("PATCH", "", updates)
    if result is not None:
        logger.info(f"Đã tạo booking {booking_id} cho {guest_name} phòng {room_id}")
        return {
            "bookingId": booking_id,
            "groupId": group_id,
            "price": price,
            "nights": nights,
            "total": price * nights,
        }
    return None


def check_room_availability(room_id: str, check_in: str, check_out: str) -> bool:
    """Kiểm tra phòng có trống không. Đơn giản: đọc bookings có roomId = room_id."""
    try:
        # Lấy tất cả bookings (cách đơn giản, đủ dùng với hostel nhỏ)
        result = _db_request("GET", "bookings")
        if not result:
            return True  # Không đọc được → assume available

        for booking in result.values():
            if not isinstance(booking, dict):
                continue
            if booking.get("isDeleted"):
                continue
            if booking.get("roomId") != room_id:
                continue
            if booking.get("status") in ("cancelled",):
                continue

            b_in = booking.get("checkIn", "")
            b_out = booking.get("checkOut", "")
            # Overlap check: check_in < b_out AND check_out > b_in
            if check_in < b_out and check_out > b_in:
                return False  # Bị trùng

        return True
    except Exception as e:
        logger.error(f"Lỗi kiểm tra phòng: {e}")
        return True  # Assume available nếu lỗi


# ────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────

def _calc_nights(check_in: str, check_out: str) -> int:
    try:
        d1 = datetime.strptime(check_in, "%Y-%m-%d")
        d2 = datetime.strptime(check_out, "%Y-%m-%d")
        diff = (d2 - d1).days
        return max(1, diff)
    except Exception:
        return 1


def format_currency(amount: int) -> str:
    return f"{amount:,}đ".replace(",", ".")


def format_date_vn(date_str: str) -> str:
    """YYYY-MM-DD → dd/mm/yyyy"""
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d")
        return d.strftime("%d/%m/%Y")
    except Exception:
        return date_str
