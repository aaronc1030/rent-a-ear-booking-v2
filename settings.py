import os
TIMEZONE = os.getenv("BOOKING_TIMEZONE", "America/Chicago")
SLOT_MINUTES = 60
DAYS_AHEAD = int(os.getenv("BOOKING_DAYS_AHEAD", "21"))
LEAD_MINUTES = int(os.getenv("BOOKING_LEAD_MINUTES", "120"))
BUSINESS_HOURS = {
    "mon": ["00:00-05:00", "21:00-23:59"],
    "tue": ["00:00-05:00", "21:00-23:59"],
    "wed": ["00:00-05:00", "21:00-23:59"],
    "thu": ["00:00-05:00", "21:00-23:59"],
    "fri": ["00:00-05:00", "21:00-23:59"],
    "sat": ["00:00-05:00", "21:00-23:59"],
    "sun": ["00:00-05:00", "21:00-23:59"],
}
BLOCK_DATES = set([])
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "http://localhost:5000")
SENDGRID_API_KEY = os.getenv("SENDGRID_API_KEY")
EMAIL_FROM = os.getenv("EMAIL_FROM", "Bookings <no-reply@rentaear.local>")
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_FROM_NUMBER = os.getenv("TWILIO_FROM_NUMBER")
