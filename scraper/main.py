import asyncio
import json
import os
from pathlib import Path

from scraper.aggregator import Aggregator, RunSummary
from scraper.utils.logger import get_logger
from scraper.repository.supabase_repository import SupabaseRepository

logger = get_logger(__name__)


def print_run_summary(summary: RunSummary) -> None:
    """
    Prints a detailed summary of the scraper run, including all failures
    and parsing warnings (fields that returned None).

    Args:
        summary: The RunSummary from the aggregator run
    """
    print("\n" + "=" * 60)
    print("                    SCRAPER RUN SUMMARY")
    print("=" * 60)
    print(f"Total hospitals: {summary.total}")
    print(f"Successful: {summary.successful} ({summary.success_rate:.1f}%)")
    print(f"Failed: {summary.failed} ({100 - summary.success_rate:.1f}%)")
    print(f"Duration: {summary.duration_seconds:.1f} seconds")

    if summary.failures:
        print("\nFAILED HOSPITALS:")
        print("-" * 60)

        for failure in summary.failures:
            print(f"\nHospital ID: {failure.hospital_id}")
            print(f"Action: {failure.action}")
            print(f"URL: {failure.url}")
            print(f"Error: {failure.error}")

    if summary.parsing_warnings:
        print("\n" + "=" * 60)
        print("                   PARSING WARNINGS")
        print("                 (Fields returned None)")
        print("=" * 60)
        print(f"Hospitals with null fields: {summary.hospitals_with_null_fields}")
        print(f"Total null fields: {len(summary.parsing_warnings)}")
        print("-" * 60)

        # Group warnings by hospital for cleaner output
        warnings_by_hospital = {}
        for warning in summary.parsing_warnings:
            if warning.hospital_id not in warnings_by_hospital:
                warnings_by_hospital[warning.hospital_id] = {
                    "action": warning.action,
                    "url": warning.url,
                    "fields": []
                }
            warnings_by_hospital[warning.hospital_id]["fields"].append(warning.field)

        for hospital_id, info in sorted(warnings_by_hospital.items()):
            print(f"\nHospital ID: {hospital_id}")
            print(f"Action: {info['action']}")
            print(f"URL: {info['url']}")
            print(f"Null fields: {', '.join(info['fields'])}")

    print("=" * 60 + "\n")


def write_github_summary(summary: RunSummary) -> None:
    """
    In GitHub Actions, surface problems where they're seen: a table on the run's summary page
    and a warning annotation per failed hospital. Outside Actions this does nothing.
    """
    if not os.environ.get("GITHUB_ACTIONS"):
        return

    for failure in summary.failures:
        first_line = (failure.error or "unknown error").splitlines()[0][:200]
        print(f"::warning title=Scrape failed: {failure.hospital_id}::{first_line} ({failure.url})")

    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    lines = [
        "## Scraper run",
        f"{summary.successful}/{summary.total} hospitals scraped ({summary.success_rate:.0f}%) "
        f"in {summary.duration_seconds:.0f}s.",
    ]
    if summary.failures:
        lines += ["", "### Failed (saved as offline)", "| Hospital | Action | Error |", "|---|---|---|"]
        for f in summary.failures:
            err = (f.error or "").splitlines()[0][:160].replace("|", "\\|") if f.error else ""
            lines.append(f"| `{f.hospital_id}` | {f.action} | {err} |")
    if summary.parsing_warnings:
        lines += ["", "### Scraped, but configured fields came back empty", "| Hospital | Fields |", "|---|---|"]
        by_hospital = {}
        for w in summary.parsing_warnings:
            by_hospital.setdefault(w.hospital_id, []).append(w.field)
        for hospital_id, fields in sorted(by_hospital.items()):
            lines.append(f"| `{hospital_id}` | {', '.join(fields)} |")
    with open(path, "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


async def main():
    """
    Main entry point for running the scraper. It:
      1. Saves target data to a JSON file.
      2. Reads the JSON data containing scraping targets.
      3. Instantiates the aggregator and processes each target concurrently.
      4. Prints a detailed run summary with any failures.
    """
    logger.info("Starting the scraper...")

    # Step 1: Save the target data to a JSON file for subsequent use.
    save_target_data_to_json()

    # Step 2: Load the saved scraping targets from the JSON file.
    scraping_targets_path = Path(__file__).parent.parent / "data" / "scraping_targets_data.json"
    try:
        with open(scraping_targets_path, "r", encoding="utf-8") as f:
            scraping_targets = json.load(f)
        logger.debug(f"Loaded {len(scraping_targets)} scraping targets from {scraping_targets_path}.")
    except FileNotFoundError:
        # Log an error if the file is missing and exit early.
        logger.error(f"scraping_targets_data.json not found at {scraping_targets_path}")
        return
    except json.JSONDecodeError as e:
        # Log an error if there's an issue with JSON decoding.
        logger.error(f"JSON decoding error for {scraping_targets_path}: {e}")
        return

    # Step 3: Instantiate the aggregator with the loaded targets and run the process.
    aggregator = Aggregator(scraping_targets)
    logger.info("Running the aggregator...")
    summary = await aggregator.run()

    # Step 4: Print the run summary with failure details.
    print_run_summary(summary)
    write_github_summary(summary)

    logger.info("Scraper run complete.")


def save_target_data_to_json():
    """
    Retrieves target data from the Supabase repository and saves it to a local JSON file.

    Raises:
        Exception: If an unexpected error occurs during the data retrieval or file operations.
    """
    logger.info("Starting the process to save target data to JSON...")

    try:
        # Create a SupabaseRepository instance to fetch the target data.
        repo = SupabaseRepository()
        target_data = repo.get_target_data()

        if not target_data:
            # Log a warning if no data was retrieved.
            logger.warning("No target data retrieved from the repository.")
            return

        # Create the data directory if it does not already exist.
        data_dir = os.path.join(os.path.dirname(__file__), '../data')
        os.makedirs(data_dir, exist_ok=True)

        # Define the output path for the JSON file.
        output_path = os.path.join(data_dir, 'scraping_targets_data.json')

        # Write the target data to the JSON file.
        with open(output_path, 'w', encoding='utf-8') as json_file:
            json.dump(target_data, json_file, indent=4, ensure_ascii=False)
        logger.info(f"Data successfully saved to {output_path}")

    except Exception as e:
        # Log any exception that occurs, including stack trace.
        logger.error(f"Error occurred while saving target data to JSON: {e}", exc_info=True)


if __name__ == "__main__":
    # Only run the script if called directly (not imported).
    asyncio.run(main())
