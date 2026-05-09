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

import logging
import os
import secrets
import smtplib
import threading
from datetime import datetime, timedelta
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
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_wtf.csrf import CSRFProtect
from werkzeug.middleware.proxy_fix import ProxyFix

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

log = logging.getLogger(__name__)

csrf = CSRFProtect()


def create_app() -> Flask:
    app = Flask(
        __name__,
        template_folder=str(TEMPLATES_DIR),
        static_folder=None,
    )
    app.secret_key = os.environ.get("FLASK_SECRET_KEY") or secrets.token_hex(32)
    app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024  # 5 MB upload cap
    app.config["WTF_CSRF_TIME_LIMIT"] = None  # CSRF tokens valid as long as session

    is_prod = os.environ.get("DEPLOYMENT_MODE", "development").lower() == "production"
    if is_prod:
        # Behind cloudflared (or any reverse proxy) — read real client IP/scheme.
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
        app.config.update(
            SESSION_COOKIE_SECURE=True,
            SESSION_COOKIE_HTTPONLY=True,
            SESSION_COOKIE_SAMESITE="Lax",
            PREFERRED_URL_SCHEME="https",
        )
        log.info("Dashboard starting in PRODUCTION mode (secure cookies, ProxyFix on).")
    else:
        log.info("Dashboard starting in DEVELOPMENT mode.")

    csrf.init_app(app)

    # Brute-force protection on /login (5 attempts / 15 min / IP).
    limiter = Limiter(
        get_remote_address,
        app=app,
        default_limits=[],
        storage_uri="memory://",
    )
    app.extensions["limiter"] = limiter

    # Make BRAND available in every template.
    @app.context_processor
    def inject_brand():
        return {"brand": BRAND}

    # Clean up runs left in 'running' from a prior worker that died — otherwise
    # the manual-pull button would stay disabled forever after a gunicorn crash.
    _reap_stale_runs()

    _register_routes(app, limiter)

    # Per-agent self-service portal at /metrics. CSRF-exempt: it uses its own
    # HTTP-only cookie (not Flask session), and the only POST surface is
    # /metrics/login which at worst triggers an email to the user themselves.
    from src.agent_portal import bp as portal_bp

    csrf.exempt(portal_bp)
    app.register_blueprint(portal_bp)

    return app


# ── Manual-pull background pipeline ───────────────────────────────────────────

# Stale threshold: a run still in 'running' after this is treated as crashed.
_STALE_RUN_MINUTES = 30


def _reap_stale_runs() -> None:
    active = storage.get_active_run()
    if not active:
        return
    try:
        started = datetime.fromisoformat(active["created_at"])
    except (KeyError, TypeError, ValueError):
        return
    if datetime.utcnow() - started > timedelta(minutes=_STALE_RUN_MINUTES):
        log.warning("Reaping stale run #%d (started %s)", active["id"], active["created_at"])
        storage.finish_run(active["id"], "error", "stale — reaped at app startup")


def _pull_pipeline_worker(run_id: int) -> None:
    """
    Run the full monthly pipeline in a background thread:
      1. Fetch metrics from FUB → save_period (uses pre-allocated run_id)
      2. Refresh KPI thresholds (non-fatal — warn-only)
      3. Build draft emails for the period that just landed

    Errors at step 1 or 3 mark the run 'error' and trigger an admin alert.
    Step 2 is best-effort.
    """
    try:
        from src.fub_client import fetch_all_agents

        agents = fetch_all_agents()
        if not agents:
            storage.finish_run(run_id, "ok", "FUB returned 0 agents")
            return

        storage.save_period(agents, source="fub", run_id=run_id)
        period = storage.normalize_period(agents[0]["period"])

        # Step 2: research is non-fatal — keep yesterday's thresholds if it fails.
        try:
            from src.threshold_researcher import run_research

            run_research()
        except Exception as exc:
            log.warning("Threshold research failed (continuing): %s", exc)

        # Step 3: queue drafts for the period we just pulled.
        from src.email_builder import build_email
        from src.metrics import score_all_agents

        scored = score_all_agents(storage.load_period(period))
        for agent in scored:
            html = build_email(agent)
            storage.queue_draft(agent["agent_id"], agent["period"], html)

        storage.finish_run(
            run_id,
            "ok",
            f"pulled {len(agents)}, queued {len(scored)} drafts for {period}",
        )
        log.info("Manual pull complete: run #%d", run_id)
    except Exception as exc:
        log.exception("Manual-pull pipeline failed")
        try:
            storage.finish_run(run_id, "error", str(exc)[:500])
        except Exception:
            log.exception("Failed to mark run as errored")
        try:
            from src.notifier import notify_admin_failure

            notify_admin_failure(
                "Anchor Group: manual pull failed",
                f"Run #{run_id} failed during the manual-pull pipeline.\n\n"
                f"Error: {exc}\n\n"
                f"Check journalctl -u anchor-dashboard for the traceback.",
            )
        except Exception:
            log.exception("Failed to send admin failure notification")


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


def _register_routes(app: Flask, limiter: Limiter) -> None:

    @app.route("/")
    def root():
        if session.get("authed"):
            return redirect(url_for("home"))
        return redirect(url_for("login"))

    @app.route("/healthz")
    @csrf.exempt
    def healthz():
        import shutil
        from datetime import datetime as _dt

        from flask import jsonify

        ok = True
        db_writable = False
        last_heartbeat_age_hours: float | None = None
        draft_queue_size = 0
        disk_used_pct: float = 0.0

        try:
            with storage.connect() as conn:
                conn.execute("SELECT 1").fetchone()
                db_writable = True

                row = conn.execute(
                    "SELECT created_at FROM runs WHERE source = 'fub' ORDER BY id DESC LIMIT 1"
                ).fetchone()
                if row and row["created_at"]:
                    try:
                        last = _dt.fromisoformat(row["created_at"])
                        delta = _dt.utcnow() - last
                        last_heartbeat_age_hours = round(delta.total_seconds() / 3600, 2)
                    except (TypeError, ValueError):
                        pass

                draft_queue_size = conn.execute(
                    "SELECT COUNT(*) FROM drafts WHERE status='pending'"
                ).fetchone()[0]
        except Exception:
            ok = False

        try:
            usage = shutil.disk_usage(str(storage.DB_PATH.parent))
            disk_used_pct = round((usage.used / usage.total) * 100, 1)
        except Exception:  # noqa: S110 — disk_used_pct is best-effort; missing it doesn't degrade health
            pass
        # Disk-full alerting is scripts/disk_check.sh's responsibility — healthz
        # just reports the percent so monitors can read it.

        payload = {
            "ok": ok and db_writable,
            "db_writable": db_writable,
            "last_heartbeat_age_hours": last_heartbeat_age_hours,
            "draft_queue_size": draft_queue_size,
            "disk_used_pct": disk_used_pct,
        }
        return jsonify(payload), (200 if payload["ok"] else 503)

    @app.route("/login", methods=["GET", "POST"])
    @limiter.limit("5 per 15 minutes", methods=["POST"])
    def login():
        if request.method == "POST":
            if _check_password(request.form.get("password", "")):
                session.clear()
                session["authed"] = True
                session.permanent = True
                return redirect(url_for("home"))
            flash("Incorrect password.", "error")
        return render_template("admin/login.html")

    @app.errorhandler(429)
    def too_many_requests(e):
        return render_template(
            "admin/login.html",
            rate_limited=True,
            retry_after=str(getattr(e, "description", "")),
        ), 429

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
            key=lambda a: a["operational_readiness"] or 0,
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
                    f"Ingested {len(agents)} agent(s) from {file.filename} (run #{run_id}).",
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

    @app.route("/pull-now", methods=["POST"])
    @login_required
    def pull_now():
        from config.settings import FUB_API_KEY

        # An empty AGENTS list is intentional — the roster auto-discovers from
        # FUB /v1/users inside fetch_all_agents(), so we don't pre-empt the run
        # here. If discovery yields nothing the worker will mark the run "ok"
        # with a "FUB returned 0 agents" note.
        if not FUB_API_KEY:
            flash("FUB_API_KEY is not set in the deployment environment.", "error")
            return redirect(url_for("home"))
        if storage.get_active_run():
            flash("A pull is already in progress.", "error")
            return redirect(url_for("home"))

        run_id = storage.start_run(source="fub")
        threading.Thread(
            target=_pull_pipeline_worker,
            args=(run_id,),
            daemon=True,
            name=f"pull-{run_id}",
        ).start()
        flash("FUB pull started — drafts will appear when it finishes.", "success")
        return redirect(url_for("home"))

    @app.route("/pull-status")
    @login_required
    def pull_status():
        return render_template(
            "admin/_pull_status.html",
            active=storage.get_active_run(),
            latest=storage.latest_run(source="fub"),
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
