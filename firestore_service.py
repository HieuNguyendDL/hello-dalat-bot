"""
Hello Dalat Hostel — Firestore Service
Handles all booking CRUD operations.
"""

import logging
from datetime import datetime, date
from zoneinfo import ZoneInfo
from typing import Optional

import firebase_admin
from firebase_admin import credentials, firestore

from config import FIREBASE_CRED_PATH, FIREBASE_PROJECT_ID, VN_TZ

logger = logging.getLogger(__name__)

# ── Init ──────────────────────────────────────────────────────────────────────
_db = None

def get_db():
    global _db
    if _db is None:
        if not firebase_admin._apps:
            cred = credentials.Certificate(FIREBASE_CRED_PATH)
            firebase_admin.initialize_app(cred, {"projectId": FIREBASE_PROJECT_ID})
        _db = firestore.client()
    return _db


# ── Booking ID generator ──────────────────────────────────────────────────────
def generate_booking_id() -> str:
    """HD-YYYYMMDD-NNN (sequence resets daily)"""
    today = datetime.now(VN_TZ).strftime("%Y%m%d")
    db = get_db()
    
    # Count today's bookings to get next sequence
    bookings_ref = db.collection("bookings")
    today_bookings = (
        bookings_ref
        .where("bookingDate", "==", today)
        .stream()
    )
    count = sum(1 for _ in today_bookings) + 1
    return f"HD-{today}-{count:03d}"


# ── Create booking ────────────────────────────────────────────────────────────
def create_booking(data: dict) -> dict:
    """
    Save a new booking to Firestore.
    Returns the saved document with generated bookingId.
    """
    db = get_db()
    
    booking_id = generate_booking_id()
    today = datetime.now(VN_TZ).strftime("%Y%m%d")
    
    doc = {
        "bookingId": booking_id,
        "bookingDate": today,
        # Guest info
        "guestName": data.get("guestName", ""),
        "guestPhone": data.get("guestPhone", ""),
        "guestTelegramId": data.get("guestTelegramId"),
        "guestEmail": data.get("guestEmail", ""),
        # Room info
        "roomId": data.get("roomId", ""),
        "roomName": data.get("roomName", ""),
        "guests": data.get("guests", 1),
        # Dates
        "checkIn": data.get("checkIn", ""),        # "YYYY-MM-DD"
        "checkOut": data.get("checkOut", ""),      # "YYYY-MM-DD"
        "nights": data.get("nights", 1),
        # Pricing
        "pricePerNight": data.get("pricePerNight", 0),
        "grandTotal": data.get("grandTotal", 0),
        # Meta
        "source": data.get("source", "direct_telegram"),
        "status": "confirmed",
        "paymentStatus": "unpaid",
        "paymentMethod": None,
        "notes": data.get("notes", ""),
        "createdBy": data.get("createdBy", "admin"),
        "createdAt": firestore.SERVER_TIMESTAMP,
    }
    
    db.collection("bookings").document(booking_id).set(doc)
    logger.info(f"✅ Booking created: {booking_id}")
    
    doc["bookingId"] = booking_id  # ensure it's in the returned dict
    return doc


# ── Get booking ───────────────────────────────────────────────────────────────
def get_booking(booking_id: str) -> Optional[dict]:
    db = get_db()
    doc = db.collection("bookings").document(booking_id).get()
    return doc.to_dict() if doc.exists else None


# ── Check room availability ───────────────────────────────────────────────────
def check_availability(room_id: str, checkin: str, checkout: str) -> bool:
    """
    Returns True if room is available for the given date range.
    Checks for overlapping confirmed bookings.
    """
    db = get_db()
    
    bookings = (
        db.collection("bookings")
        .where("roomId", "==", room_id)
        .where("status", "in", ["confirmed", "pending"])
        .stream()
    )
    
    for b in bookings:
        existing = b.to_dict()
        existing_in = existing.get("checkIn", "")
        existing_out = existing.get("checkOut", "")
        
        # Overlap: new_in < existing_out AND new_out > existing_in
        if checkin < existing_out and checkout > existing_in:
            return False
    
    return True


def get_available_rooms(checkin: str, checkout: str) -> list[str]:
    """Return list of available room IDs for given date range."""
    from config import ROOMS
    available = []
    for room_id in ROOMS:
        if check_availability(room_id, checkin, checkout):
            available.append(room_id)
    return available


# ── Recent bookings (for dashboard / reports) ────────────────────────────────
def get_recent_bookings(limit: int = 10) -> list[dict]:
    db = get_db()
    docs = (
        db.collection("bookings")
        .order_by("createdAt", direction=firestore.Query.DESCENDING)
        .limit(limit)
        .stream()
    )
    return [d.to_dict() for d in docs]


# ── Cancel booking ────────────────────────────────────────────────────────────
def cancel_booking(booking_id: str, reason: str = "") -> bool:
    db = get_db()
    ref = db.collection("bookings").document(booking_id)
    if not ref.get().exists:
        return False
    ref.update({
        "status": "cancelled",
        "cancelReason": reason,
        "cancelledAt": firestore.SERVER_TIMESTAMP,
    })
    logger.info(f"❌ Booking cancelled: {booking_id}")
    return True
