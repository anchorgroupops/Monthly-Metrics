"""
Flask + HTMX + Tailwind admin dashboard.

Single-admin auth (ADMIN_PASSWORD env var). Mobile-first. No JS build step:
Tailwind via CDN, HTMX via CDN. Reuses BRAND from config/settings.py and the
existing Jinja templates.

Routes
------
GET  /                  Login form (or redirect to /home if logged in).
POST /login             Auth via ADMIN_PASSWORD.
GET  /home              Leaderboard + team averages + 30/60/90 sparklines.
GET  /upload            CSV/JSON upload form.
POST /upload            Ingest uploaded file → SQLite.
GET  /review/<period>   Draft approval queue for a period.
GET  /draft/<id>        Render the queued HTML for preview.
POST /draft/<id>/approve   HTMX swap: mark approved.
POST /draft/<id>/reject    HTMX swap: mark rejected.
POST /review/<period>/approve_all   Approve all pending in period.
POST /send                   Trigger send for approved drafts.
GET  /logout
"""

from __future__ import annotations

import json
import os
import secrets
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from functools import wraps
from pathlib import Path
from tempfile import NamedTemporaryFile

from flask import (
    Flask,
    abort,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from jinja2 import ChoiceLoader, FileSystemLoader

from config.settings import (
    BRAND,
    EMAIL_FROM_ADDRESS,
    EMAIL_FROM_NAME,
    EMAIL_SUBJECT_TEMPLATE,
    SMTP_HOST,
    SMTP_PASSWORD,
    SMTP_PORT,
    SMTP_USER,
    TEMPLATES_DIR,
)
from src import storage
from src.csv_ingest import parse_file
from src.metrics import (
    load_thresholds,
    metric_keys,
    rolling_trend,
    score_all_agents,
)


def create_app() -> Flask:
    app = Flask(
        __name__,
        template_folder=str(TEMPLATES_DIR),
        static_folder=None,
    )
    app.secret_key = os.environ.get("FLASK_SECRET_KEY") or secrets.token_hex(32)
    app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024  # 5 MB upload cap

    # Make BRAND available in every template.
    @app.context_processor
    def inject_brand():
        return {"brand": BRAND}

    _register_routes(app)
    return app


# ── Auth ──────────────────────────────────────────────────────────────────────

def login_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not session.get("authed"):
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapper


def _check_password(provided: str) -> bool:
    expected = os.environ.get("ADMIN_PASSWORD", "anchor")
    return secrets.compare_digest(provided or "", expected)


# ── Routes ────────────────────────────────────────────────────────────────────

def _register_routes(app: Flask) -> None:

    @app.route("/")
    def root():
        if session.get("authed"):
            return redirect(url_for("home"))
        return redirect(url_for("login"))

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            if _check_password(request.form.get("password", "")):
                session["authed"] = True
                return redirect(url_for("home"))
            flash("Incorrect password.", "error")
        return render_template("admin/login.html")

    @app.route("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.route("/home")
    @login_required
    def home():
        periods = storage.list_periods()
        if not periods:
            return render_template("admin/empty.html")

        latest = periods[0]
        agents_data = storage.load_period(latest)
        scored = score_all_agents(agents_data) if agents_data else []
        scored.sort(
            key=lambda a: (a["operational_readiness"] or 0),
            reverse=True,
        )

        thresholds = load_thresholds()
        keys = metric_keys(thresholds)

        # Per-agent 30/60/90 trend on the hero metric
        hero_key = next(
            (k for k, m in thresholds["metrics"].items() if m.get("gauge_size") == "hero"),
            keys[0] if keys else None,
        )
        trends = {
            a["agent_id"]: rolling_trend(a["agent_id"], hero_key, 3) if hero_key else None
            for a in scored
        }

        # Pending draft count for "Review & Send" CTA
        pending = len(storage.list_drafts(period=latest, status="pending"))
        approved = len(storage.list_drafts(period=latest, status="approved"))

        return render_template(
            "admin/home.html",
            agents=scored,
            period=latest,
            period_label=storage.period_label(latest),
            periods=periods,
            thresholds=thresholds,
            metric_keys=keys,
            hero_key=hero_key,
            trends=trends,
            pending=pending,
            approved=approved,
        )

    @app.route("/upload", methods=["GET", "POST"])
    @login_required
    def upload():
        if request.method == "POST":
            file = request.files.get("file")
            if not file or not file.filename:
                flash("No file selected.", "error")
                return redirect(url_for("upload"))

            suffix = Path(file.filename).suffix.lower()
            if suffix not in (".csv", ".json"):
                flash("File must be .csv or .json.", "error")
                return redirect(url_for("upload"))

            with NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
                file.save(tmp.name)
                tmp_path = tmp.name

            try:
                agents = parse_file(tmp_path)
                run_id = storage.save_period(
                    agents,
                    source=suffix.lstrip("."),
                    file_path=file.filename,
                )
                flash(
                    f"Ingested {len(agents)} agent(s) from {file.filename} "
                    f"(run #{run_id}).",
                    "success",
                )
                return redirect(url_for("home"))
            except (FileNotFoundError, ValueError) as e:
                flash(f"Upload failed: {e}", "error")
                return redirect(url_for("upload"))
            finally:
                Path(tmp_path).unlink(missing_ok=True)

        return render_template(
            "admin/upload.html",
            metric_keys=metric_keys(load_thresholds()),
        )

    @app.route("/review/<period>")
    @login_required
    def review(period: str):
        canonical = storage.normalize_period(period)
        drafts = storage.list_drafts(period=canonical)
        return render_template(
            "admin/review.html",
            period=canonical,
            period_label=storage.period_label(canonical),
            drafts=drafts,
        )

    @app.route("/draft/<int:draft_id>")
    @login_required
    def draft_preview(draft_id: int):
        d = storage.get_draft(draft_id)
        if not d:
            abort(404)
        # Inline iframe-friendly: no chrome wrapper.
        return d["html"]

    @app.route("/draft/<int:draft_id>/approve", methods=["POST"])
    @login_required
    def draft_approve(draft_id: int):
        d = storage.get_draft(draft_id)
        if not d:
            abort(404)
        storage.approve_draft(draft_id)
        d = storage.get_draft(draft_id)
        return render_template("admin/_draft_row.html", d=d)

    @app.route("/draft/<int:draft_id>/reject", methods=["POST"])
    @login_required
    def draft_reject(draft_id: int):
        d = storage.get_draft(draft_id)
        if not d:
            abort(404)
        storage.reject_draft(draft_id)
        d = storage.get_draft(draft_id)
        return render_template("admin/_draft_row.html", d=d)

    @app.route("/review/<period>/approve_all", methods=["POST"])
    @login_required
    def approve_all(period: str):
        canonical = storage.normalize_period(period)
        n = storage.approve_all(canonical)
        flash(f"Approved {n} draft(s).", "success")
        return redirect(url_for("review", period=canonical))

    @app.route("/send", methods=["POST"])
    @login_required
    def send():
        approved = storage.list_drafts(status="approved")
        if not approved:
            flash("No approved drafts to send.", "error")
            return redirect(url_for("home"))

        if not SMTP_USER or not SMTP_PASSWORD:
            flash(
                "SMTP credentials not configured. Set SMTP_USER and SMTP_PASSWORD.",
                "error",
            )
            return redirect(url_for("home"))

        sent = 0
        try:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
                server.ehlo()
                server.starttls()
                server.login(SMTP_USER, SMTP_PASSWORD)

                for d in approved:
                    full = storage.get_draft(d["id"])
                    msg = MIMEMultipart("alternative")
                    msg["Subject"] = EMAIL_SUBJECT_TEMPLATE.format(
                        month=storage.period_label(full["period"])
                    )
                    msg["From"] = f"{EMAIL_FROM_NAME} <{EMAIL_FROM_ADDRESS}>"
                    msg["To"] = full["email"]
                    msg.attach(MIMEText(full["html"], "html", "utf-8"))
                    server.sendmail(EMAIL_FROM_ADDRESS, full["email"], msg.as_string())
                    storage.mark_sent(full["id"])
                    sent += 1
        except smtplib.SMTPException as e:
            flash(f"SMTP error after sending {sent}: {e}", "error")
            return redirect(url_for("home"))

        flash(f"Sent {sent} email(s).", "success")
        return redirect(url_for("home"))

    @app.template_filter("metric_value")
    def metric_value_filter(metric):
        """Format a scored metric value for table display."""
        v = metric.get("value")
        if v is None:
            return "—"
        unit = metric.get("unit", "")
        if unit == "percent":
            return f"{v * 100:.1f}%"
        if unit == "seconds":
            if v < 60:
                return f"{int(round(v))}s"
            mins, secs = divmod(int(round(v)), 60)
            return f"{mins}m {secs:02d}s" if secs else f"{mins}m"
        if unit == "score":
            return f"{v:.1f}"
        if unit == "count":
            return str(int(round(v)))
        return f"{v:.2f}"

    @app.template_filter("status_color")
    def status_color_filter(status: str):
        return {
            "Preferred": "bg-emerald-500",
            "At Risk": "bg-amber-500",
            "Needs Improvement": "bg-rose-500",
            "No Data": "bg-slate-400",
        }.get(status, "bg-slate-400")
