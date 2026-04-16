"""
Multi-task Airflow DAG for the ReFyn Marketing ETL Pipeline.

Tasks:
  1. detect_files     — Scan uploads folder for new CSVs, push file list to XCom
  2. validate_schema  — Run schema validation on each file, flag warnings
  3. clean_and_load   — Clean data + load to PostgreSQL
  4. generate_report  — Log ETL metadata and summary
"""

from airflow import DAG
from airflow.operators.python import PythonOperator
from datetime import datetime, timedelta
import os
import sys
import json
import logging

logger = logging.getLogger(__name__)

# Allow container to see your project
sys.path.append("/opt/project")

DATA_FOLDER = "/opt/project/data/uploads"

default_args = {
    "owner": "nags",
    "depends_on_past": False,
    "start_date": datetime(2024, 1, 1),
    "retries": 2,
    "retry_delay": timedelta(minutes=1),
}


# ─────────────────────────────────────────────
# Task 1: Detect new files
# ─────────────────────────────────────────────
def detect_new_files(**context):
    """Scan the uploads folder for unprocessed CSV/Excel files."""
    logger.info("Scanning for new files in %s", DATA_FOLDER)

    if not os.path.exists(DATA_FOLDER):
        logger.error("Upload folder not found: %s", DATA_FOLDER)
        return []

    files = [
        f for f in os.listdir(DATA_FOLDER)
        if (f.endswith(".csv") or f.endswith(".xlsx") or f.endswith(".xls"))
        and "_cleaned" not in f
    ]

    logger.info("Found %d raw file(s): %s", len(files), files)

    if not files:
        logger.warning("No raw files found to process")
        return []

    # Push file list to XCom for downstream tasks
    context["ti"].xcom_push(key="file_list", value=files)
    return files


# ─────────────────────────────────────────────
# Task 2: Validate schema for each file
# ─────────────────────────────────────────────
def validate_schema(**context):
    """Run validation checks on each discovered file."""
    from etl.transform_marketing_data import validate_input
    import pandas as pd

    files = context["ti"].xcom_pull(task_ids="detect_files", key="file_list") or []
    if not files:
        logger.warning("No files to validate")
        return

    validation_results = {}

    for filename in files:
        file_path = os.path.join(DATA_FOLDER, filename)
        logger.info("Validating: %s", filename)

        try:
            if filename.lower().endswith((".xlsx", ".xls")):
                df = pd.read_excel(file_path, sheet_name=0)
            else:
                try:
                    df = pd.read_csv(file_path, encoding="utf-8")
                except UnicodeDecodeError:
                    df = pd.read_csv(file_path, encoding="latin1")

            result = validate_input(df, filename=filename)
            validation_results[filename] = {
                "valid": result["valid"],
                "warnings": result["warnings"],
                "errors": result["errors"],
                "summary": result.get("summary", {}),
            }

            if not result["valid"]:
                logger.error("FAILED validation for %s: %s", filename, result["errors"])
            elif result["warnings"]:
                for w in result["warnings"]:
                    logger.warning("  %s: %s", filename, w)
            else:
                logger.info("  %s: PASSED validation", filename)

        except Exception as e:
            logger.error("Could not validate %s: %s", filename, str(e))
            validation_results[filename] = {
                "valid": False,
                "errors": [str(e)],
                "warnings": [],
            }

    # Push valid files to XCom
    valid_files = [f for f, v in validation_results.items() if v["valid"]]
    context["ti"].xcom_push(key="valid_files", value=valid_files)
    context["ti"].xcom_push(key="validation_results", value=validation_results)

    logger.info("Validation complete: %d/%d files passed", len(valid_files), len(files))


# ─────────────────────────────────────────────
# Task 3: Clean and load each valid file
# ─────────────────────────────────────────────
def clean_and_load(**context):
    """Run ETL (clean + load to PostgreSQL) for each validated file."""
    from etl.transform_marketing_data import clean_marketing_data, sanitize_table_name

    valid_files = context["ti"].xcom_pull(task_ids="validate_schema", key="valid_files") or []

    if not valid_files:
        logger.warning("No valid files to process")
        return

    etl_results = {}

    for filename in valid_files:
        file_path = os.path.join(DATA_FOLDER, filename)
        table_name = sanitize_table_name(filename)
        ext = os.path.splitext(filename)[1].lower()
        cleaned_path = file_path.replace(ext, f"_cleaned{ext}")

        logger.info("Processing: %s -> table '%s'", filename, table_name)

        try:
            cleaned_df, etl_report = clean_marketing_data(
                file_path, cleaned_path, table_name=table_name
            )

            etl_results[filename] = {
                "table_name": f"{table_name}_cleaned",
                "rows_before": etl_report["before"]["rows"],
                "rows_after": etl_report["after"]["rows"],
                "status": "success",
            }

            logger.info(
                "Done: %s -> '%s_cleaned' | %d rows -> %d rows",
                filename, table_name,
                etl_report["before"]["rows"],
                etl_report["after"]["rows"],
            )

        except Exception as e:
            logger.error("ETL failed for %s: %s", filename, str(e))
            etl_results[filename] = {
                "table_name": f"{table_name}_cleaned",
                "status": "failed",
                "error": str(e),
            }

    context["ti"].xcom_push(key="etl_results", value=etl_results)


# ─────────────────────────────────────────────
# Task 4: Generate summary report
# ─────────────────────────────────────────────
def generate_report(**context):
    """Log a summary of the entire ETL run."""
    validation_results = context["ti"].xcom_pull(task_ids="validate_schema", key="validation_results") or {}
    etl_results = context["ti"].xcom_pull(task_ids="clean_and_load", key="etl_results") or {}

    logger.info("=" * 60)
    logger.info("ETL PIPELINE SUMMARY")
    logger.info("=" * 60)

    total_files = len(validation_results)
    valid_files = sum(1 for v in validation_results.values() if v["valid"])
    processed = sum(1 for v in etl_results.values() if v.get("status") == "success")
    failed = sum(1 for v in etl_results.values() if v.get("status") == "failed")

    logger.info("Files scanned:    %d", total_files)
    logger.info("Validation passed: %d", valid_files)
    logger.info("ETL successful:    %d", processed)
    logger.info("ETL failed:        %d", failed)

    for filename, result in etl_results.items():
        status_icon = "OK" if result.get("status") == "success" else "FAIL"
        if result.get("status") == "success":
            logger.info(
                "  [%s] %s -> %s (%d -> %d rows)",
                status_icon, filename, result["table_name"],
                result.get("rows_before", 0), result.get("rows_after", 0),
            )
        else:
            logger.info(
                "  [%s] %s: %s",
                status_icon, filename, result.get("error", "Unknown error"),
            )

    logger.info("=" * 60)


# ─────────────────────────────────────────────
# DAG Definition
# ─────────────────────────────────────────────
with DAG(
    dag_id="marketing_etl_pipeline",
    default_args=default_args,
    description="Multi-step ETL: detect -> validate -> clean -> report",
    schedule_interval=None,
    catchup=False,
    tags=["marketing", "etl"],
) as dag:

    t1 = PythonOperator(
        task_id="detect_files",
        python_callable=detect_new_files,
    )

    t2 = PythonOperator(
        task_id="validate_schema",
        python_callable=validate_schema,
    )

    t3 = PythonOperator(
        task_id="clean_and_load",
        python_callable=clean_and_load,
    )

    t4 = PythonOperator(
        task_id="generate_report",
        python_callable=generate_report,
    )

    t1 >> t2 >> t3 >> t4