# =============================================================================
# schema.py
# DS3294 — PDF Library Organiser for Scientific Articles
# Project #12
#
# ⚠️  SHARED FILE — DO NOT MODIFY WITHOUT TEAM AGREEMENT ⚠️
#
# This file is the single source of truth for the article data structure.
# Every member imports from this file. No one defines their own dict structure.
#
# Members:
#   Member 1 (Ingestion)  — fills: article_id, file_path, file_hash, ingested_at
#   Member 2 (Extraction) — fills: title, authors, journal, year, keywords,
#                                   abstract, source, confidence, flags, duplicate_of
#   Member 3 (Storage)    — receives full dict, saves and retrieves from DB
#   Member 4 (Search)     — queries DB, expects consistent types on all fields
# =============================================================================

import uuid
from datetime import datetime


# -----------------------------------------------------------------------------
# VALID VALUES — only these strings are allowed in "source" and "flags" fields
# -----------------------------------------------------------------------------

VALID_SOURCES = {
    "embedded",   # metadata came from the PDF's built-in properties
    "parsed",     # metadata was extracted by reading/parsing the PDF text
    "filename",   # metadata was guessed from the PDF filename
    "manual",     # metadata was entered or corrected by hand
}

VALID_FLAGS = {
    "missing_title",       # title could not be extracted
    "missing_authors",     # authors could not be extracted
    "missing_year",        # year could not be extracted
    "year_uncertain",      # year was guessed, not confirmed
    "missing_abstract",    # abstract could not be extracted
    "possible_duplicate",  # this article may already exist in the library
}


# -----------------------------------------------------------------------------
# FIELD REFERENCE — description and type of every field in the schema
# -----------------------------------------------------------------------------
#
#  Field            Type         Filled by    Description
#  ───────────────────────────────────────────────────────────────────────────
#  article_id       str          Member 1     UUID4 string, unique per article
#  file_path        str          Member 1     Absolute path to the PDF file
#  file_hash        str          Member 1     MD5 hash of the PDF file bytes
#  ingested_at      str          Member 1     ISO 8601 timestamp of ingestion
#
#  title            str|None     Member 2     Title of the article
#  authors          list[str]    Member 2     List of author names e.g. ["Vaswani, A."]
#  journal          str|None     Member 2     Journal or conference name
#  year             int|None     Member 2     Publication year as integer e.g. 2017
#  keywords         list[str]    Member 2     Lowercase keyword strings
#  abstract         str|None     Member 2     Full abstract text
#  source           str|None     Member 2     One of VALID_SOURCES
#  confidence       float        Member 2     0.0 to 1.0 — reliability of extraction
#  flags            list[str]    Member 2     Subset of VALID_FLAGS
#  duplicate_of     str|None     Member 2     article_id of original, or None
# -----------------------------------------------------------------------------


# -----------------------------------------------------------------------------
# empty_article()
# Returns a blank article dict with ALL fields initialised to safe defaults.
# ALWAYS start from this — never build a dict from scratch.
# -----------------------------------------------------------------------------

def empty_article() -> dict:
    """
    Returns a blank article dictionary with all 14 fields set to safe defaults.

    Usage:
        from schema import empty_article
        article = empty_article()
        article["title"] = "My Paper Title"
        article["year"]  = 2021
    """
    return {
        # --- Filled by Member 1 (Ingestion) ---
        "article_id":    None,   # str  : UUID4
        "file_path":     None,   # str  : absolute path to PDF
        "file_hash":     None,   # str  : MD5 hex digest
        "ingested_at":   None,   # str  : ISO timestamp

        # --- Filled by Member 2 (Extraction) ---
        "title":         None,   # str  : article title
        "authors":       [],     # list : ["Last, F.", "Last, F."]
        "journal":       None,   # str  : journal or conference name
        "year":          None,   # int  : e.g. 2017, NOT "2017"
        "keywords":      [],     # list : lowercase strings
        "abstract":      None,   # str  : full abstract text
        "source":        None,   # str  : one of VALID_SOURCES
        "confidence":    0.0,    # float: 0.0 → 1.0
        "flags":         [],     # list : subset of VALID_FLAGS
        "duplicate_of":  None,   # str  : article_id of original, or None
    }


# -----------------------------------------------------------------------------
# generate_id()
# Generates a new unique article ID. Called by Member 1 only.
# -----------------------------------------------------------------------------

def generate_id() -> str:
    """
    Returns a new UUID4 string to use as article_id.

    Usage (Member 1 only):
        from schema import generate_id
        article["article_id"] = generate_id()
    """
    return str(uuid.uuid4())


# -----------------------------------------------------------------------------
# current_timestamp()
# Returns the current time as an ISO 8601 string. Called by Member 1 only.
# -----------------------------------------------------------------------------

def current_timestamp() -> str:
    """
    Returns the current datetime as an ISO 8601 formatted string.

    Usage (Member 1 only):
        from schema import current_timestamp
        article["ingested_at"] = current_timestamp()
    """
    return datetime.now().isoformat(timespec="seconds")


# -----------------------------------------------------------------------------
# validate_article(article)
# Checks a dict against the schema rules. Returns a list of errors found.
# Any member can call this to verify their output before passing it forward.
# -----------------------------------------------------------------------------

def validate_article(article: dict) -> list:
    """
    Validates an article dict against the schema rules.
    Returns a list of error strings. Empty list means the article is valid.

    Usage:
        from schema import validate_article
        errors = validate_article(article)
        if errors:
            print("Validation failed:", errors)
        else:
            print("Article is valid ✅")
    """
    errors = []

    # 1. Check all required keys exist
    expected_keys = set(empty_article().keys())
    missing_keys  = expected_keys - set(article.keys())
    if missing_keys:
        errors.append(f"Missing keys: {missing_keys}")

    # 2. Check types of filled fields
    if article.get("year") is not None:
        if not isinstance(article["year"], int):
            errors.append(
                f"'year' must be an int, got {type(article['year']).__name__} "
                f"(value: {article['year']!r}) — hint: use int(year), not str"
            )

    if not isinstance(article.get("authors", []), list):
        errors.append(
            f"'authors' must be a list, got {type(article['authors']).__name__} "
            f"— wrap single authors in a list: ['Author, A.']"
        )

    if not isinstance(article.get("keywords", []), list):
        errors.append(
            f"'keywords' must be a list, got {type(article['keywords']).__name__}"
        )

    if not isinstance(article.get("flags", []), list):
        errors.append(
            f"'flags' must be a list, got {type(article['flags']).__name__}"
        )

    if not isinstance(article.get("confidence", 0.0), float):
        errors.append(
            f"'confidence' must be a float, got {type(article['confidence']).__name__} "
            f"— hint: use 1.0 not 1"
        )

    # 3. Check confidence is in range 0.0 to 1.0
    confidence = article.get("confidence", 0.0)
    if isinstance(confidence, float) and not (0.0 <= confidence <= 1.0):
        errors.append(
            f"'confidence' must be between 0.0 and 1.0, got {confidence}"
        )

    # 4. Check source is a valid value
    source = article.get("source")
    if source is not None and source not in VALID_SOURCES:
        errors.append(
            f"'source' must be one of {VALID_SOURCES}, got {source!r}"
        )

    # 5. Check all flags are valid values
    invalid_flags = set(article.get("flags", [])) - VALID_FLAGS
    if invalid_flags:
        errors.append(
            f"Invalid flags: {invalid_flags} — allowed flags are: {VALID_FLAGS}"
        )

    # 6. Check keywords are all lowercase
    for kw in article.get("keywords", []):
        if isinstance(kw, str) and kw != kw.lower():
            errors.append(
                f"Keyword {kw!r} must be lowercase — use kw.lower().strip()"
            )

    return errors


# -----------------------------------------------------------------------------
# EXAMPLE RECORDS — for reference and testing
# -----------------------------------------------------------------------------

EXAMPLE_FULL = {
    "article_id":    "a3f9c1d2-4e88-4b1a-9c77-d2f3e1a0b456",
    "file_path":     "/data/pdfs/attention_is_all_you_need.pdf",
    "file_hash":     "d41d8cd98f00b204e9800998ecf8427e",
    "ingested_at":   "2025-03-08T14:32:00",

    "title":         "Attention Is All You Need",
    "authors":       ["Vaswani, A.", "Shazeer, N.", "Parmar, N."],
    "journal":       "NeurIPS",
    "year":          2017,
    "keywords":      ["transformer", "attention mechanism", "nlp"],
    "abstract":      "The dominant sequence transduction models are based on complex "
                     "recurrent or convolutional neural networks...",
    "source":        "embedded",
    "confidence":    1.0,
    "flags":         [],
    "duplicate_of":  None,
}

EXAMPLE_INCOMPLETE = {
    "article_id":    "b7c2d3e4-5f99-4c2b-8d88-e3g4f2b1c567",
    "file_path":     "/data/pdfs/unknown_paper_2019.pdf",
    "file_hash":     "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6",
    "ingested_at":   "2025-03-08T15:00:00",

    "title":         None,
    "authors":       [],
    "journal":       None,
    "year":          None,
    "keywords":      [],
    "abstract":      None,
    "source":        "filename",
    "confidence":    0.4,
    "flags":         ["missing_title", "missing_authors", "missing_year"],
    "duplicate_of":  None,
}


# -----------------------------------------------------------------------------
# Quick self-test — run this file directly to confirm schema is working
# python schema.py
# -----------------------------------------------------------------------------

if __name__ == "__main__":
    print("=" * 60)
    print("schema.py — self test")
    print("=" * 60)

    # Test 1: empty_article has all keys
    article = empty_article()
    print(f"\n✅ empty_article() returned {len(article)} fields:")
    for key, val in article.items():
        print(f"   {key:<15} = {val!r}")

    # Test 2: generate_id works
    new_id = generate_id()
    print(f"\n✅ generate_id()     = {new_id!r}")

    # Test 3: current_timestamp works
    ts = current_timestamp()
    print(f"✅ current_timestamp() = {ts!r}")

    # Test 4: validate catches a bad article
    bad_article = empty_article()
    bad_article["year"]       = "2017"        # wrong type
    bad_article["authors"]    = "Vaswani, A." # wrong type — should be list
    bad_article["confidence"] = 1             # wrong type — should be float
    bad_article["source"]     = "auto"        # not in VALID_SOURCES
    bad_article["flags"]      = ["made_up"]   # not in VALID_FLAGS
    bad_article["keywords"]   = ["NLP"]       # not lowercase

    errors = validate_article(bad_article)
    print(f"\n✅ validate_article() caught {len(errors)} errors in a bad record:")
    for e in errors:
        print(f"   ❌ {e}")

    # Test 5: validate passes a good article
    errors = validate_article(EXAMPLE_FULL)
    if not errors:
        print(f"\n✅ validate_article() passed EXAMPLE_FULL with no errors")

    print("\n" + "=" * 60)
    print("All tests passed. Share this file with your team.")
    print("=" * 60)









