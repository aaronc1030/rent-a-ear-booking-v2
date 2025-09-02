import os
import uuid
import requests
import phonenumbers
from datetime import datetime, timedelta, time
from zoneinfo import ZoneInfo
from flask import Flask, render_template, request, redirect, url_for, flash, send_file, jsonify
from sqlalchemy import create_engine, Column, String, DateTime
from sqlalchemy.orm import declarative_base, sessionmaker
from email_validator import validate_email, EmailNotValidError
import settings

# --- Flask + DB Setup ---
app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET", "dev-secret")

engine = create_engine(os.getenv("BOOKING_DB", "sqlite:///booking.db"), echo=False, future=True)
Base = declarative_base()
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)

# --- Helpers ---
def ensure_aware_utc(dt):
    """Force datetime to be tz-aware in UTC."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=ZoneInfo("UTC"))
    return dt.astimezone(ZoneInfo("UTC"))

def overlaps(a_start, a_end, b_start, b_end):
    """Safe overlap check between aware datetimes."""
    a_start = ensure_aware_utc(a_start)
    a_end = ensure_aware_utc(a_end)
    b_start = ensure_aware_utc(b_start)
    b_end = ensure_aware_utc(b_end)
    return a_start < b_end and a_end > b_start

# --- Models ---
class Booking(Base):
    __tablename__ = "bookings"
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String, nullable=False)
    email = Column(String, nullable=False)
    phone = Column(String, nullable=False)
    start_utc = Column(DateTime(timezone=True), nullable=False)
    end_utc = Column(DateTime(timezone=True), nullable=False)
    status = Column(String, nullable=False, default="confirmed")
    manage_token = Column(String, nullable=False, unique=True, default=lambda: str(uuid.uuid4()))
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(ZoneInfo("UTC")))

Base.metadata.create_all(engine)

# --- Routes ---
@app.route("/")
def home():
    start_q = request.args.get("start")
    try:
        start_date_local = datetime.strptime(start_q, "%Y-%m-%d").date() if start_q else datetime.now().date()
    except ValueError:
        start_date_local = datetime.now().date()

    slots = available_slots(
        start_date_local,
        settings.DAYS_AHEAD,
        settings.SLOT_MINUTES,
        settings.LEAD_MINUTES,
    )
    return render_template("index.html", slots=slots, timezone=settings.TIMEZONE,
                           start_date=start_date_local.isoformat(), days_ahead=settings.DAYS_AHEAD)

@app.route("/book", methods=["POST"])
def book_post():
    name = request.form.get("name", "").strip()
    email = request.form.get("email", "").strip()
    phone = request.form.get("phone", "").strip()
    slot_start_iso = request.form.get("slot_start_iso")
    slot_end_iso = request.form.get("slot_end_iso")

    if not (name and email and phone and slot_start_iso and slot_end_iso):
        flash("Name, email, phone, and time slot are required.", "error")
        return redirect(url_for("home"))

    try:
        validate_email(email, check_deliverability=False)
    except EmailNotValidError as e:
        flash(f"Invalid email: {e}", "error")
        return redirect(url_for("home"))

    try:
        pn = phonenumbers.parse(phone, "US")
        if not phonenumbers.is_valid_number(pn):
            raise ValueError()
    except Exception:
        flash("Invalid phone number. Use format like 3125550123.", "error")
        return redirect(url_for("home"))

    tz = ZoneInfo(settings.TIMEZONE)
    try:
        start_local = datetime.fromisoformat(slot_start_iso).replace(tzinfo=tz)
        end_local = datetime.fromisoformat(slot_end_iso).replace(tzinfo=tz)
    except Exception:
        flash("Invalid time slot.", "error")
        return redirect(url_for("home"))

    b = Booking(
        name=name,
        email=email,
        phone=phone,
        start_utc=start_local.astimezone(ZoneInfo("UTC")),
        end_utc=end_local.astimezone(ZoneInfo("UTC")),
    )

    with SessionLocal() as session:
        session.add(b)
        session.commit()

    flash("Booking confirmed!", "success")
    return redirect(url_for("home"))

# --- Healthcheck ---
@app.route("/health")
def health():
    return jsonify(ok=True)

# --- Error handler ---
@app.errorhandler(Exception)
def handle_exception(e):
    return jsonify(error=str(e)), 500

# --- Slot Generation ---
def available_slots(start_date_local, days_ahead, slot_minutes, lead_minutes):
    slots = []
    tz = ZoneInfo(settings.TIMEZONE)
    today = datetime.now(tz).date()

    for i in range(days_ahead):
        d = start_date_local + timedelta(days=i)
        ranges = settings.BUSINESS_HOURS.get(d.strftime("%a").lower(), [])

        for r in ranges:
            start_str, end_str = r.split("-")
            t1 = time.fromisoformat(start_str)
            t2 = time.fromisoformat(end_str)
            cur = datetime.combine(d, t1, tzinfo=tz)
            end = datetime.combine(d, t2, tzinfo=tz)
            step = timedelta(minutes=slot_minutes)

            while cur + step <= end:
                slots.append((cur, cur + step))
                cur += step

    return slots

if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0")

