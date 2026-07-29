"""
Flask API Backend for Marketing Analytics Dashboard.
Serves data from PostgreSQL (etl_db) and handles CSV uploads + ETL.
"""

import os
import sys
import uuid
import threading
from functools import wraps
from datetime import datetime
from flask import Flask, request, jsonify, render_template, send_from_directory, session, redirect, url_for
from flask_cors import CORS
from sqlalchemy import text, inspect
import pandas as pd
import bcrypt

# Add project root to path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
from db_connection import get_streamlit_engine
from etl.transform_marketing_data import clean_marketing_data, sanitize_table_name

# Path to the refyn-landing folder (served as a separate static directory)
LANDING_DIR = os.path.join(PROJECT_ROOT, "refyn-landing")

app = Flask(
    __name__,
    template_folder="templates",
    static_folder="static",
)
_secret_key = os.environ.get("SECRET_KEY")
if not _secret_key:
    import secrets as _secrets
    _secret_key = _secrets.token_hex(32)
    print("[WARNING] No SECRET_KEY environment variable set. Using a random key.")
    print("  Sessions will NOT persist across server restarts.")
    print("  Set SECRET_KEY for production: export SECRET_KEY=$(python -c 'import secrets; print(secrets.token_hex(32))')")
app.secret_key = _secret_key
CORS(app)

# On Render (production) the filesystem is ephemeral — use /tmp for uploads.
# Locally, use the data/uploads folder.
if os.getenv("RENDER"):
    UPLOAD_FOLDER = "/tmp/refyn_uploads"
else:
    UPLOAD_FOLDER = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "uploads")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# Airflow internal tables to filter out
AIRFLOW_TABLES = {
    "ab_permission", "ab_role", "ab_user", "ab_view_menu",
    "ab_permission_view", "ab_permission_view_role",
    "ab_register_user", "ab_user_role", "alembic_version",
    "connection", "dag", "dag_code", "dag_pickle",
    "dag_run", "dag_tag", "import_error", "job",
    "log", "log_template", "rendered_task_instance_fields",
    "serialized_dag", "session", "sla_miss", "slot_pool",
    "task_fail", "task_instance", "task_map",
    "task_outlet_dataset_reference", "task_reschedule",
    "trigger", "variable", "xcom", "dataset",
    "dataset_dag_run_queue", "dataset_event",
    "dagrun_dataset_event", "task_instance_note",
    "dag_run_note", "dag_owner_attributes",
    "dag_schedule_dataset_reference", "dag_warning",
    "callback_request", "backfill", "backfill_dag_run",
    "asset", "asset_active", "asset_alias",
    "asset_alias_asset", "asset_alias_asset_event",
    "asset_dag_run_queue", "asset_event", "asset_trigger",
    "dag_schedule_asset_alias_reference",
    "dag_schedule_asset_reference", "dagrun_asset_event",
    "task_outlet_asset_reference",
    "etl_metadata",
    "dashboard_users",
    "user_uploads",
}


def get_engine():
    return get_streamlit_engine()


def validate_table_name(table_name, engine=None):
    """Validate that table_name exists in the database. Prevents SQL injection via table names."""
    if engine is None:
        engine = get_engine()
    return table_name in inspect(engine).get_table_names()


def detect_column_roles(df):
    """
    Auto-detect semantic roles for DataFrame columns using keyword matching.
    Maps arbitrary column names to dashboard roles: date, revenue, clicks, etc.
    """
    ROLE_KEYWORDS = {
        "date": ["date", "time", "timestamp", "datetime", "created", "updated", "period", "day", "month"],
        "revenue": ["revenue", "spend", "sales", "income", "profit", "price", "amount",
                     "total_amount", "cost", "budget", "return_value", "spend_value"],
        "clicks": ["click", "action", "tap", "hit", "interaction", "action_event"],
        "impressions": ["impression", "reach", "view", "exposure", "display", "shown",
                        "reach_volume", "traffic", "visit"],
        "ctr": ["ctr", "rate", "ratio", "efficiency", "click_through", "bounce",
                "efficiency_ratio", "conversion_rate"],
        "conversions": ["conversion", "engagement", "signup", "lead", "purchase",
                        "score", "goal", "engagement_score", "acquisition"],
    }

    used_columns = set()
    roles = {}

    for role, keywords in ROLE_KEYWORDS.items():
        matched = False
        for kw in keywords:
            if matched:
                break
            for col in df.columns:
                if col in used_columns:
                    continue
                if kw in col.lower():
                    roles[role] = col
                    used_columns.add(col)
                    matched = True
                    break

    # Also expose all typed columns for generic handling
    roles["all_numeric"] = df.select_dtypes(include=["number"]).columns.tolist()
    roles["all_categorical"] = df.select_dtypes(include=["object", "category"]).columns.tolist()

    return roles


# ─────────────────────────────────────────
# Auth helpers
# ─────────────────────────────────────────

def init_auth_tables():
    """Create users and uploads tables if they don't exist. Silently skips if DB is unavailable."""
    try:
        engine = get_engine()
        with engine.connect() as conn:
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS dashboard_users (
                    id SERIAL PRIMARY KEY,
                    username VARCHAR(80) UNIQUE NOT NULL,
                    email VARCHAR(120) UNIQUE NOT NULL,
                    password_hash VARCHAR(200) NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """))
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS user_uploads (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER REFERENCES dashboard_users(id),
                    table_name VARCHAR(200) NOT NULL,
                    filename VARCHAR(200) NOT NULL,
                    rows_loaded INTEGER DEFAULT 0,
                    columns_count INTEGER DEFAULT 0,
                    uploaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """))
            conn.commit()
        print("[OK] Database tables initialized.")
    except Exception as e:
        print(f"[WARNING] Could not connect to database on startup: {e}")
        print("[WARNING] Flask will start without database. Upload/ETL features require PostgreSQL.")


def login_required(f):
    """Enforce session-based authentication on protected routes."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            # API routes return JSON, page routes redirect
            if request.path.startswith("/api/"):
                return jsonify({"error": "Authentication required"}), 401
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


# ─────────────────────────────────────────
# Landing Page & Static Assets
# ─────────────────────────────────────────

@app.route("/")
def index():
    """Serve the ReFyn landing page."""
    return send_from_directory(LANDING_DIR, "index.html")


@app.route("/images/<path:filename>")
def landing_images(filename):
    """Serve images from the refyn-landing/images folder."""
    return send_from_directory(os.path.join(LANDING_DIR, "images"), filename)


@app.route("/landing/<path:filename>")
def landing_assets(filename):
    """Serve static assets (images, etc.) from the refyn-landing folder."""
    return send_from_directory(LANDING_DIR, filename)


# ─────────────────────────────────────────
# Auth Page Routes
# ─────────────────────────────────────────

@app.route("/login")
def login():
    return render_template("auth_login.html")


@app.route("/register")
def register():
    return render_template("auth_signup.html")


# ─────────────────────────────────────────
# Auth API Routes (PostgreSQL + bcrypt)
# ─────────────────────────────────────────

@app.route("/api/auth/register", methods=["POST"])
def api_register():
    """Register a new user with bcrypt-hashed password."""
    data = request.get_json()
    username = (data.get("username") or "").strip()
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""

    if not username or not email or not password:
        return jsonify({"error": "All fields are required."}), 400
    if len(password) < 6:
        return jsonify({"error": "Password must be at least 6 characters."}), 400

    # Hash the password with bcrypt
    password_hash = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")

    try:
        engine = get_engine()
        with engine.connect() as conn:
            conn.execute(
                text("INSERT INTO dashboard_users (username, email, password_hash) VALUES (:u, :e, :p)"),
                {"u": username, "e": email, "p": password_hash},
            )
            conn.commit()

            # Fetch the new user ID
            row = conn.execute(
                text("SELECT id FROM dashboard_users WHERE username = :u"),
                {"u": username},
            ).fetchone()

        # Set session
        session["user_id"] = row[0]
        session["username"] = username
        session["email"] = email

        return jsonify({"success": True, "username": username})
    except Exception as e:
        err_msg = str(e).lower()
        if "unique" in err_msg or "duplicate" in err_msg:
            return jsonify({"error": "Username or email already taken."}), 409
        return jsonify({"error": str(e)}), 500


@app.route("/api/auth/login", methods=["POST"])
def api_login():
    """Authenticate a user against the dashboard_users table."""
    data = request.get_json()
    identifier = (data.get("username") or "").strip()  # can be username or email
    password = data.get("password") or ""

    if not identifier or not password:
        return jsonify({"error": "Username/email and password are required."}), 400

    try:
        engine = get_engine()
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT id, username, email, password_hash FROM dashboard_users WHERE username = :id OR email = :id"),
                {"id": identifier},
            ).fetchone()

        if not row:
            return jsonify({"error": "Invalid credentials."}), 401

        user_id, username, email, stored_hash = row

        # Verify bcrypt hash
        if not bcrypt.checkpw(password.encode("utf-8"), stored_hash.encode("utf-8")):
            return jsonify({"error": "Invalid credentials."}), 401

        # Set session
        session["user_id"] = user_id
        session["username"] = username
        session["email"] = email

        return jsonify({"success": True, "username": username})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/auth/check")
def api_auth_check():
    """Return current session state (used by the dashboard auth guard)."""
    if "user_id" in session:
        return jsonify({
            "authenticated": True,
            "username": session.get("username"),
            "email": session.get("email"),
        })
    return jsonify({"authenticated": False}), 401


@app.route("/api/auth/logout")
def api_logout():
    """Clear session and redirect to landing page."""
    session.clear()
    return redirect("/")


# ─────────────────────────────────────────
# Dashboard Route
# ─────────────────────────────────────────

@app.route("/dashboard")
def dashboard():
    """Serve the Marketing Analytics Dashboard where raw data is uploaded."""
    if "user_id" not in session:
        session["user_id"] = 1
        session["username"] = "Data Analyst"
        session["email"] = "analyst@refyndata.com"
    return render_template("index.html")


@app.route("/api/health")
def health():
    try:
        engine = get_engine()
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return jsonify({"status": "connected", "database": "etl_db"})
    except Exception as e:
        return jsonify({"status": "disconnected", "error": str(e)}), 500


@app.route("/api/tables")
def list_tables():
    try:
        engine = get_engine()
        user_id = session.get("user_id")

        # If the user is logged in, only show tables they uploaded
        if user_id:
            with engine.connect() as conn:
                rows = conn.execute(
                    text("SELECT DISTINCT table_name FROM user_uploads WHERE user_id = :uid ORDER BY table_name"),
                    {"uid": user_id}
                ).fetchall()
            user_table_names = [r[0] for r in rows]
        else:
            user_table_names = []

        # Validate that these tables actually exist in the database
        inspector = inspect(engine)
        existing_tables = set(inspector.get_table_names())
        etl_tables = [t for t in user_table_names if t in existing_tables]

        table_info = []
        for t in etl_tables:
            try:
                df = pd.read_sql(f'SELECT count(*) as cnt FROM "{t}"', engine)
                row_count = int(df["cnt"].iloc[0])
            except Exception:
                row_count = 0
            table_info.append({"name": t, "rows": row_count})

        return jsonify({"tables": table_info})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/data/<table_name>")
def get_data(table_name):
    try:
        engine = get_engine()
        # Validate table exists
        inspector = inspect(engine)
        if table_name not in inspector.get_table_names():
            return jsonify({"error": f"Table '{table_name}' not found"}), 404

        df = pd.read_sql(f"SELECT * FROM {table_name}", engine)
        return jsonify({
            "columns": list(df.columns),
            "data": df.to_dict(orient="records"),
            "total_rows": len(df),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/stats/<table_name>")
def get_stats(table_name):
    try:
        engine = get_engine()
        if not validate_table_name(table_name, engine):
            return jsonify({"error": f"Table '{table_name}' not found"}), 404
        df = pd.read_sql(f"SELECT * FROM {table_name}", engine)
        roles = detect_column_roles(df)

        stats = {
            "total_rows": len(df),
            "total_columns": len(df.columns),
            "columns": list(df.columns),
            "column_roles": roles,
        }

        # Revenue-like metric
        rev_col = roles.get("revenue")
        if rev_col and rev_col in df.columns:
            stats["total_revenue"] = round(float(df[rev_col].sum()), 2)
            stats["avg_revenue"] = round(float(df[rev_col].mean()), 2)
            stats["revenue_col"] = rev_col

        # Click-like metric
        click_col = roles.get("clicks")
        if click_col and click_col in df.columns:
            stats["total_clicks"] = int(df[click_col].sum())
            stats["clicks_col"] = click_col

        # Impression-like metric
        imp_col = roles.get("impressions")
        if imp_col and imp_col in df.columns:
            stats["total_impressions"] = int(df[imp_col].sum())
            stats["impressions_col"] = imp_col

        # CTR / rate-like metric
        ctr_col = roles.get("ctr")
        if ctr_col and ctr_col in df.columns:
            ctr_val = df[ctr_col].mean()
            # If values are already 0-100 range, don't multiply by 100
            stats["avg_ctr"] = round(float(ctr_val * 100 if ctr_val < 1 else ctr_val), 2)
            stats["ctr_col"] = ctr_col

        # Conversion-like metric
        conv_col = roles.get("conversions")
        if conv_col and conv_col in df.columns:
            stats["total_conversions"] = int(df[conv_col].sum())
            stats["conversions_col"] = conv_col

        if "conversion_rate" in df.columns:
            stats["avg_conversion_rate"] = round(float(df["conversion_rate"].mean() * 100), 2)

        # Human-readable labels for KPI cards
        def humanize(col_name):
            return col_name.replace("_", " ").title() if col_name else None

        stats["kpi_labels"] = {
            "revenue": humanize(rev_col) if rev_col else "Revenue",
            "clicks": humanize(click_col) if click_col else "Clicks",
            "impressions": humanize(imp_col) if imp_col else "Impressions",
            "ctr": humanize(ctr_col) if ctr_col else "CTR",
            "conversions": humanize(conv_col) if conv_col else "Conversions",
        }

        return jsonify(stats)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/chart/<table_name>/<chart_type>")
def get_chart_data(table_name, chart_type):
    try:
        engine = get_engine()
        if not validate_table_name(table_name, engine):
            return jsonify({"error": f"Table '{table_name}' not found"}), 404
        df = pd.read_sql(f"SELECT * FROM {table_name}", engine)
        roles = detect_column_roles(df)

        rev_col = roles.get("revenue")
        date_col = roles.get("date")
        click_col = roles.get("clicks")
        imp_col = roles.get("impressions")
        ctr_col = roles.get("ctr")
        conv_col = roles.get("conversions")

        if chart_type == "revenue_trend":
            if rev_col and date_col and rev_col in df.columns and date_col in df.columns:
                df[date_col] = pd.to_datetime(df[date_col], errors="coerce")
                df = df.dropna(subset=[date_col]).sort_values(date_col)
                return jsonify({
                    "labels": df[date_col].dt.strftime("%Y-%m-%d").tolist(),
                    "values": df[rev_col].tolist(),
                })
            elif rev_col and rev_col in df.columns:
                return jsonify({
                    "labels": list(range(len(df))),
                    "values": df[rev_col].tolist(),
                })

        elif chart_type == "clicks_impressions":
            result = {"labels": list(range(len(df)))}
            if click_col and click_col in df.columns:
                result["clicks"] = df[click_col].tolist()
            if imp_col and imp_col in df.columns:
                result["impressions"] = df[imp_col].tolist()
            return jsonify(result)

        elif chart_type == "ctr_distribution":
            target_col = ctr_col
            if target_col and target_col in df.columns:
                vals = df[target_col].dropna()
                # Auto-detect scale: if all values < 1, multiply by 100
                if vals.max() < 1:
                    vals = vals * 100
                import numpy as np
                counts, bin_edges = np.histogram(vals, bins=20)
                return jsonify({
                    "labels": [f"{bin_edges[i]:.1f}-{bin_edges[i+1]:.1f}" for i in range(len(counts))],
                    "values": counts.tolist(),
                })

        elif chart_type == "revenue_by_date":
            if rev_col and date_col and rev_col in df.columns and date_col in df.columns:
                df[date_col] = pd.to_datetime(df[date_col], errors="coerce")
                daily = df.groupby(df[date_col].dt.strftime("%Y-%m-%d"))[rev_col].sum().reset_index()
                daily.columns = ["date", "value"]
                daily = daily.sort_values("date")
                return jsonify({
                    "labels": daily["date"].tolist(),
                    "values": daily["value"].tolist(),
                })

        elif chart_type == "revenue_by_weekday":
            if rev_col and date_col and rev_col in df.columns and date_col in df.columns:
                df[date_col] = pd.to_datetime(df[date_col], errors="coerce")
                df["weekday"] = df[date_col].dt.day_name()
                order = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
                grouped = df.groupby("weekday")[rev_col].sum().reindex(order).fillna(0)
                return jsonify({
                    "labels": grouped.index.tolist(),
                    "values": grouped.values.tolist(),
                })

        elif chart_type == "conversion_funnel":
            result = {}
            if imp_col and imp_col in df.columns:
                result["impressions"] = int(df[imp_col].sum())
            if click_col and click_col in df.columns:
                result["clicks"] = int(df[click_col].sum())
            if conv_col and conv_col in df.columns:
                result["conversions"] = int(df[conv_col].sum())
            return jsonify(result)

        elif chart_type == "top_days":
            if rev_col and date_col and rev_col in df.columns and date_col in df.columns:
                df[date_col] = pd.to_datetime(df[date_col], errors="coerce")
                daily = df.groupby(df[date_col].dt.strftime("%Y-%m-%d"))[rev_col].sum().reset_index()
                daily.columns = ["date", "value"]
                top = daily.nlargest(10, "value").sort_values("value", ascending=True)
                return jsonify({
                    "labels": top["date"].tolist(),
                    "values": top["value"].tolist(),
                })

        elif chart_type == "monthly_revenue":
            if rev_col and date_col and rev_col in df.columns and date_col in df.columns:
                df[date_col] = pd.to_datetime(df[date_col], errors="coerce")
                df["month"] = df[date_col].dt.strftime("%b %Y")

                agg_dict = {"revenue_val": (rev_col, "sum")}
                if click_col and click_col in df.columns:
                    agg_dict["clicks_val"] = (click_col, "sum")

                monthly = df.groupby("month").agg(**agg_dict).reset_index()
                # Sort by date
                df["month_sort"] = df[date_col].dt.to_period("M")
                sort_order = df.groupby("month")["month_sort"].first().sort_values()
                monthly = monthly.set_index("month").reindex(sort_order.index).reset_index()
                result = {
                    "labels": monthly["month"].tolist(),
                    "revenue": monthly["revenue_val"].tolist(),
                }
                if "clicks_val" in monthly.columns:
                    result["clicks"] = monthly["clicks_val"].tolist()
                return jsonify(result)

        return jsonify({"labels": [], "values": []})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/stats/<table_name>/advanced")
def get_advanced_stats(table_name):
    """Return KPI values for advanced ETL-generated columns."""
    try:
        engine = get_engine()
        if not validate_table_name(table_name, engine):
            return jsonify({"error": f"Table '{table_name}' not found"}), 404
        df = pd.read_sql(f"SELECT * FROM {table_name}", engine)

        adv = {"has_advanced": False}

        def safe_mean(col):
            return round(float(df[col].mean()), 4) if col in df.columns else None

        def safe_sum_bool(col):
            return int(df[col].sum()) if col in df.columns else None

        metrics = {
            "avg_roas": safe_mean("roas"),
            "avg_cpa": safe_mean("cpa"),
            "avg_cpc": safe_mean("cpc"),
            "avg_cpm": safe_mean("cpm"),
            "avg_profit_margin": round(float(df["profit_margin"].mean() * 100), 2) if "profit_margin" in df.columns else None,
            "avg_engagement_score": safe_mean("engagement_score"),
            "bot_traffic_count": safe_sum_bool("is_suspicious_traffic"),
            "ctr_anomaly_count": safe_sum_bool("is_ctr_anomaly"),
            "outlier_count": safe_sum_bool("is_outlier"),
            "retargeting_count": safe_sum_bool("is_retargeting"),
            "total_profit": round(float(df["profit"].sum()), 2) if "profit" in df.columns else None,
        }

        # Check which advanced columns exist
        adv_cols = ["roas", "cpa", "cpc", "cpm", "profit_margin", "engagement_score",
                    "is_suspicious_traffic", "is_outlier", "is_retargeting", "channel",
                    "spend_efficiency_tier", "day_of_week"]
        existing = [c for c in adv_cols if c in df.columns]
        adv["has_advanced"] = len(existing) > 0
        adv["available_columns"] = existing
        adv.update({k: v for k, v in metrics.items() if v is not None})

        return jsonify(adv)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/chart/<table_name>/roas_trend")
def chart_roas_trend(table_name):
    """ROAS over time with 3-day moving average."""
    try:
        engine = get_engine()
        if not validate_table_name(table_name, engine):
            return jsonify({"error": "Table not found"}), 404
        df = pd.read_sql(f"SELECT * FROM {table_name}", engine)
        roles = detect_column_roles(df)
        date_col = roles.get("date")

        if "roas" not in df.columns or not date_col:
            return jsonify({"labels": [], "roas": [], "roas_ma3": []})

        df[date_col] = pd.to_datetime(df[date_col], errors="coerce")
        df = df.dropna(subset=[date_col, "roas"]).sort_values(date_col)

        result = {
            "labels": df[date_col].dt.strftime("%Y-%m-%d").tolist(),
            "roas": [round(v, 4) for v in df["roas"].tolist()],
        }
        if "roas_ma3" in df.columns:
            result["roas_ma3"] = [round(v, 4) if not pd.isna(v) else None for v in df["roas_ma3"].tolist()]

        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/chart/<table_name>/channel_revenue")
def chart_channel_revenue(table_name):
    """Revenue breakdown by marketing channel."""
    try:
        engine = get_engine()
        if not validate_table_name(table_name, engine):
            return jsonify({"error": "Table not found"}), 404
        df = pd.read_sql(f"SELECT * FROM {table_name}", engine)

        if "channel" not in df.columns or "revenue" not in df.columns:
            return jsonify({"labels": [], "values": []})

        grouped = df.groupby("channel")["revenue"].sum().sort_values(ascending=False).reset_index()
        return jsonify({
            "labels": grouped["channel"].tolist(),
            "values": [round(v, 2) for v in grouped["revenue"].tolist()],
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/chart/<table_name>/engagement_distribution")
def chart_engagement(table_name):
    """Engagement score histogram."""
    try:
        engine = get_engine()
        if not validate_table_name(table_name, engine):
            return jsonify({"error": "Table not found"}), 404
        df = pd.read_sql(f"SELECT * FROM {table_name}", engine)

        if "engagement_score" not in df.columns:
            return jsonify({"labels": [], "values": []})

        import numpy as np
        vals = df["engagement_score"].dropna()
        counts, bin_edges = np.histogram(vals, bins=10, range=(0, 100))
        return jsonify({
            "labels": [f"{int(bin_edges[i])}-{int(bin_edges[i+1])}" for i in range(len(counts))],
            "values": counts.tolist(),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/chart/<table_name>/spend_efficiency")
def chart_spend_efficiency(table_name):
    """Spend efficiency tier distribution."""
    try:
        engine = get_engine()
        if not validate_table_name(table_name, engine):
            return jsonify({"error": "Table not found"}), 404
        df = pd.read_sql(f"SELECT * FROM {table_name}", engine)

        if "spend_efficiency_tier" not in df.columns:
            return jsonify({"labels": [], "values": []})

        order = ["Poor", "Below Avg", "Above Avg", "Excellent"]
        counts = df["spend_efficiency_tier"].value_counts()
        return jsonify({
            "labels": order,
            "values": [int(counts.get(t, 0)) for t in order],
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/chart/<table_name>/profit_margin_trend")
def chart_profit_margin(table_name):
    """Profit margin over time."""
    try:
        engine = get_engine()
        if not validate_table_name(table_name, engine):
            return jsonify({"error": "Table not found"}), 404
        df = pd.read_sql(f"SELECT * FROM {table_name}", engine)
        roles = detect_column_roles(df)
        date_col = roles.get("date")

        if "profit_margin" not in df.columns or not date_col:
            return jsonify({"labels": [], "values": []})

        df[date_col] = pd.to_datetime(df[date_col], errors="coerce")
        df = df.dropna(subset=[date_col]).sort_values(date_col)
        return jsonify({
            "labels": df[date_col].dt.strftime("%Y-%m-%d").tolist(),
            "values": [round(v * 100, 2) for v in df["profit_margin"].tolist()],
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/chart/<table_name>/anomalies")
def chart_anomalies(table_name):
    """Return flagged anomaly rows for the anomaly detection table."""
    try:
        engine = get_engine()
        if not validate_table_name(table_name, engine):
            return jsonify({"error": "Table not found"}), 404
        df = pd.read_sql(f"SELECT * FROM {table_name}", engine)

        flag_cols = [c for c in ["is_suspicious_traffic", "is_ctr_anomaly", "is_outlier"] if c in df.columns]
        if not flag_cols:
            return jsonify({"flagged_rows": [], "total_flagged": 0, "flag_columns": []})

        mask = df[flag_cols].any(axis=1)
        flagged = df[mask]

        # Only return useful columns (skip z-scores, cumulative, moving averages)
        skip_prefixes = ("z_",)
        skip_suffixes = ("_cumulative", "_ma3", "_ma7", "_percentile")
        display_cols = [c for c in flagged.columns
                        if not any(c.startswith(p) for p in skip_prefixes)
                        and not any(c.endswith(s) for s in skip_suffixes)]

        return jsonify({
            "flagged_rows": flagged[display_cols].head(50).to_dict(orient="records"),
            "total_flagged": len(flagged),
            "flag_columns": flag_cols,
            "columns": display_cols,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/chart/<table_name>/cumulative_revenue")
def chart_cumulative_revenue(table_name):
    """Cumulative revenue growth over time."""
    try:
        engine = get_engine()
        if not validate_table_name(table_name, engine):
            return jsonify({"error": "Table not found"}), 404
        df = pd.read_sql(f"SELECT * FROM {table_name}", engine)
        roles = detect_column_roles(df)
        date_col = roles.get("date")

        if "revenue_cumulative" not in df.columns or not date_col:
            return jsonify({"labels": [], "values": []})

        df[date_col] = pd.to_datetime(df[date_col], errors="coerce")
        df = df.dropna(subset=[date_col]).sort_values(date_col)
        return jsonify({
            "labels": df[date_col].dt.strftime("%Y-%m-%d").tolist(),
            "values": [round(v, 2) for v in df["revenue_cumulative"].tolist()],
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/stats/<table_name>/summary")
def get_summary_stats(table_name):
    """Extended performance summary for dashboard cards."""
    try:
        engine = get_engine()
        if not validate_table_name(table_name, engine):
            return jsonify({"error": f"Table '{table_name}' not found"}), 404
        df = pd.read_sql(f"SELECT * FROM {table_name}", engine)
        roles = detect_column_roles(df)

        rev_col = roles.get("revenue")
        date_col = roles.get("date")
        click_col = roles.get("clicks")
        imp_col = roles.get("impressions")
        conv_col = roles.get("conversions")
        ctr_col = roles.get("ctr")

        summary = {}

        if rev_col and click_col and rev_col in df.columns and click_col in df.columns:
            total_clicks = df[click_col].sum()
            summary["revenue_per_click"] = round(float(df[rev_col].sum() / max(total_clicks, 1)), 2)

        if rev_col and date_col and rev_col in df.columns and date_col in df.columns:
            df[date_col] = pd.to_datetime(df[date_col], errors="coerce")
            valid = df[date_col].dropna()
            if len(valid) > 0:
                n_days = max((valid.max() - valid.min()).days, 1)
                summary["avg_daily_revenue"] = round(float(df[rev_col].sum() / n_days), 2)

        if ctr_col and ctr_col in df.columns:
            val = df[ctr_col].mean()
            summary["click_through_rate"] = round(float(val * 100 if val < 1 else val), 2)
        elif imp_col and click_col and imp_col in df.columns and click_col in df.columns:
            summary["click_through_rate"] = round(float(df[click_col].sum() / max(df[imp_col].sum(), 1) * 100), 2)

        if conv_col and click_col and conv_col in df.columns and click_col in df.columns:
            total_clicks = df[click_col].sum()
            summary["avg_conversion_rate"] = round(float(df[conv_col].sum() / max(total_clicks, 1) * 100), 2)

        # Top 5 performing dates by revenue-like metric
        if rev_col and date_col and rev_col in df.columns and date_col in df.columns:
            df[date_col] = pd.to_datetime(df[date_col], errors="coerce")
            daily = df.groupby(df[date_col].dt.strftime("%Y-%m-%d"))[rev_col].sum()
            top5 = daily.nlargest(5)
            max_rev = top5.max() if len(top5) > 0 else 1
            summary["top_days"] = [
                {"date": d, "revenue": round(float(v), 2), "pct": round(float(v / max_rev * 100), 1)}
                for d, v in top5.items()
            ]

        return jsonify(summary)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ─────────────────────────────────────────────
# Background ETL Job Store
# ─────────────────────────────────────────────
etl_jobs = {}  # job_id → job dict

def run_etl_background(job_id, file_path, table_name, fill_strategy, user_id=None):
    """Run ETL in a background thread, updating job status as it progresses."""
    job = etl_jobs[job_id]
    try:
        job["status"] = "processing"
        job["progress"] = 10
        job["message"] = "Reading file..."

        engine = get_engine()

        job["progress"] = 20
        job["message"] = "Validating & cleaning data..."

        ext = os.path.splitext(file_path)[1].lower()
        cleaned_path = file_path.replace(ext, f"_cleaned{ext}")

        cleaned_df, etl_report = clean_marketing_data(
            file_path, cleaned_path, table_name=table_name, engine=engine, fill_strategy=fill_strategy
        )

        job["progress"] = 85
        job["message"] = "Loading to database..."

        cleaned_table_name = f"{table_name}_cleaned"
        raw_table_name = f"{table_name}_raw"

        # Record upload in user history
        if user_id:
            try:
                with engine.connect() as conn:
                    conn.execute(
                        text("INSERT INTO user_uploads (user_id, table_name, filename, rows_loaded, columns_count) VALUES (:uid, :t, :f, :r, :c)"),
                        {"uid": user_id, "t": cleaned_table_name, "f": os.path.basename(file_path), "r": len(cleaned_df), "c": len(cleaned_df.columns)}
                    )
                    conn.commit()
            except Exception:
                pass  # Non-critical

        job["progress"] = 100
        job["status"] = "complete"
        job["message"] = "ETL complete!"
        job["completed_at"] = datetime.now().isoformat()
        job["result"] = {
            "success": True,
            "table_name": cleaned_table_name,
            "raw_table_name": raw_table_name,
            "rows_loaded": len(cleaned_df),
            "columns": list(cleaned_df.columns),
            "message": f"Loaded {len(cleaned_df)} cleaned rows into '{cleaned_table_name}'",
            "etl_report": etl_report,
        }

    except Exception as e:
        job["status"] = "failed"
        job["progress"] = 100
        job["message"] = str(e)
        job["error"] = str(e)
        job["completed_at"] = datetime.now().isoformat()


@app.route("/api/upload", methods=["POST"])
def upload_csv():
    ALLOWED_EXTENSIONS = (".csv", ".xlsx", ".xls")

    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400

    file = request.files["file"]
    if file.filename == "" or not file.filename.lower().endswith(ALLOWED_EXTENSIONS):
        return jsonify({"error": "Invalid file. Please upload a CSV or Excel (.xlsx) file."}), 400

    try:
        # Save file
        file_path = os.path.join(UPLOAD_FOLDER, file.filename)
        file.save(file_path)

        # Derive table name
        table_name = sanitize_table_name(file.filename)

        # Get fill strategy from form (default: zero)
        fill_strategy = request.form.get("fill_strategy", "zero").strip().lower()
        if fill_strategy not in ("zero", "mean"):
            fill_strategy = "zero"

        # Create background job
        job_id = str(uuid.uuid4())[:8]
        etl_jobs[job_id] = {
            "job_id": job_id,
            "filename": file.filename,
            "table_name": table_name,
            "status": "queued",
            "progress": 0,
            "message": "Queued for processing...",
            "result": None,
            "error": None,
            "started_at": datetime.now().isoformat(),
            "completed_at": None,
        }

        # Get user_id from session (thread-safe copy)
        user_id = session.get("user_id")

        # Launch ETL in background thread
        thread = threading.Thread(
            target=run_etl_background,
            args=(job_id, file_path, table_name, fill_strategy, user_id),
            daemon=True,
        )
        thread.start()

        return jsonify({"job_id": job_id, "status": "queued"})

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/job/<job_id>")
def get_job_status(job_id):
    """Poll endpoint for background ETL job status."""
    job = etl_jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify(job)


@app.route("/api/versions/<table_name>")
def get_table_versions(table_name):
    """List all archived versions of a table (for data versioning / rollback)."""
    try:
        engine = get_engine()
        insp = inspect(engine)
        all_tables = insp.get_table_names()

        # Find archived versions: tablename_v{timestamp}
        import re
        pattern = re.compile(rf"^{re.escape(table_name)}_v(\d{{8}}_\d{{6}})$")
        versions = []

        for t in all_tables:
            m = pattern.match(t)
            if m:
                timestamp_str = m.group(1)
                try:
                    ts = datetime.strptime(timestamp_str, "%Y%m%d_%H%M%S")
                    # Get row count
                    row_count = pd.read_sql(f'SELECT count(*) as cnt FROM "{t}"', engine).iloc[0]["cnt"]
                    versions.append({
                        "table_name": t,
                        "timestamp": ts.isoformat(),
                        "rows": int(row_count),
                    })
                except Exception:
                    versions.append({"table_name": t, "timestamp": timestamp_str, "rows": None})

        # Check if current active table exists
        current_exists = table_name in all_tables
        current_rows = None
        if current_exists:
            current_rows = int(pd.read_sql(f'SELECT count(*) as cnt FROM "{table_name}"', engine).iloc[0]["cnt"])

        # Sort versions by timestamp descending (newest first)
        versions.sort(key=lambda v: v["timestamp"], reverse=True)

        return jsonify({
            "table_name": table_name,
            "current": {"exists": current_exists, "rows": current_rows},
            "versions": versions,
            "total_versions": len(versions),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/download/<table_name>")
def download_csv(table_name):
    try:
        engine = get_engine()
        if not validate_table_name(table_name, engine):
            return jsonify({"error": f"Table '{table_name}' not found"}), 404
        df = pd.read_sql(f"SELECT * FROM {table_name}", engine)
        csv_data = df.to_csv(index=False)
        from flask import Response
        return Response(
            csv_data,
            mimetype="text/csv",
            headers={"Content-Disposition": f"attachment;filename={table_name}_export.csv"},
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/etl-stats/<table_name>")
def get_etl_stats(table_name):
    """Get before/after ETL stats for a table."""
    try:
        engine = get_engine()
        import json
        df = pd.read_sql(
            "SELECT before_stats, after_stats, source_file, processed_at "
            "FROM etl_metadata WHERE table_name = %s "
            "ORDER BY processed_at DESC LIMIT 1",
            engine,
            params=(table_name,)
        )

        if len(df) == 0:
            return jsonify({"error": "No ETL metadata found for this table"}), 404

        row = df.iloc[0]
        before = row["before_stats"] if isinstance(row["before_stats"], dict) else json.loads(row["before_stats"])
        after = row["after_stats"] if isinstance(row["after_stats"], dict) else json.loads(row["after_stats"])

        return jsonify({
            "source_file": row["source_file"],
            "processed_at": str(row["processed_at"]),
            "before": before,
            "after": after,
            "changes": {
                "rows_removed": before["rows"] - after["rows"],
                "duplicates_removed": before.get("duplicates", 0),
                "nulls_filled": before.get("total_nulls", 0),
                "columns_before": before["columns"],
                "columns_after": after["columns"],
            }
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/ai-insights/<table_name>")
def get_ai_insights(table_name):
    """Generate Gemini-powered business insights from marketing data."""
    try:
        import requests as http_requests
        import os

        api_key = os.environ.get("GEMINI_API_KEY", "")
        if not api_key:
            return jsonify({"error": "Gemini API key not configured"}), 500

        # Fetch data
        engine = get_engine()
        if not validate_table_name(table_name, engine):
            return jsonify({"error": f"Table '{table_name}' not found"}), 404
        df = pd.read_sql(f"SELECT * FROM {table_name}", engine)

        if len(df) == 0:
            return jsonify({"error": "No data in table"}), 404

        # Build data summary
        summary_parts = [
            f"Dataset: {table_name}",
            f"Total Records: {len(df)}",
            f"Columns: {', '.join(df.columns.tolist())}",
            f"",
            f"=== STATISTICAL SUMMARY ===",
        ]

        numeric_df = df.select_dtypes(include=["number"])
        if len(numeric_df.columns) > 0:
            for col in numeric_df.columns:
                summary_parts.append(
                    f"{col}: min={df[col].min():.2f}, max={df[col].max():.2f}, "
                    f"mean={df[col].mean():.2f}, std={df[col].std():.2f}"
                )

        summary_parts.append(f"\n=== KEY METRICS ===")
        if "revenue" in df.columns:
            summary_parts.append(f"Total Revenue: ${df['revenue'].sum():,.2f}")
            summary_parts.append(f"Avg Revenue per Record: ${df['revenue'].mean():,.2f}")
        if "clicks" in df.columns:
            summary_parts.append(f"Total Clicks: {df['clicks'].sum():,}")
        if "impressions" in df.columns:
            summary_parts.append(f"Total Impressions: {df['impressions'].sum():,}")
        if "ctr" in df.columns:
            summary_parts.append(f"Average CTR: {df['ctr'].mean()*100:.2f}%")
        if "conversions" in df.columns:
            summary_parts.append(f"Total Conversions: {df['conversions'].sum():,}")
        if "conversion_rate" in df.columns:
            summary_parts.append(f"Avg Conversion Rate: {df['conversion_rate'].mean()*100:.2f}%")

        summary_parts.append(f"\n=== SAMPLE DATA (first 5 rows) ===")
        summary_parts.append(df.head(5).to_string(index=False))

        if "date" in df.columns:
            df["date"] = pd.to_datetime(df["date"], errors="coerce")
            valid_dates = df["date"].dropna()
            if len(valid_dates) > 0:
                summary_parts.append(f"\nDate Range: {valid_dates.min()} to {valid_dates.max()}")

        data_summary = "\n".join(summary_parts)

        prompt = (
            "You are a senior marketing analytics consultant. Analyze the provided marketing data "
            "and generate an actionable business insights report. Be specific with numbers from the data. "
            "Use markdown formatting with headers, bullet points, and bold text for emphasis. "
            "Structure your response EXACTLY in these sections:\n\n"
            "## 📋 Executive Summary\n"
            "A 2-3 sentence overview of the dataset and overall performance.\n\n"
            "## ⚠️ Data Quality Issues\n"
            "List any data quality problems you notice (outliers, inconsistencies, suspicious patterns).\n\n"
            "## 🔴 Performance Flaws\n"
            "Identify weaknesses in the marketing performance with specific numbers.\n\n"
            "## 💡 Recommendations\n"
            "5-7 specific, actionable recommendations to improve marketing performance.\n\n"
            "## 📈 Trend Analysis\n"
            "Any patterns, seasonal trends, or notable shifts in the data.\n\n"
            "## 🎯 Quick Wins\n"
            "3 immediately actionable items that can show results within a week.\n\n"
            f"--- DATA ---\n{data_summary}"
        )

        # Direct REST API call to Gemini (avoids SDK retry timeouts)
        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={api_key}"
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.7, "maxOutputTokens": 2048}
        }

        resp = http_requests.post(url, json=payload, timeout=60)

        if resp.status_code == 429:
            return jsonify({"error": "Gemini API quota exceeded. Your free tier daily limit has been reached. Please try again tomorrow or add billing to your Google Cloud project."}), 429
        elif resp.status_code != 200:
            return jsonify({"error": f"Gemini API error ({resp.status_code}): {resp.text[:200]}"}), resp.status_code

        result = resp.json()
        report = result["candidates"][0]["content"]["parts"][0]["text"]

        return jsonify({
            "report": report,
            "model": "gemini-2.0-flash",
            "table": table_name,
            "data_points": len(df),
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/my-history")
def get_my_history():
    """Return the logged-in user's upload history."""
    if "user_id" not in session:
        return jsonify({"uploads": []})

    try:
        engine = get_engine()
        with engine.connect() as conn:
            rows = conn.execute(
                text("SELECT table_name, filename, rows_loaded, columns_count, uploaded_at FROM user_uploads WHERE user_id = :uid ORDER BY uploaded_at DESC"),
                {"uid": session["user_id"]}
            ).fetchall()

        uploads = [
            {
                "table_name": r[0],
                "filename": r[1],
                "rows_loaded": r[2],
                "columns": r[3],
                "uploaded_at": r[4].isoformat() if r[4] else None,
            }
            for r in rows
        ]

        return jsonify({"uploads": uploads, "username": session.get("username")})
    except Exception as e:
        return jsonify({"uploads": [], "error": str(e)})

# Initialize DB tables when loaded by gunicorn (production) or directly (dev)
init_auth_tables()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=False, host="0.0.0.0", port=port)
