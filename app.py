git add app.py
git commit -m "Rewrite app.py: UTC-aware datetimes, safe overlaps, robust tz & notifications"
git push
import os
import uuid
import requests

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from typing import List, Tuple

from flask import (
    Flask, render_template, request, redirect, url_for, flash, jsonify
)

from sqlalchemy import (
    create_engine, Column, String, DateTime
)
from sqlalchemy.orm import declarative_base, sessionmaker

from email_validator import validate_email, EmailNotValidError
import phonenumbers

import settings  # your settings.py (BUSINESS_HOURS, SLOT_MINUTES, etc.)


# ------------------------------------------------------------------------------
# Flask
# ------------------------------------------------------------------------------
app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET", "dev-secret")


# ------------------------------------------------------------------------------
# Database (SQLite by default; can be PostgreSQL using BOOKING_DB env var)
# ------------------------------------------------------------------------------
engine = create_engine(os.getenv("BOOKING_DB", "sqlite:///booking.db"),
                       echo=False, future=True)
Base = declarative_base()
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)


class Booking(Base):
    __tablename__ = "bookings"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String, nullable=False)
    email = Column(String, nullable=False)
    phone = Column(String, nullable=False)
    start_utc = Column(DateTime(timezone=True), nullable=False)
    end_utc = Column(DateTime(timezone=True), nullable=False)
    status = Column(String, nullable=False, default="confirmed")  # confirmed/canceled
    manage_token = Column(String, nullable=False, unique=True, default=lambda: str(uuid.uuid4()))
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))


Base.metadata.create_all(engine)


# ------------------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------------------

WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def safe_tz(tz_str: str) -> ZoneInfo:
    """Return a ZoneInfo; fall back to settings.TIMEZONE if bad/unknown."""
    try:
        return ZoneInfo(tz_str)
    except Exception:
        return ZoneInfo(settings.TIMEZONE)


def ensure_aware_utc(dt: datetime) -> datetime:
    """Make any datetime offset-aware in UTC (attach UTC if naive)."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def overlaps(a_start, a_end, b_start, b_end):
    # Normalize all datetimes to be timezone-aware UTC
    if a_start.tzinfo is None:
        a_start = a_start.replace(tzinfo=timezone.utc)
    if a_end.tzinfo is None:
        a_end = a_end.replace(tzinfo=timezone.utc)
    if b_start.tzinfo is None:
        b_start = b_start.replace(tzinfo=timezone.utc)
    if b_end.tzinfo is None:
        b_end = b_end.replace(tzinfo=timezone.utc)

    return a_start < b_end and a_end > b_start


def daterange(start_date: datetime, end_date: datetime):
    """Yield date objects from start (inclusive) to end (exclusive)."""
    days = int((end_date - start_date).days)
    for i in range(days):
        yield (start_date + timedelta(days=i)).date()


def parse_range(r: str):
    """
    Parse 'HH:MM-HH:MM' and allow '24:00' as end-of-day.
    Returns two `datetime.time` objects.
    """
    a, b = r.split("-")
    h1, m1 = [int(x) for x in a.split(":")]
    h2, m2 = [int(x) for x in b.split(":")]
    from datetime import time
    start_t = time(hour=h1, minute=m1)
    # Treat 24:00 as end-of-day (use 23:59 so last slot can render)
    if h2 == 24 and m2 == 0:
        end_t = time(hour=23, minute=59)
    else:
        end_t = time(hour=h2, minute=m2)
    return start_t, end_t


def generate_slots_for_date(d, tz: ZoneInfo, slot_minutes: int) -> List[Tuple[datetime, datetime]]:
    """
    Given a date `d` and tz, return list of (start_local, end_local) for that date,
    using BUSINESS_HOURS[weekday]. Each range is 'HH:MM-HH:MM'.
    """
    ranges = settings.BUSINESS_HOURS.get(WEEKDAYS[d.weekday()], [])
    slots: List[Tuple[datetime, datetime]] = []
    step = timedelta(minutes=slot_minutes)

    for r in ranges:
        t1, t2 = parse_range(r)
        cur = datetime.combine(d, t1, tzinfo=tz)
        end = datetime.combine(d, t2, tzinfo=tz)

        # Allow ranges ending at 23:59 or "24:00" shim above
        while cur + step <= end + timedelta(minutes=1):
            slots.append((cur, cur + step))
            cur += step

    return slots


def available_slots(start_date_local: datetime,
                    days_ahead: int,
                    slot_minutes: int,
                    lead_minutes: int,
                    tz_override: str = None,
                    exclude_booking_id: str = None) -> List[Tuple[datetime, datetime]]:
    """
    Compute available local slots from start_date_local across days_ahead.
    Excludes slots that overlap existing confirmed bookings (in UTC).
    """
    tz = safe_tz(tz_override or settings.TIMEZONE)
    now_local = datetime.now(tz) + timedelta(minutes=lead_minutes)

    # Generate candidate local slots across the window
    end_date_local = start_date_local + timedelta(days=days_ahead)
    candidates: List[Tuple[datetime, datetime]] = []
    for d in daterange(start_date_local, end_date_local):
        candidates.extend(generate_slots_for_date(d, tz, slot_minutes))

    # Filter out slots that start before lead time (now_local)
    candidates = [(s, e) for (s, e) in candidates if s >= now_local]

    # Load existing bookings in UTC
    with SessionLocal() as session:
        q = session.query(Booking).filter(Booking.status == "confirmed")
        if exclude_booking_id:
            q = q.filter(Booking.id != exclude_booking_id)
        existing = q.all()

    # Remove any slot that overlaps an existing booking (normalize to UTC for compare)
    free: List[Tuple[datetime, datetime]] = []
    for s_local, e_local in candidates:
        s_utc = s_local.astimezone(timezone.utc)
        e_utc = e_local.astimezone(timezone.utc)
        if any(overlaps(s_utc, e_utc, b.start_utc, b.end_utc) for b in existing):
            continue
        free.append((s_local, e_local))

    return free


def fmt_local(dt_utc: datetime, tz_str: str) -> str:
    """Format a UTC datetime into a readable string in tz_str."""
    tz = safe_tz(tz_str)
    return dt_utc.astimezone(tz).strftime("%b %d, %Y %I:%M %p")


# ------------------------------------------------------------------------------
# Email / SMS (guarded; won't crash if misconfigured)
# ------------------------------------------------------------------------------

def send_email(to_email: str, subject: str, html: str) -> None:
    try:
        key = os.getenv("SENDGRID_API_KEY")
        from_email = os.getenv("EMAIL_FROM")
        if not key or not from_email:
            print("[email] missing SENDGRID_API_KEY or EMAIL_FROM; skipping")
            return
        r = requests.post(
            "https://api.sendgrid.com/v3/mail/send",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={
                "personalizations": [{"to": [{"email": to_email}]}],
                "from": {
                    "email": from_email.split("<")[-1].strip(">").strip(),
                    "name": (from_email.split("<")[0].strip() or "Bookings"),
                },
                "subject": subject,
                "content": [{"type": "text/html", "value": html}],
            },
            timeout=20,
        )
        if r.status_code >= 400:
            print("[email] error:", r.status_code, r.text)
    except Exception as e:
        print("[email] exception:", e)


def send_sms(to_phone: str, body: str) -> None:
    try:
        sid = os.getenv("TWILIO_ACCOUNT_SID")
        token = os.getenv("TWILIO_AUTH_TOKEN")
        from_num = os.getenv("TWILIO_FROM_NUMBER")
        if not (sid and token and from_num):
            print("[sms] missing Twilio creds; skipping")
            return
        url = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"
        r = requests.post(url, data={"To": to_phone, "From": from_num, "Body": body},
                          auth=(sid, token), timeout=20)
        if r.status_code >= 400:
            print("[sms] error:", r.status_code, r.text)
    except Exception as e:
        print("[sms] exception:", e)


# ------------------------------------------------------------------------------
# Routes
# ------------------------------------------------------------------------------

@app.get("/")
def home():
    # Accept ?start=YYYY-MM-DD and ?tz=Area/City
    tz_str = request.args.get("tz") or settings.TIMEZONE
    tz = safe_tz(tz_str)
    start_q = request.args.get("start")

    if start_q:
        try:
            start_date_local = datetime.strptime(start_q, "%Y-%m-%d").date()
        except ValueError:
            start_date_local = datetime.now(tz).date()
    else:
        start_date_local = datetime.now(tz).date()

    # Build slots
    slots = available_slots(
        datetime.combine(start_date_local, datetime.min.time(), tzinfo=tz),
        settings.DAYS_AHEAD,
        settings.SLOT_MINUTES,
        settings.LEAD_MINUTES,
        tz_override=tz_str,
    )

    return render_template(
        "index.html",
        slots=slots,
        timezone=tz_str,
        start_date=start_date_local.isoformat(),
        days_ahead=settings.DAYS_AHEAD,
    )


@app.post("/book")
def book_post():
    name = request.form.get("name", "").strip()
    email = request.form.get("email", "").strip()
    phone = request.form.get("phone", "").strip()
    slot_start_iso = request.form.get("slot_start_iso", "")
    slot_end_iso = request.form.get("slot_end_iso", "")
    tz_str = request.form.get("tz") or settings.TIMEZONE
    tz = safe_tz(tz_str)

    if not (name and email and phone and slot_start_iso and slot_end_iso):
        flash("Name, email, and phone are required.", "error")
        return redirect(url_for("home"))

    try:
        validate_email(email, check_deliverability=False)
    except EmailNotValidError as e:
        flash(f"Email looks invalid: {str(e)}", "error")
        return redirect(url_for("home"))

    # phone (US example)
    try:
        pn = phonenumbers.parse(phone, "US")
        if not phonenumbers.is_possible_number(pn) or not phonenumbers.is_valid_number(pn):
            raise ValueError()
        phone_e164 = phonenumbers.format_number(pn, phonenumbers.PhoneNumberFormat.E164)
    except Exception:
        flash("Please enter a valid US phone number (e.g., 3125550123).", "error")
        return redirect(url_for("home"))

    try:
        start_local = datetime.fromisoformat(slot_start_iso).replace(tzinfo=tz)
        end_local = datetime.fromisoformat(slot_end_iso).replace(tzinfo=tz)
    except Exception:
        flash("Invalid slot time.", "error")
        return redirect(url_for("home"))

    start_utc = start_local.astimezone(timezone.utc)
    end_utc = end_local.astimezone(timezone.utc)

    with SessionLocal() as session:
        # deny overlaps
        existing = session.query(Booking).filter(Booking.status == "confirmed").all()
        if any(overlaps(start_utc, end_utc, b.start_utc, b.end_utc) for b in existing):
            flash("That time overlaps an existing booking.", "error")
            return redirect(url_for("home"))

        b = Booking(
            name=name,
            email=email,
            phone=phone_e164,
            start_utc=start_utc,
            end_utc=end_utc,
            status="confirmed",
            manage_token=str(uuid.uuid4()),
        )
        session.add(b)
        session.commit()

        # notifications (best-effort)
        manage_url = f"{os.getenv('PUBLIC_BASE_URL', '').rstrip('/')}{url_for('manage', token=b.manage_token)}"
        subj = "Your 60-minute session is booked ✅"
        html = (
            f"<p>Hi {b.name},</p>"
            f"<p><b>When:</b> {fmt_local(b.start_utc, tz_str)} – {fmt_local(b.end_utc, tz_str)} ({tz_str})</p>"
            f'<p><a href="{manage_url}">Manage / reschedule / cancel</a></p>'
        )
        send_email(b.email, subj, html)
        send_sms(b.phone, f"Confirmed: {fmt_local(b.start_utc, tz_str)} ({tz_str}). Manage: {manage_url}")

    flash("Booked! Check your email/text for details.", "success")
    return redirect(url_for("home"))


@app.get("/manage/<token>")
def manage(token):
    tz_str = request.args.get("tz") or settings.TIMEZONE
    with SessionLocal() as session:
        b = session.query(Booking).filter_by(manage_token=token).first()
        if not b:
            flash("Manage link not found.", "error")
            return redirect(url_for("home"))
        return render_template("manage.html", booking=b, timezone=tz_str)


@app.get("/reschedule/<token>")
def reschedule(token):
    tz_str = request.args.get("tz") or settings.TIMEZONE
    tz = safe_tz(tz_str)

    with SessionLocal() as session:
        b = session.query(Booking).filter_by(manage_token=token).first()
        if not b:
            flash("Manage link not found.", "error")
            return redirect(url_for("home"))

    start_date_local = datetime.now(tz).date()
    slots = available_slots(
        datetime.combine(start_date_local, datetime.min.time(), tzinfo=tz),
        settings.DAYS_AHEAD,
        settings.SLOT_MINUTES,
        settings.LEAD_MINUTES,
        tz_override=tz_str,
        exclude_booking_id=b.id,
    )

    return render_template(
        "reschedule.html",
        booking=b,
        slots=slots,
        timezone=tz_str,
        start_date=start_date_local.isoformat(),
        days_ahead=settings.DAYS_AHEAD,
    )


@app.post("/reschedule/<token>")
def reschedule_post(token):
    tz_str = request.form.get("tz") or settings.TIMEZONE
    tz = safe_tz(tz_str)

    slot_start_iso = request.form.get("slot_start_iso", "")
    slot_end_iso = request.form.get("slot_end_iso", "")

    try:
        start_local = datetime.fromisoformat(slot_start_iso).replace(tzinfo=tz)
        end_local = datetime.fromisoformat(slot_end_iso).replace(tzinfo=tz)
    except Exception:
        flash("Invalid slot time.", "error")
        return redirect(url_for("manage", token=token))

    start_utc = start_local.astimezone(timezone.utc)
    end_utc = end_local.astimezone(timezone.utc)

    with SessionLocal() as session:
        b = session.query(Booking).filter_by(manage_token=token).first()
        if not b:
            flash("Manage link not found.", "error")
            return redirect(url_for("home"))

        others = session.query(Booking).filter(Booking.id != b.id, Booking.status == "confirmed").all()
        if any(overlaps(start_utc, end_utc, x.start_utc, x.end_utc) for x in others):
            flash("That time overlaps another booking.", "error")
            return redirect(url_for("manage", token=token))

        b.start_utc = start_utc
        b.end_utc = end_utc
        session.commit()

        manage_url = f"{os.getenv('PUBLIC_BASE_URL', '').rstrip('/')}{url_for('manage', token=b.manage_token)}"
        subj = "Your session was rescheduled 🔁"
        html = (
            f"<p>Hi {b.name},</p>"
            f"<p><b>New time:</b> {fmt_local(b.start_utc, tz_str)} – {fmt_local(b.end_utc, tz_str)} ({tz_str})</p>"
            f'<p><a href="{manage_url}">Manage / cancel</a></p>'
        )
        send_email(b.email, subj, html)
        send_sms(b.phone, f"Rescheduled: {fmt_local(b.start_utc, tz_str)} ({tz_str}). Manage: {manage_url}")

    flash("Rescheduled.", "success")
    return redirect(url_for("manage", token=token))


@app.post("/cancel/<token>")
def cancel(token):
    with SessionLocal() as session:
        b = session.query(Booking).filter_by(manage_token=token).first()
        if not b:
            flash("Manage link not found.", "error")
            return redirect(url_for("home"))
        b.status = "canceled"
        session.commit()
    flash("Canceled.", "success")
    return redirect(url_for("home"))


@app.get("/admin/bookings")
def admin_bookings():
    with SessionLocal() as session:
        items = session.query(Booking).order_by(Booking.start_utc.desc()).all()
    return render_template("manage.html", bookings=items)  # or a dedicated admin template


# health & error pages ----------------------------------------------------------

@app.get("/health")
def health():
    return {"ok": True}, 200


@app.errorhandler(Exception)
def handle_error(e):
    # Log to Render logs
    try:
        import traceback
        traceback.print_exc()
    except Exception:
        pass
    # Avoid exposing internals to users
    return render_template("error.html", message=str(e)), 500


# ------------------------------------------------------------------------------
# Main (local dev only; Render will use gunicorn app:app)
# ------------------------------------------------------------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)

