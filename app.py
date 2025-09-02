import os, uuid, requests, phonenumbers
from datetime import datetime, timedelta, time
from zoneinfo import ZoneInfo
from typing import List, Tuple
from flask import Flask, render_template, request, redirect, url_for, flash, send_file, jsonify
from sqlalchemy import create_engine, Column, String, DateTime
from sqlalchemy.orm import declarative_base, sessionmaker
from email_validator import validate_email, EmailNotValidError
import settings

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET", "dev-secret")
engine = create_engine(os.getenv("BOOKING_DB","sqlite:///booking.db"), echo=False, future=True)
Base = declarative_base()
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)

class Booking(Base):
    __tablename__ = "bookings"
    id = Column(String, primary_key=True)
    name = Column(String, nullable=False)
    email = Column(String, nullable=False)
    phone = Column(String, nullable=False)
    start_utc = Column(DateTime, nullable=False)
    end_utc = Column(DateTime, nullable=False)
    status = Column(String, nullable=False)   # confirmed/canceled
    manage_token = Column(String, nullable=False, unique=True)
    created_at = Column(DateTime, nullable=False)

Base.metadata.create_all(engine)

WEEKDAYS = ["mon","tue","wed","thu","fri","sat","sun"]
def local_now(): return datetime.now(ZoneInfo(settings.TIMEZONE))
def daterange(start_date, end_date):
    for n in range(int((end_date-start_date).days)+1): yield start_date+timedelta(n)

def parse_range(r: str):
    """Parse 'HH:MM-HH:MM' and allow '24:00' as end-of-day."""
    a, b = r.split("-")
    h1, m1 = [int(x) for x in a.split(":")]
    h2, m2 = [int(x) for x in b.split(":")]
    from datetime import time
    start_t = time(hour=h1, minute=m1)
    # treat 24:00 as the very end of the day
    if h2 == 24 and m2 == 0:
        end_t = time(hour=23, minute=59)
    else:
        end_t = time(hour=h2, minute=m2)
    return start_t, end_t

def overlaps(a_start,a_end,b_start,b_end): return a_start<b_end and a_end>b_start

def generate_slots_for_date(d, tz: ZoneInfo, slot_minutes: int) -> List[tuple]:
    ranges = settings.BUSINESS_HOURS.get(WEEKDAYS[d.weekday()], [])
    slots = []
    step = timedelta(minutes=slot_minutes)

    for r in ranges:
        t1, t2 = parse_range(r)
        cur = datetime.combine(d, t1, tzinfo=tz)
        end = datetime.combine(d, t2, tzinfo=tz)

        # allow a closing slot like 23:00–24:00 to appear
        while cur + step <= end + timedelta(minutes=1):
            slots.append((cur, cur + step))
            cur += step

    return slots

def get_existing(session, start_utc, end_utc):
    return list(session.query(Booking).filter(Booking.status=="confirmed").filter(Booking.start_utc<end_utc, Booking.end_utc>start_utc).all())

def available_slots(start_date_local, days_ahead, slot_minutes, lead_minutes, exclude_id=None):
    tz = ZoneInfo(settings.TIMEZONE); now_local = local_now(); end_date_local = start_date_local+timedelta(days=days_ahead-1)
    slots_by_date = {}
    with SessionLocal() as s:
        ws = datetime.combine(start_date_local,time(0,0),tzinfo=tz).astimezone(ZoneInfo("UTC"))
        we = datetime.combine(end_date_local,time(23,59),tzinfo=tz).astimezone(ZoneInfo("UTC"))
        existing = get_existing(s, ws, we)
        if exclude_id: existing = [b for b in existing if b.id != exclude_id]
        for d in daterange(start_date_local, end_date_local):
            ds = d.isoformat()
            if ds in settings.BLOCK_DATES: continue
            day_slots=[]
            for start_local,end_local in generate_slots_for_date(d,tz,slot_minutes):
                if end_local <= now_local+timedelta(minutes=lead_minutes): continue
                start_utc = start_local.astimezone(ZoneInfo("UTC")); end_utc = end_local.astimezone(ZoneInfo("UTC"))
                if any(overlaps(start_utc,end_utc,b.start_utc,b.end_utc) for b in existing): continue
                day_slots.append((start_local,end_local))
            if day_slots: slots_by_date[ds]=day_slots
    return slots_by_date

def send_email(to_email, subject, html_body, text_body=None):
    key=settings.SENDGRID_API_KEY
    if not key: print("[email] missing key; skipping", to_email); return
    from_email = settings.EMAIL_FROM if "<" not in settings.EMAIL_FROM else settings.EMAIL_FROM.split("<")[-1].strip(">").strip()
    data = {"personalizations":[{"to":[{"email":to_email}]}],
            "from":{"email":from_email},
            "subject":subject,
            "content":[{"type":"text/plain","value":text_body or ""},{"type":"text/html","value":html_body}]}
    r=requests.post("https://api.sendgrid.com/v3/mail/send", headers={"Authorization":f"Bearer {key}","Content-Type":"application/json"}, json=data, timeout=30)
    print("[email] status", r.status_code, r.text if r.status_code>=400 else "")

def send_sms(to_phone, body):
    sid=settings.TWILIO_ACCOUNT_SID; token=settings.TWILIO_AUTH_TOKEN; from_num=settings.TWILIO_FROM_NUMBER
    if not (sid and token and from_num): print("[sms] missing creds; skipping", to_phone); return
    r=requests.post(f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json",
                    data={"To":to_phone,"From":from_num,"Body":body}, auth=(sid,token), timeout=30)
    print("[sms] status", r.status_code, r.text if r.status_code>=400 else "")

def fmt_local(dt_utc): return dt_utc.astimezone(ZoneInfo(settings.TIMEZONE))


@app.route("/")
def home():
    start_q=request.args.get("start")
    if start_q:
        try: start_date_local=datetime.strptime(start_q,"%Y-%m-%d").date()
        except ValueError: start_date_local=local_now().date()
    else: start_date_local=local_now().date()
    slots=available_slots(start_date_local, settings.DAYS_AHEAD, settings.SLOT_MINUTES, settings.LEAD_MINUTES)
    return render_template("index.html", slots=slots, timezone=settings.TIMEZONE, start_date=start_date_local.isoformat(), days_ahead=settings.DAYS_AHEAD, slot_minutes=settings.SLOT_MINUTES, os=os)

@app.post("/book")
def book_post():
    name=request.form.get("name","").strip(); email=request.form.get("email","").strip(); phone=request.form.get("phone","").strip()
    slot_start_iso=request.form.get("slot_start_iso"); slot_end_iso=request.form.get("slot_end_iso")
    if not (name and email and phone and slot_start_iso and slot_end_iso):
        flash("Name, email, and phone are required.","error"); return redirect(url_for("home"))
    try: validate_email(email, check_deliverability=False)
    except EmailNotValidError as e: flash(f"Email looks invalid: {str(e)}","error"); return redirect(url_for("home"))
    try:
        pn=phonenumbers.parse(phone,"US")
        if not phonenumbers.is_possible_number(pn) or not phonenumbers.is_valid_number(pn): raise ValueError()
        phone_e164=phonenumbers.format_number(pn, phonenumbers.PhoneNumberFormat.E164)
    except Exception: flash("Please enter a valid US phone number (e.g., 3125550123).","error"); return redirect(url_for("home"))
    tz=ZoneInfo(settings.TIMEZONE)
    try: start_local=datetime.fromisoformat(slot_start_iso).replace(tzinfo=tz); end_local=datetime.fromisoformat(slot_end_iso).replace(tzinfo=tz)
    except Exception: flash("Invalid slot time.","error"); return redirect(url_for("home"))
    start_utc=start_local.astimezone(ZoneInfo("UTC")); end_utc=end_local.astimezone(ZoneInfo("UTC"))
    with SessionLocal() as s:
        conflict = s.query(Booking).filter(Booking.status=="confirmed").filter(Booking.start_utc<end_utc, Booking.end_utc>start_utc).first()
        if conflict: flash("Sorry, that slot was just taken. Please choose another.","error"); return redirect(url_for("home"))
        token=uuid.uuid4().hex
        b=Booking(id=str(uuid.uuid4()), name=name, email=email, phone=phone_e164, start_utc=start_utc, end_utc=end_utc, status="confirmed", manage_token=token, created_at=datetime.utcnow())
        s.add(b); s.commit()
        start_local=fmt_local(b.start_utc); end_local=fmt_local(b.end_utc)
        manage_url=f"{settings.PUBLIC_BASE_URL}/manage/{b.manage_token}"; ics_url=f"{settings.PUBLIC_BASE_URL}/ics/{b.id}.ics"
        subj="Your Rent a Ear session is booked"
        text=f"Hi {b.name}, your session is confirmed for {start_local.strftime('%a %b %d, %Y %I:%M %p')} - {end_local.strftime('%I:%M %p')} ({settings.TIMEZONE}). Manage: {manage_url}\nAdd to calendar: {ics_url}"
        html=f"<p>Hi {b.name},</p><p>Your session is <b>confirmed</b> for <b>{start_local.strftime('%A, %B %d, %Y %I:%M %p')}</b> – <b>{end_local.strftime('%I:%M %p')}</b> ({settings.TIMEZONE}).</p><p><a href='{ics_url}'>Add to calendar (.ics)</a> • <a href='{manage_url}'>Reschedule / Cancel</a></p><p>Thanks,<br>Rent a Ear</p>"
        send_email(b.email, subj, html, text); send_sms(b.phone, text)
        return redirect(url_for("confirm", booking_id=b.id))

@app.get("/confirm/<booking_id>")
def confirm(booking_id):
    with SessionLocal() as s:
        b=s.get(Booking, booking_id)
        if not b: flash("Booking not found.","error"); return redirect(url_for("home"))
        start_local=fmt_local(b.start_utc); end_local=fmt_local(b.end_utc); manage_url=f"{settings.PUBLIC_BASE_URL}/manage/{b.manage_token}"
        return render_template("confirm.html", booking=b, start_local=start_local, end_local=end_local, timezone=settings.TIMEZONE, os=os, manage_url=manage_url)

@app.get("/manage/<token>")
def manage(token):
    with SessionLocal() as s:
        b=s.query(Booking).filter_by(manage_token=token).first()
        if not b: flash("Link invalid.","error"); return redirect(url_for("home"))
        start_local=fmt_local(b.start_utc); end_local=fmt_local(b.end_utc)
        return render_template("manage.html", booking=b, start_local=start_local, end_local=end_local, timezone=settings.TIMEZONE, os=os)

@app.post("/cancel/<token>")
def cancel(token):
    with SessionLocal() as s:
        b=s.query(Booking).filter_by(manage_token=token).first()
        if not b: flash("Link invalid.","error"); return redirect(url_for("home"))
        if b.status=="canceled": flash("This booking is already canceled.","error"); return redirect(url_for("manage", token=token))
        b.status="canceled"; s.commit()
        start_local=fmt_local(b.start_utc); subj="Your Rent a Ear session has been canceled"
        text=f"Hi {b.name}, your session on {start_local.strftime('%a %b %d, %Y %I:%M %p')} is now canceled."
        html=f"<p>Hi {b.name},</p><p>Your session on <b>{start_local.strftime('%A, %B %d, %Y %I:%M %p')}</b> has been <b>canceled</b>.</p>"
        send_email(b.email, subj, html, text); send_sms(b.phone, text)
        flash("Booking canceled.","message"); return redirect(url_for("manage", token=token))

@app.get("/reschedule/<token>")
def reschedule(token):
    with SessionLocal() as s:
        b=s.query(Booking).filter_by(manage_token=token).first()
        if not b: flash("Link invalid.","error"); return redirect(url_for("home"))
        if b.status!="confirmed": flash("Only confirmed bookings can be rescheduled.","error"); return redirect(url_for("manage", token=token))
        start_q=request.args.get("start")
        if start_q:
            try: start_date_local=datetime.strptime(start_q,"%Y-%m-%d").date()
            except ValueError: start_date_local=local_now().date()
        else: start_date_local=local_now().date()
        slots=available_slots(start_date_local, settings.DAYS_AHEAD, settings.SLOT_MINUTES, settings.LEAD_MINUTES, exclude_id=b.id)
        return render_template("reschedule.html", booking=b, slots=slots, timezone=settings.TIMEZONE, start_date=start_date_local.isoformat(), days_ahead=settings.DAYS_AHEAD, slot_minutes=settings.SLOT_MINUTES, os=os)

@app.post("/reschedule/<token>")
def reschedule_post(token):
    with SessionLocal() as s:
        b=s.query(Booking).filter_by(manage_token=token).first()
        if not b: flash("Link invalid.","error"); return redirect(url_for("home"))
        if b.status!="confirmed": flash("Only confirmed bookings can be rescheduled.","error"); return redirect(url_for("manage", token=token))
        slot_start_iso=request.form.get("slot_start_iso"); slot_end_iso=request.form.get("slot_end_iso")
        tz=ZoneInfo(settings.TIMEZONE)
        try: start_local=datetime.fromisoformat(slot_start_iso).replace(tzinfo=tz); end_local=datetime.fromisoformat(slot_end_iso).replace(tzinfo=tz)
        except Exception: flash("Invalid slot time.","error"); return redirect(url_for("reschedule", token=token))
        start_utc=start_local.astimezone(ZoneInfo("UTC")); end_utc=end_local.astimezone(ZoneInfo("UTC"))
        conflict=s.query(Booking).filter(Booking.id!=b.id).filter(Booking.status=="confirmed").filter(Booking.start_utc<end_utc, Booking.end_utc>start_utc).first()
        if conflict: flash("Sorry, that slot was just taken. Choose another.","error"); return redirect(url_for("reschedule", token=token))
        b.start_utc=start_utc; b.end_utc=end_utc; s.commit()
        start_local=fmt_local(b.start_utc); end_local=fmt_local(b.end_utc)
        manage_url=f"{settings.PUBLIC_BASE_URL}/manage/{b.manage_token}"; ics_url=f"{settings.PUBLIC_BASE_URL}/ics/{b.id}.ics"
        subj="Your Rent a Ear session was rescheduled"
        text=f"Hi {b.name}, your session is now {start_local.strftime('%a %b %d, %Y %I:%M %p')} - {end_local.strftime('%I:%M %p')} ({settings.TIMEZONE}). Manage: {manage_url}\nAdd to calendar: {ics_url}"
        html=f"<p>Hi {b.name},</p><p>Your session has been <b>rescheduled</b> to <b>{start_local.strftime('%A, %B %d, %Y %I:%M %p')}</b> – <b>{end_local.strftime('%I:%M %p')}</b> ({settings.TIMEZONE}).</p><p><a href='{ics_url}'>Add to calendar (.ics)</a> • <a href='{manage_url}'>Manage</a></p>"
        send_email(b.email, subj, html, text); send_sms(b.phone, text)
        flash("Rescheduled successfully.","message"); return redirect(url_for("manage", token=token))

@app.get("/ics/<booking_id>.ics")
def ics(booking_id):
    with SessionLocal() as s:
        b=s.get(Booking, booking_id)
        if not b: return ("Not found",404)
        uid=f"{b.id}@rent-a-ear"; dtstamp=datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
        dtstart=b.start_utc.strftime("%Y%m%dT%H%M%SZ"); dtend=b.end_utc.strftime("%Y%m%dT%H%M%SZ")
        ics=f"BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:-//Rent a Ear//Booking//EN\nCALSCALE:GREGORIAN\nBEGIN:VEVENT\nUID:{uid}\nDTSTAMP:{dtstamp}\nDTSTART:{dtstart}\nDTEND:{dtend}\nSUMMARY:Rent a Ear Session\nDESCRIPTION:Session with {b.name}\nLOCATION:Phone / Online\nEND:VEVENT\nEND:VCALENDAR\n"
        from io import BytesIO
        return send_file(BytesIO(ics.encode("utf-8")), mimetype="text/calendar", as_attachment=True, download_name=f"booking-{b.id}.ics")

@app.get("/admin/bookings")
def admin_bookings():
    with SessionLocal() as s:
        items=s.query(Booking).order_by(Booking.start_utc.asc()).all()
        return jsonify([{"id":b.id,"name":b.name,"email":b.email,"phone":b.phone,"start_utc":b.start_utc.isoformat(),"end_utc":b.end_utc.isoformat(),"status":b.status,"manage_token":b.manage_token,"created_at":b.created_at.isoformat()} for b in items])

if __name__=="__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT","5000")), debug=True)
