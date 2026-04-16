import pandas as pd
import re
import os
import json
import logging
from datetime import datetime
from db_connection import get_engine
from sqlalchemy import inspect, text

logger = logging.getLogger(__name__)


def _archive_table_if_exists(engine, table_name):
    """Archive an existing table by renaming it with a timestamp suffix.
    E.g., 'sales_cleaned' -> 'sales_cleaned_v20260414_013100'
    Silently skips if the table doesn't exist.
    """
    insp = inspect(engine)
    if table_name not in insp.get_table_names():
        return  # Nothing to archive

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    archive_name = f"{table_name}_v{timestamp}"

    try:
        with engine.connect() as conn:
            conn.execute(text(f'ALTER TABLE "{table_name}" RENAME TO "{archive_name}"'))
            conn.commit()
        logger.info("   Archived '%s' -> '%s'", table_name, archive_name)
    except Exception as e:
        logger.warning("   Could not archive '%s': %s", table_name, str(e))


def ensure_table_exists(engine, df, table_name="marketing_data"):
    inspector = inspect(engine)

    if table_name not in inspector.get_table_names():
        df.head(0).to_sql(
            name=table_name,
            con=engine,
            if_exists="replace",
            index=False,
        )


def sanitize_table_name(filename):
    """
    Converts a filename into a safe PostgreSQL table name.
    e.g. 'Sales Data (2024).csv' → 'sales_data_2024'
    """
    name = filename.lower()
    name = name.replace(".csv", "").replace(".xlsx", "").replace(".xls", "")
    name = re.sub(r"[^a-z0-9]+", "_", name)  # replace non-alphanumeric with _
    name = name.strip("_")
    if name[0].isdigit():
        name = "t_" + name
    return name


def capture_data_stats(df, label="raw"):
    """Capture data quality metrics from a DataFrame."""
    stats = {
        "label": label,
        "rows": int(len(df)),
        "columns": int(len(df.columns)),
        "column_names": list(df.columns),
        "duplicates": int(df.duplicated().sum()),
        "total_nulls": int(df.isnull().sum().sum()),
        "null_percentage": round(float(df.isnull().sum().sum() / (len(df) * len(df.columns)) * 100), 2) if len(df) > 0 else 0,
        "null_per_column": {col: int(df[col].isnull().sum()) for col in df.columns},
        "dtypes": {col: str(df[col].dtype) for col in df.columns},
        "memory_mb": round(df.memory_usage(deep=True).sum() / (1024 * 1024), 3),
    }

    # Numeric stats
    numeric_cols = df.select_dtypes(include=["number"]).columns
    if len(numeric_cols) > 0:
        stats["numeric_summary"] = {}
        for col in numeric_cols:
            stats["numeric_summary"][col] = {
                "min": round(float(df[col].min()), 4) if not pd.isna(df[col].min()) else 0,
                "max": round(float(df[col].max()), 4) if not pd.isna(df[col].max()) else 0,
                "mean": round(float(df[col].mean()), 4) if not pd.isna(df[col].mean()) else 0,
                "std": round(float(df[col].std()), 4) if not pd.isna(df[col].std()) else 0,
            }

    return stats


def save_etl_metadata(engine, table_name, before_stats, after_stats, filename=""):
    """Save ETL before/after stats to etl_metadata table."""
    # Create metadata table if not exists
    create_sql = text("""
        CREATE TABLE IF NOT EXISTS etl_metadata (
            id SERIAL PRIMARY KEY,
            table_name VARCHAR(255) NOT NULL,
            source_file VARCHAR(500),
            processed_at TIMESTAMP DEFAULT NOW(),
            before_stats JSONB,
            after_stats JSONB
        )
    """)

    with engine.connect() as conn:
        conn.execute(create_sql)
        conn.execute(
            text("""
                INSERT INTO etl_metadata (table_name, source_file, processed_at, before_stats, after_stats)
                VALUES (:table_name, :source_file, :processed_at, :before_stats, :after_stats)
            """),
            {
                "table_name": table_name,
                "source_file": filename,
                "processed_at": datetime.now(),
                "before_stats": json.dumps(before_stats),
                "after_stats": json.dumps(after_stats),
            }
        )
        conn.commit()

    print(f"[STATS] Saved ETL metadata for '{table_name}'")


def validate_input(df, filename=""):
    """
    Validate input DataFrame before cleaning.
    Returns a validation report dict with warnings, errors, and profiling info.
    """
    report = {
        "valid": True,
        "errors": [],
        "warnings": [],
        "profile": {},
    }

    # ── Fatal checks ────────────────────────────────────────────────────────
    if len(df) == 0:
        report["valid"] = False
        report["errors"].append("File is empty (0 rows)")
        return report

    if len(df.columns) < 2:
        report["valid"] = False
        report["errors"].append(f"File has only {len(df.columns)} column(s). Minimum 2 required for meaningful analysis.")
        return report

    # ── Column-level checks ─────────────────────────────────────────────────
    all_null_cols = [col for col in df.columns if df[col].isnull().all()]
    if all_null_cols:
        report["warnings"].append(f"{len(all_null_cols)} column(s) are entirely null: {', '.join(all_null_cols[:5])}")

    high_null_cols = []
    for col in df.columns:
        null_pct = df[col].isnull().sum() / len(df) * 100
        if 50 <= null_pct < 100:
            high_null_cols.append(f"{col} ({null_pct:.0f}%)")
    if high_null_cols:
        report["warnings"].append(f"{len(high_null_cols)} column(s) have >50% nulls: {', '.join(high_null_cols[:5])}")

    # ── Duplicate check ─────────────────────────────────────────────────────
    dup_count = df.duplicated().sum()
    dup_pct = dup_count / len(df) * 100
    if dup_pct > 30:
        report["warnings"].append(f"High duplicate rate: {dup_count} rows ({dup_pct:.1f}%) are duplicates")

    # ── Outlier detection (Z-score > 3 for numeric columns) ─────────────────
    numeric_cols = df.select_dtypes(include=["number"]).columns
    outlier_summary = {}
    for col in numeric_cols:
        col_data = df[col].dropna()
        if len(col_data) < 10:
            continue
        mean = col_data.mean()
        std = col_data.std()
        if std == 0:
            continue
        z_scores = ((col_data - mean) / std).abs()
        n_outliers = int((z_scores > 3).sum())
        if n_outliers > 0:
            outlier_summary[col] = {
                "count": n_outliers,
                "pct": round(n_outliers / len(col_data) * 100, 2),
                "min_val": round(float(col_data[z_scores > 3].min()), 2),
                "max_val": round(float(col_data[z_scores > 3].max()), 2),
            }

    if outlier_summary:
        total_outliers = sum(v["count"] for v in outlier_summary.values())
        report["warnings"].append(f"Detected {total_outliers} outlier(s) across {len(outlier_summary)} column(s) (|Z| > 3)")
    report["outliers"] = outlier_summary

    # ── Data profiling ──────────────────────────────────────────────────────
    profile = {}
    for col in df.columns:
        col_profile = {
            "dtype": str(df[col].dtype),
            "null_count": int(df[col].isnull().sum()),
            "null_pct": round(df[col].isnull().sum() / len(df) * 100, 1),
            "unique_count": int(df[col].nunique()),
            "unique_pct": round(df[col].nunique() / max(len(df), 1) * 100, 1),
        }
        if col in numeric_cols:
            col_data = df[col].dropna()
            if len(col_data) > 0:
                col_profile["min"] = round(float(col_data.min()), 4)
                col_profile["max"] = round(float(col_data.max()), 4)
                col_profile["mean"] = round(float(col_data.mean()), 4)
                col_profile["median"] = round(float(col_data.median()), 4)
                col_profile["std"] = round(float(col_data.std()), 4)
        else:
            top_values = df[col].value_counts().head(3)
            if len(top_values) > 0:
                col_profile["top_values"] = {str(k): int(v) for k, v in top_values.items()}
        profile[col] = col_profile

    report["profile"] = profile
    report["summary"] = {
        "total_rows": len(df),
        "total_columns": len(df.columns),
        "numeric_columns": len(numeric_cols),
        "categorical_columns": len(df.select_dtypes(include=["object", "category"]).columns),
        "total_nulls": int(df.isnull().sum().sum()),
        "total_duplicates": int(dup_count),
        "memory_mb": round(df.memory_usage(deep=True).sum() / (1024 * 1024), 3),
    }

    return report


def clean_marketing_data(
    input_path,
    output_path=None,
    table_name="marketing_data",
    engine=None,
    fill_strategy="zero",
    strategy_numeric=None,
    strategy_categorical="mode",
):
    """
    Cleans and transforms marketing data.
    Standardizes schema and loads into PostgreSQL.

    Args:
        input_path: path to source CSV
        output_path: optional path to save cleaned CSV
        table_name: target table in DB
        engine: optional SQLAlchemy engine
        fill_strategy: legacy numeric strategy — "zero" or "mean"
            (overridden by strategy_numeric if provided)
        strategy_numeric: "zero" | "mean"  — how to fill numeric NaNs
        strategy_categorical: "mode" | "constant"  — how to fill categorical NaNs
            "mode"     → most frequent value (falls back to 'Unknown' if empty)
            "constant" → fills with 'Unknown'

    Returns: (cleaned_df, etl_report)
    """

    # -----------------------------
    # [LOAD] Robust file loading (CSV + Excel)
    # -----------------------------
    input_lower = input_path.lower()
    if input_lower.endswith(".xlsx") or input_lower.endswith(".xls"):
        try:
            df = pd.read_excel(input_path, sheet_name=0)
            print(f"[EXCEL] Loaded Excel file (sheet: first)")
        except Exception as e:
            raise ValueError(f"Failed to read Excel file: {e}")
    elif input_lower.endswith(".csv"):
        try:
            df = pd.read_csv(input_path, encoding="utf-8")
        except UnicodeDecodeError:
            try:
                df = pd.read_csv(input_path, encoding="latin1")
            except UnicodeDecodeError:
                df = pd.read_csv(input_path, encoding="ISO-8859-1")
        print("[CSV] Loaded CSV file")
    else:
        raise ValueError(f"Unsupported file type: '{input_path.split('.')[-1]}'. Only CSV and Excel (.xlsx/.xls) files are supported.")

    # ─────────────────────────────
    # [VALIDATE] VALIDATE INPUT DATA
    # ─────────────────────────────
    validation = validate_input(df, filename=os.path.basename(input_path))
    if not validation["valid"]:
        raise ValueError(f"Validation failed: {'; '.join(validation['errors'])}")
    if validation["warnings"]:
        for w in validation["warnings"]:
            print(f"   [WARN] {w}")

    # ─────────────────────────────
    # [STATS] CAPTURE BEFORE STATS
    # ─────────────────────────────
    before_stats = capture_data_stats(df, label="before")
    print(f"   Before: {before_stats['rows']} rows, {before_stats['columns']} cols, "
          f"{before_stats['duplicates']} duplicates, {before_stats['total_nulls']} nulls")

    # -----------------------------
    # [CLEAN] Basic cleaning — schema-safe
    # -----------------------------
    df.columns = df.columns.str.strip().str.lower().str.replace(" ", "_", regex=False)
    df = df.drop_duplicates()

    # -----------------------------
    # [FILL] Fill missing values — numeric + categorical
    # -----------------------------
    # Resolve effective numeric strategy (strategy_numeric takes precedence)
    eff_numeric = (strategy_numeric or fill_strategy or "zero").lower().strip()
    eff_categorical = (strategy_categorical or "mode").lower().strip()

    numeric_cols = df.select_dtypes(include=["number"]).columns.tolist()
    categorical_cols = df.select_dtypes(include=["object", "category", "string"]).columns.tolist()

    # ── Numeric imputation ──────────────────────────────────────────────────
    if eff_numeric == "mean":
        for col in numeric_cols:
            col_mean = df[col].mean()
            df[col] = df[col].fillna(col_mean if not pd.isna(col_mean) else 0)
        print("[FILL] Numeric fill: MEAN (column average)")
    else:  # zero (default / legacy)
        for col in numeric_cols:
            df[col] = df[col].fillna(0)
        print("[FILL] Numeric fill: ZERO")

    # ── Categorical imputation ───────────────────────────────────────────────
    for col in categorical_cols:
        if df[col].isnull().all():
            # Edge case: entire column is null → use 'Unknown'
            df[col] = df[col].fillna("Unknown")
            print(f"   [WARN]  '{col}': all-null column → filled with 'Unknown'")
            continue

        if eff_categorical == "constant":
            df[col] = df[col].fillna("Unknown")
        else:  # mode (default)
            mode_vals = df[col].mode()
            fill_val = mode_vals[0] if len(mode_vals) > 0 else "Unknown"
            df[col] = df[col].fillna(fill_val)

    if categorical_cols:
        print(f"[FILL] Categorical fill: {eff_categorical.upper()} ({len(categorical_cols)} columns)")

    # legacy: keep fill_strategy in sync for reporting
    fill_strategy = eff_numeric

    # -----------------------------
    # [ETL] Column standardization (optional aliases — safe to skip)
    # -----------------------------
    COLUMN_MAPPING = {
        "ad_clicks": "clicks",
        "total_clicks": "clicks",
        "adclicks": "clicks",
        "impr": "impressions",
        "views": "impressions",
        "qty": "quantity",
        "units_sold": "quantity",
        "cost": "price",
        "amount": "price",
    }
    # Only rename columns that actually exist in this CSV
    rename_map = {k: v for k, v in COLUMN_MAPPING.items() if k in df.columns}
    if rename_map:
        df = df.rename(columns=rename_map)
        logger.info(f"[RENAME] Renamed columns: {rename_map}")

    # -----------------------------
    # [DETECT]️ Smart column detection (keyword-based, case-insensitive)
    # -----------------------------
    DATE_KEYWORDS = ["date", "time", "timestamp", "datetime", "created", "updated"]
    METRIC_KEYWORDS = ["click", "ctr", "impression", "conversion", "bounce", "session"]
    REVENUE_KEYWORDS = ["revenue", "sales", "income", "profit", "amount", "total"]

    # Auto-detect and convert date columns
    for col in df.columns:
        if any(kw in col for kw in DATE_KEYWORDS):
            try:
                converted = pd.to_datetime(df[col], errors="coerce")
                if converted.isnull().all() and not df[col].isnull().all():
                    logger.warning(f"[WARN] '{col}' failed datetime parse completely. Reverting.")
                else:
                    df[col] = converted
                    logger.info(f"[DATE] Auto-converted '{col}' to datetime")
            except Exception:
                logger.warning(f"[WARN] Could not parse '{col}' as datetime, skipping")

    # Log detected column roles (informational)
    detected_metrics = [c for c in df.columns if any(kw in c for kw in METRIC_KEYWORDS)]
    detected_revenue = [c for c in df.columns if any(kw in c for kw in REVENUE_KEYWORDS)]
    if detected_metrics:
        logger.info(f"[STATS] Detected metric columns: {detected_metrics}")
    if detected_revenue:
        logger.info(f"[MONEY] Detected revenue columns: {detected_revenue}")

    # -----------------------------
    # [FIX] Feature engineering (conditional — only if columns exist)
    # -----------------------------
    if "price" in df.columns and "quantity" in df.columns:
        df["revenue"] = df["price"] * df["quantity"]
        logger.info("[ADD] Added 'revenue' = price × quantity")

    if "clicks" in df.columns and "impressions" in df.columns:
        df["ctr"] = df["clicks"] / df["impressions"].replace(0, 1)
        logger.info("[ADD] Added 'ctr' = clicks / impressions")
    else:
        logger.info("[INFO] Skipped CTR — 'clicks' and/or 'impressions' not found")

    if "conversions" in df.columns and "clicks" in df.columns:
        df["conversion_rate"] = df["conversions"] / df["clicks"].replace(0, 1)
        logger.info("[ADD] Added 'conversion_rate' = conversions / clicks")

    # =====================================================================
    # --- [ADVANCED TRANSFORMATIONS] ---
    # These are schema-agnostic: each block only fires when the required
    # columns exist. New columns are appended — nothing is dropped.
    # =====================================================================

    # ── 1. Time-Series & Seasonality Features ────────────────────────────
    datetime_cols = df.select_dtypes(include=['datetime64[ns]', 'datetime64[ns, UTC]']).columns
    if len(datetime_cols) > 0:
        dt_col = datetime_cols[0]
        df["day_of_week"] = df[dt_col].dt.day_name()
        df["is_weekend"] = df[dt_col].dt.dayofweek >= 5
        df["month"] = df[dt_col].dt.strftime('%Y-%m')
        df["quarter"] = df[dt_col].dt.to_period('Q').astype(str)
        df["week_number"] = df[dt_col].dt.isocalendar().week.astype(int)
        df["days_since_start"] = (df[dt_col] - df[dt_col].min()).dt.days
        logger.info(f"[ADD] Extracted temporal features (day_of_week, is_weekend, month, quarter, week_number, days_since_start) from '{dt_col}'")

    # ── 2. Funnel Efficiency Metrics (ROAS, CPA, CPM, CPC) ──────────────
    spend_col = next((c for c in df.columns if c in ["spend", "ad_spend", "cost", "budget"]), None)
    if spend_col:
        if "revenue" in df.columns:
            df["roas"] = df["revenue"] / df[spend_col].replace(0, 1)
            logger.info(f"[ADD] Added 'roas' = revenue / {spend_col}")

        if "conversions" in df.columns:
            df["cpa"] = df[spend_col] / df["conversions"].replace(0, 1)
            logger.info(f"[ADD] Added 'cpa' = {spend_col} / conversions")

        if "clicks" in df.columns:
            df["cpc"] = df[spend_col] / df["clicks"].replace(0, 1)
            logger.info(f"[ADD] Added 'cpc' = {spend_col} / clicks")

        if "impressions" in df.columns:
            df["cpm"] = (df[spend_col] / df["impressions"].replace(0, 1)) * 1000
            logger.info(f"[ADD] Added 'cpm' = ({spend_col} / impressions) × 1000")

    # ── 3. Profit Margin ─────────────────────────────────────────────────
    if "revenue" in df.columns and spend_col:
        df["profit"] = df["revenue"] - df[spend_col]
        df["profit_margin"] = df["profit"] / df["revenue"].replace(0, 1)
        logger.info("[ADD] Added 'profit' and 'profit_margin'")

    # ── 4. Bot Traffic & Multi-Signal Anomaly Flagging ───────────────────
    if "clicks" in df.columns and "conversions" in df.columns:
        df["is_suspicious_traffic"] = (df["clicks"] > 200) & (df["conversions"] == 0)
        logger.info("[ADD] Added 'is_suspicious_traffic' flag")

    if "clicks" in df.columns and "impressions" in df.columns:
        # CTR > 100% is physically impossible → broken tracking or bots
        df["is_ctr_anomaly"] = (df["clicks"] / df["impressions"].replace(0, 1)) > 1.0
        logger.info("[ADD] Added 'is_ctr_anomaly' flag (CTR > 100%)")

    # ── 5. Z-Score Anomaly Detection on Numeric Columns ──────────────────
    zscore_flags = []
    for col in numeric_cols:
        col_data = df[col]
        if col_data.nunique() < 3:
            continue
        col_mean = col_data.mean()
        col_std = col_data.std()
        if col_std == 0:
            continue
        z_col_name = f"z_{col}"
        df[z_col_name] = ((col_data - col_mean) / col_std).round(4)
        zscore_flags.append(z_col_name)

    if zscore_flags:
        # Global outlier flag: True if ANY z-score exceeds ±3
        df["is_outlier"] = df[zscore_flags].abs().gt(3).any(axis=1)
        outlier_count = int(df["is_outlier"].sum())
        logger.info(f"[ADD] Z-scores for {len(zscore_flags)} columns; {outlier_count} rows flagged as outliers (|Z| > 3)")

    # ── 6. Percentile Ranking ────────────────────────────────────────────
    rank_targets = [c for c in ["revenue", "clicks", "conversions", "roas"] if c in df.columns]
    for col in rank_targets:
        df[f"{col}_percentile"] = df[col].rank(pct=True).round(4) * 100
    if rank_targets:
        logger.info(f"[ADD] Percentile ranks for: {rank_targets}")

    # ── 7. Rolling / Moving Averages (requires sorted date) ──────────────
    if len(datetime_cols) > 0 and len(df) >= 3:
        dt_col = datetime_cols[0]
        df = df.sort_values(dt_col)
        rolling_targets = [c for c in ["revenue", "clicks", "conversions", "roas"] if c in df.columns]
        for col in rolling_targets:
            df[f"{col}_ma3"] = df[col].rolling(window=3, min_periods=1).mean().round(4)
            df[f"{col}_ma7"] = df[col].rolling(window=7, min_periods=1).mean().round(4)
        if rolling_targets:
            logger.info(f"[ADD] 3-day & 7-day moving averages for: {rolling_targets}")

    # ── 8. Cumulative Metrics ────────────────────────────────────────────
    if len(datetime_cols) > 0:
        cumul_targets = [c for c in ["revenue", "clicks", "conversions"] if c in df.columns]
        if spend_col:
            cumul_targets.append(spend_col)
        for col in cumul_targets:
            df[f"{col}_cumulative"] = df[col].cumsum()
        if cumul_targets:
            logger.info(f"[ADD] Cumulative sums for: {cumul_targets}")

    # ── 9. Spend Efficiency Tier (quartile-based) ────────────────────────
    if "roas" in df.columns and len(df) >= 4:
        try:
            df["spend_efficiency_tier"] = pd.qcut(
                df["roas"], q=4, labels=["Poor", "Below Avg", "Above Avg", "Excellent"],
                duplicates="drop"
            )
            logger.info("[ADD] Added 'spend_efficiency_tier' (quartile-based ROAS ranking)")
        except ValueError:
            # Not enough unique values for 4 bins
            logger.warning("[WARN] Could not create spend_efficiency_tier — not enough unique ROAS values")

    # ── 10. Composite Engagement Score (0–100 normalized) ────────────────
    engagement_components = []
    if "ctr" in df.columns:
        engagement_components.append("ctr")
    if "conversion_rate" in df.columns:
        engagement_components.append("conversion_rate")
    if "roas" in df.columns:
        engagement_components.append("roas")

    if len(engagement_components) >= 2:
        # Min-max normalize each component to 0–1, then average into a 0–100 score
        norm_parts = []
        for comp in engagement_components:
            col_min = df[comp].min()
            col_max = df[comp].max()
            if col_max - col_min == 0:
                norm_parts.append(pd.Series(0.5, index=df.index))
            else:
                norm_parts.append((df[comp] - col_min) / (col_max - col_min))
        df["engagement_score"] = (sum(norm_parts) / len(norm_parts) * 100).round(2)
        logger.info(f"[ADD] Added 'engagement_score' (composite of {engagement_components}, 0–100)")

    # ── 11. Campaign & Audience Parsing ──────────────────────────────────
    campaign_col = next((c for c in df.columns if c in ["campaign", "campaign_name", "ad_group"]), None)
    if campaign_col:
        df["campaign_prefix"] = df[campaign_col].str.split('_').str[0]
        df["is_retargeting"] = df[campaign_col].str.lower().str.contains("retargeting|rmkt|rtg", na=False)
        # Extract channel (Search, Display, Social, Email, Video)
        channel_pattern = r"(search|display|social|email|video)"
        df["channel"] = df[campaign_col].str.lower().str.extract(f"({channel_pattern})", expand=False)[0]
        df["channel"] = df["channel"].fillna("other").str.title()
        logger.info(f"[ADD] Parsed campaign dimensions (prefix, is_retargeting, channel) from '{campaign_col}'")

    # ── 12. Data Quality Score per Row ───────────────────────────────────
    # Measures how "complete" each row was BEFORE imputation (uses original null counts)
    total_cols = len(df.columns)
    # Count non-null values per row in the current (post-fill) frame as a proxy
    df["data_quality_score"] = ((df.notna().sum(axis=1) / total_cols) * 100).round(1)
    logger.info("[ADD] Added 'data_quality_score' — completeness % per row")

    # -----------------------------
    # [CLEAN] Keep ALL columns (schema-agnostic — no whitelist filtering)
    # -----------------------------
    existing_cols = list(df.columns)  # used in report calculations below

    # ─────────────────────────────
    # [STATS] CAPTURE AFTER STATS
    # ─────────────────────────────
    after_stats = capture_data_stats(df, label="after")
    print(f"[STATS] After: {after_stats['rows']} rows, {after_stats['columns']} cols, "
          f"{after_stats['duplicates']} duplicates, {after_stats['total_nulls']} nulls")

    # Build ETL report
    etl_report = {
        "before": before_stats,
        "after": after_stats,
        "validation": validation,
        "fill_strategy": fill_strategy,
        "fill_strategy_categorical": eff_categorical,
        "schema_agnostic": True,
        "detected_numeric_cols": numeric_cols,
        "detected_categorical_cols": categorical_cols,
        "changes": {
            "rows_removed": before_stats["rows"] - after_stats["rows"],
            "duplicates_removed": before_stats["duplicates"],
            "nulls_filled": before_stats["total_nulls"],
            "strategy_numeric": eff_numeric,
            "strategy_categorical": eff_categorical,
            "columns_before": before_stats["columns"],
            "columns_after": after_stats["columns"],
            "columns_added": max(0, after_stats["columns"] - before_stats["columns"] + (before_stats["columns"] - len(existing_cols))),
            "columns_dropped": before_stats["columns"] - len([c for c in before_stats["column_names"] if c.lower().strip() in [x.lower() for x in after_stats["column_names"]]]),
            "memory_saved_mb": round(before_stats["memory_mb"] - after_stats["memory_mb"], 3),
        }
    }

    # -----------------------------
    # [DB] Load to PostgreSQL (SAFE)
    # -----------------------------
    if engine is None:
        engine = get_engine()

    # Save RAW data (before cleaning) as a separate table
    raw_table_name = f"{table_name}_raw"
    cleaned_table_name = f"{table_name}_cleaned"

    # Archive existing tables before overwriting (data versioning)
    _archive_table_if_exists(engine, raw_table_name)
    _archive_table_if_exists(engine, cleaned_table_name)

    if input_lower.endswith(".xlsx") or input_lower.endswith(".xls"):
        raw_df = pd.read_excel(input_path, sheet_name=0)
    else:
        try:
            raw_df = pd.read_csv(input_path, encoding="utf-8")
        except UnicodeDecodeError:
            try:
                raw_df = pd.read_csv(input_path, encoding="latin1")
            except UnicodeDecodeError:
                raw_df = pd.read_csv(input_path, encoding="ISO-8859-1")
    raw_df.columns = raw_df.columns.str.strip().str.lower().str.replace(" ", "_", regex=False)

    raw_df.to_sql(
        name=raw_table_name,
        con=engine,
        if_exists="replace",
        index=False,
        method="multi",
        chunksize=1000,
    )
    print(f"   Saved {len(raw_df)} raw rows to '{raw_table_name}'")

    # Save CLEANED data
    cleaned_table_name = f"{table_name}_cleaned"
    print(f"[DB] Connecting to Postgres... (table: {cleaned_table_name})")

    ensure_table_exists(engine, df, cleaned_table_name)

    rows_before = len(df)

    df.to_sql(
        name=cleaned_table_name,
        con=engine,
        if_exists="replace",
        index=False,
        method="multi",
        chunksize=1000,
    )

    print(f"[OK] Loaded {rows_before} cleaned rows to PostgreSQL table '{cleaned_table_name}'")

    # Save ETL metadata
    try:
        filename = os.path.basename(input_path)
        save_etl_metadata(engine, cleaned_table_name, before_stats, after_stats, filename)
    except Exception as e:
        print(f"[WARN] Could not save ETL metadata: {e}")

    # -----------------------------
    # 💾 Save cleaned file
    # -----------------------------
    if output_path:
        df.to_csv(output_path, index=False)

    return df, etl_report