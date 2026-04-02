# =============================================================================
# main.py
# DS3294 — PDF Library Organiser for Scientific Articles
# Member 1 (Integration Lead) — Full pipeline entry point
#
# Wires all 4 modules together:
#   ingestion.py  -> scan folder, hash, build skeleton, call extractor + storage
#   extractor.py  -> 3-stage metadata extraction (embedded / text / filename)
#   storage.py    -> persist to SQLite or CSV
#   search.py     -> query the library, print dashboard
#
# Usage:
#   python main.py                          # ingest from default folder
#   python main.py --folder /path/to/pdfs  # custom folder
#   python main.py --backend flatfile       # CSV instead of SQLite
#   python main.py --reset-hashes          # treat all files as new
#   python main.py --dry-run               # scan only, don't save
#   python main.py --search "quantum"      # run a quick full-text search
#   python main.py --summary               # print dashboard only (no ingest)
# =============================================================================

import argparse
import logging
import os
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Logging — set up BEFORE importing the other modules so their loggers
# inherit the root config.
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Import pipeline modules
# ---------------------------------------------------------------------------
from storage   import set_backend, get_all, insert, DB_FILE, CSV_FILE
from ingestion import ingest_folder, HASH_STORE_FILE
from extractor import extract_metadata
from citation  import enrich_citations, build_citation_network, print_network_summary
from search    import (
    library_summary,
    full_text_search,
    filter_by_field,
    filter_by_year_range,
    get_clean_articles,
    get_flagged_articles,
    combined_search,
)


# =============================================================================
# DASHBOARD PRINTER
# =============================================================================

def print_dashboard():
    """
    Load the library and print a formatted summary dashboard.
    Called after ingestion or when --summary flag is used.
    """
    all_articles = get_all()

    if all_articles.empty:
        print("\n  No articles in library — run ingestion first.\n")
        return

    s = library_summary(df=all_articles)

    sep = "=" * 65

    print(f"\n{sep}")
    print("  LIBRARY DASHBOARD")
    print(sep)
    print(f"  Total articles        : {s['total']}")
    print(f"  Fully extracted       : {s['fully_extracted']}")
    print(f"  Partially extracted   : {s['flagged']}")
    print()

    if s.get("by_field"):
        print("  By research field:")
        for field, count in sorted(s["by_field"].items()):
            bar_fill = "█" * count
            print(f"    {field:<14} {count:>3}  {bar_fill}")
    print()

    if s.get("by_decade"):
        print("  By decade:")
        for decade, count in sorted(s["by_decade"].items()):
            print(f"    {decade:<10} {count:>3}")
    print()

    if s.get("flag_breakdown"):
        print("  Extraction flag breakdown:")
        for flag, count in sorted(s["flag_breakdown"].items(),
                                  key=lambda x: -x[1]):
            print(f"    {flag:<25} {count:>3} articles")
    print()

    if s.get("by_source"):
        print("  By extraction source:")
        for src, count in sorted(s["by_source"].items()):
            print(f"    {src:<20} {count:>3}")
    print(sep + "\n")


# =============================================================================
# QUICK SEARCH PRINTER
# =============================================================================

def print_search_results(query: str):
    """Run a full-text search and print the results to the terminal."""
    all_articles = get_all()
    if all_articles.empty:
        print("  No articles in library.")
        return

    t0     = time.perf_counter()
    result = full_text_search(query, df=all_articles)
    ms     = (time.perf_counter() - t0) * 1000

    print(f"\n  Full-text search: '{query}'  ->  {len(result)} result(s)  [{ms:.1f}ms]")
    print("  " + "-" * 63)

    if result.empty:
        print("  No matches found.")
    else:
        for _, row in result.iterrows():
            title   = str(row.get("title") or "(no title)")[:60]
            year    = str(row.get("year", "????"))
            field   = str(row.get("field") or "—")
            journal = str(row.get("journal") or "—")[:30]
            print(f"  [{year}] [{field:<12}] {title}")
            print(f"           {journal}")
            print()
    print("  " + "-" * 63 + "\n")


# =============================================================================
# ARGUMENT PARSER
# =============================================================================

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="main.py",
        description="DS3294 — PDF Library Organiser  |  Full pipeline runner",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    p.add_argument(
        "--folder",
        default=".",
        metavar="PATH",
        help="Folder containing PDF files (default: current directory)",
    )
    p.add_argument(
        "--backend",
        choices=["sqlite", "flatfile"],
        default="sqlite",
        help="Storage backend: sqlite (default) or flatfile (CSV)",
    )
    p.add_argument(
        "--reset-hashes",
        action="store_true",
        help="Clear seen_hashes.json so all files are treated as new",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Scan and classify files but do not extract or save anything",
    )
    p.add_argument(
        "--summary",
        action="store_true",
        help="Print library dashboard without running ingestion",
    )
    p.add_argument(
        "--search",
        metavar="QUERY",
        default=None,
        help="Run a full-text search after ingestion and print results",
    )
    p.add_argument(
        "--no-dashboard",
        action="store_true",
        help="Skip the dashboard after ingestion",
    )
    p.add_argument(
        "--citations",
        action="store_true",
        help="Fetch citation counts from Semantic Scholar and build citation network",
    )
    return p


# =============================================================================
# MAIN
# =============================================================================

def main():
    parser = build_parser()
    args   = parser.parse_args()

    # -- 1. Configure storage backend ----------------------------------------
    set_backend(args.backend)
    log.info(f"Storage backend : {args.backend}")

    # -- 2. Summary-only mode ------------------------------------------------
    if args.summary:
        print_dashboard()
        if args.search:
            print_search_results(args.search)
        sys.exit(0)

    # -- 3. Run ingestion -----------------------------------------------------
    log.info("Starting pipeline")
    log.info(f"  Folder       : {args.folder}")
    log.info(f"  Backend      : {args.backend}")
    log.info(f"  Dry run      : {args.dry_run}")
    log.info(f"  Reset hashes : {args.reset_hashes}")

    # Handle --reset-hashes: delete the hash store before ingesting
    if args.reset_hashes:
        # Clear hash store so all files are treated as new
        hash_file = Path(HASH_STORE_FILE)
        if hash_file.exists():
            os.remove(hash_file)
            log.info("Hash store cleared — all files will be treated as new")
        # Also wipe the DB so we don't accumulate duplicate records
        db_file = Path(DB_FILE)
        if db_file.exists():
            os.remove(db_file)
            log.info(f"Database cleared — '{DB_FILE}' deleted for fresh ingest")
        csv_file = Path(CSV_FILE)
        if csv_file.exists():
            os.remove(csv_file)
            log.info(f"Flatfile cleared — '{CSV_FILE}' deleted for fresh ingest")
        # Re-initialise the table after deletion
        from storage import create_table
        create_table()

    t0 = time.perf_counter()

    # ingest_folder returns a list of ingested article dicts and prints
    # its own detailed report internally — we measure time and count here
    articles = ingest_folder(
        folder_path=args.folder,
        dry_run=args.dry_run,
        extract_fn=extract_metadata,   # Member 2 — 3-stage metadata extraction
        store_fn=insert,               # Member 3 — saves to DB / CSV
    )

    elapsed = time.perf_counter() - t0
    log.info(f"Pipeline finished in {elapsed:.1f}s — "
             f"{len(articles)} article(s) ingested")

    # -- 4. Dashboard --------------------------------------------------------
    if not args.dry_run and not args.no_dashboard:
        print_dashboard()

    # -- 5. Optional quick search --------------------------------------------
    if args.search and not args.dry_run:
        print_search_results(args.search)

    # -- 6. Citation features (--citations flag) ------------------------------
    if args.citations and not args.dry_run:
        log.info("Starting citation enrichment...")
        all_articles = get_all()
        if all_articles.empty:
            log.warning("No articles in DB - run ingestion first")
        else:
            # Fetch citation counts from Semantic Scholar
            enriched = enrich_citations(all_articles)

            # Build citation network
            G = build_citation_network(enriched)
            if G is not None:
                print_network_summary(G)


if __name__ == "__main__":
    main()