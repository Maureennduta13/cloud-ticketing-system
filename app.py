import os
import re
import imaplib
import email as email_lib
from email import policy as email_policy
from email.mime.text import MIMEText
from email.utils import parseaddr

import smtplib

from flask import Flask, render_template, request, redirect, url_for, flash
from flask_sqlalchemy import SQLAlchemy
from dotenv import load_dotenv

load_dotenv()  # reads variables from a local .env file, if present

app = Flask(__name__)
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///tickets.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
# A secret key is required for flash messages (the "3 new tickets found"
# style notifications) to work securely.
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-change-this")

db = SQLAlchemy(app)

# Email credentials - never hardcode these directly in the file.
# They're loaded from environment variables set in a local .env file
# (see .env.example for the format).
EMAIL_ADDRESS = os.environ.get("EMAIL_ADDRESS")
EMAIL_PASSWORD = os.environ.get("EMAIL_PASSWORD")
SMTP_SERVER = os.environ.get("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
IMAP_SERVER = os.environ.get("IMAP_SERVER", "imap.gmail.com")


class Ticket(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(100), nullable=False)
    issue = db.Column(db.String(200), nullable=False)
    priority = db.Column(db.String(20), nullable=False)
    description = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(20), nullable=False, default="Open")

    # Stores the Message-ID header of the customer's most recent email.
    # Used so our replies thread correctly in their inbox instead of
    # showing up as a disconnected new email.
    email_message_id = db.Column(db.String(255), nullable=True)

    replies = db.relationship(
        "Reply", backref="ticket", order_by="Reply.created_at",
        cascade="all, delete-orphan"
    )


class Reply(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    ticket_id = db.Column(db.Integer, db.ForeignKey("ticket.id"), nullable=False)
    message = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, server_default=db.func.now())

    # "support" = written by IT support in the app.
    # "customer" = pulled in automatically from an incoming email.
    sender = db.Column(db.String(20), nullable=False, default="support")


with app.app_context():
    db.create_all()


def send_email(to_address, subject, body, in_reply_to=None):
    """Sends a plain-text email via SMTP. If in_reply_to is given (the
    Message-ID of the email being replied to), the email is threaded
    correctly in the recipient's inbox instead of appearing as a
    disconnected new message."""

    if not EMAIL_ADDRESS or not EMAIL_PASSWORD:
        raise RuntimeError(
            "Email credentials are not configured. Check your .env file."
        )

    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = EMAIL_ADDRESS
    msg["To"] = to_address

    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to

    with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
        server.starttls()
        server.login(EMAIL_ADDRESS, EMAIL_PASSWORD)
        server.send_message(msg)


def get_email_body(msg):
    """Extracts the plain-text body from an email message.

    Uses the modern email API (msg.get_body / part.get_content), which
    automatically handles character encoding and MIME transfer encoding
    correctly - the older API left encoded subjects/bodies as raw,
    unreadable text in some cases."""

    body_part = msg.get_body(preferencelist=("plain", "html"))

    if body_part is None:
        return "(No content found)"

    try:
        return body_part.get_content()
    except Exception:
        return "(Could not decode message content)"


# Patterns commonly used in automated/system sender addresses.
# Not foolproof, but catches the vast majority of noreply-style senders.
AUTOMATED_SENDER_PATTERNS = [
    "no-reply", "noreply", "donotreply", "do-not-reply",
    "mailer-daemon", "postmaster",
]


def strip_quoted_reply(body):
    """Removes quoted previous messages from an email reply, keeping
    only the new text the person actually typed. Handles Gmail's
    standard quoting pattern: "On <date>, <sender> wrote: > ..." -
    everything from that point onward is always the old thread, so
    it's safe to cut there."""
    match = re.search(r"\bOn\s.{5,150}?wrote:\s*", body, re.DOTALL)
    if match:
        body = body[:match.start()]
    return body.strip()


def is_automated_sender(sender_email):
    """Returns True if the sender's address looks like an automated
    or system-generated account rather than a real person."""
    lowered = sender_email.lower()
    return any(pattern in lowered for pattern in AUTOMATED_SENDER_PATTERNS)


def check_inbox():
    """Connects to the Gmail inbox via IMAP, finds unread emails, and
    either creates a new ticket or attaches a reply to an existing one
    (matched by ticket number in the subject line, e.g. "(Ticket #5)").
    Automated/no-reply senders are skipped entirely (but still marked
    as read, so they aren't checked again next time).

    Returns a tuple: (new_tickets_count, new_replies_count)
    """

    if not EMAIL_ADDRESS or not EMAIL_PASSWORD:
        raise RuntimeError(
            "Email credentials are not configured. Check your .env file."
        )

    new_tickets = 0
    new_replies = 0
    skipped = 0

    imap = imaplib.IMAP4_SSL(IMAP_SERVER)
    imap.login(EMAIL_ADDRESS, EMAIL_PASSWORD)
    imap.select("INBOX")

    # Search for unread emails only, so we never process the same email twice.
    status, message_ids = imap.search(None, "UNSEEN")

    if status == "OK":
        for msg_id in message_ids[0].split():
            status, msg_data = imap.fetch(msg_id, "(RFC822)")
            if status != "OK":
                continue

            raw_email = msg_data[0][1]
            msg = email_lib.message_from_bytes(raw_email, policy=email_policy.default)

            subject = str(msg.get("Subject", "(No subject)"))
            message_id_header = str(msg.get("Message-ID", ""))
            sender_name, sender_email = parseaddr(str(msg.get("From", "")))
            body = strip_quoted_reply(get_email_body(msg))

            # Skip automated/system emails entirely. The fetch above with
            # "(RFC822)" already marks this email as read on Gmail's side,
            # so it won't be picked up again on the next check.
            if is_automated_sender(sender_email):
                skipped += 1
                continue

            # Check if this is a reply to an existing ticket, based on
            # our own subject format: "... (Ticket #5)"
            ticket_match = re.search(r"\(Ticket #(\d+)\)", subject)

            if ticket_match:
                ticket_id = int(ticket_match.group(1))
                ticket = db.session.get(Ticket, ticket_id)

                if ticket:
                    reply = Reply(
                        ticket_id=ticket.id,
                        message=body,
                        sender="customer",
                    )
                    db.session.add(reply)

                    # Update so our next reply threads off this latest message
                    ticket.email_message_id = message_id_header

                    new_replies += 1
                    db.session.commit()
                    continue

            # Otherwise, treat it as a brand new ticket
            ticket = Ticket(
                name=sender_name or sender_email,
                email=sender_email,
                issue=subject,
                priority="Low",
                description=body,
                status="Open",
                email_message_id=message_id_header,
            )
            db.session.add(ticket)
            new_tickets += 1
            db.session.commit()

    imap.logout()
    return new_tickets, new_replies, skipped


@app.route("/")
def home():
    tickets = Ticket.query.order_by(Ticket.id.desc()).all()
    return render_template("index.html", tickets=tickets)


@app.route("/create-ticket", methods=["POST"])
def create_ticket():
    ticket = Ticket(
        name=request.form["name"],
        email=request.form["email"],
        issue=request.form["issue"],
        priority=request.form["priority"],
        description=request.form["description"],
        status="Open",
    )

    db.session.add(ticket)
    db.session.commit()

    return redirect(url_for("home") + "#tickets")


@app.route("/tickets")
def view_tickets():
    return redirect(url_for("home") + "#tickets")


@app.route("/update-status/<int:ticket_id>", methods=["POST"])
def update_status(ticket_id):
    ticket = Ticket.query.get_or_404(ticket_id)
    new_status = request.form["status"]

    # Only allow known status values, to avoid junk data ending up in the DB
    if new_status in ("Open", "In Progress", "Resolved"):
        ticket.status = new_status
        db.session.commit()

    return redirect(url_for("home") + "#tickets")


@app.route("/reply/<int:ticket_id>", methods=["POST"])
def reply_to_ticket(ticket_id):
    ticket = Ticket.query.get_or_404(ticket_id)
    message = request.form["message"].strip()

    if not message:
        return redirect(url_for("home") + "#tickets")

    # Save the reply first, so it's recorded even if the email send fails
    reply = Reply(ticket_id=ticket.id, message=message, sender="support")
    db.session.add(reply)
    db.session.commit()

    try:
        send_email(
            to_address=ticket.email,
            subject=f"Re: {ticket.issue} (Ticket #{ticket.id})",
            body=message,
            in_reply_to=ticket.email_message_id,
        )
    except Exception as e:
        print(f"Failed to send email for ticket {ticket.id}: {e}")

    return redirect(url_for("home") + "#tickets")


@app.route("/check-inbox", methods=["POST"])
def check_inbox_route():
    try:
        new_tickets, new_replies, skipped = check_inbox()
        message = f"Inbox checked: {new_tickets} new ticket(s), {new_replies} new reply(ies) found."
        if skipped:
            message += f" ({skipped} automated email(s) skipped.)"
        flash(message)
    except Exception as e:
        flash(f"Could not check inbox: {e}")

    return redirect(url_for("home") + "#tickets")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)