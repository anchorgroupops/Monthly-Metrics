"""
Daily metrics dashboard routes.

Registered as a blueprint in dashboard.py's create_app().
"""

from __future__ import annotations

import logging
from datetime import datetime

from flask import Blueprint, redirect, render_template, session, url_for

from config.settings import BRAND

log = logging.getLogger(__name__)

bp = Blueprint("daily", __name__)


def _login_required(f):
    """Lightweight login check — redirects to main login."""
    from functools import wraps

    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("authenticated"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)

    return wrapper


@bp.route("/daily")
@_login_required
def daily():
    """Daily metrics dashboard with progress bars."""
    try:
        from src.fub_daily_metrics import (
            TARGETS,
            calc_team_averages,
            fetch_daily_metrics,
            save_daily_snapshot,
        )

        results = fetch_daily_metrics(days=30)
        save_daily_snapshot(results)
        team_avg = calc_team_averages(results)

        # Sort agents: those with Zillow leads first, then by response time
        results.sort(
            key=lambda r: (
                0 if r["metrics"]["total_zillow_leads"] > 0 else 1,
                r["metrics"].get("response_time_avg") or 999999,
            )
        )

        return render_template(
            "admin/daily.html",
            brand=BRAND,
            agents=results,
            team_avg=team_avg,
            targets=TARGETS,
            updated_at=datetime.now().strftime("%I:%M %p"),
            error=None,
        )
    except Exception as e:
        log.exception("Daily metrics fetch failed")
        return render_template(
            "admin/daily.html",
            brand=BRAND,
            agents=[],
            team_avg=None,
            targets={},
            updated_at=datetime.now().strftime("%I:%M %p"),
            error=str(e),
        )


@bp.route("/daily/refresh", methods=["POST"])
@_login_required
def daily_refresh():
    """Force refresh daily metrics."""
    return redirect(url_for("daily.daily"))


@bp.route("/daily/data")
@_login_required
def daily_data():
    """HTMX endpoint for auto-refresh (returns empty for now, triggers page reload)."""
    return "", 204
