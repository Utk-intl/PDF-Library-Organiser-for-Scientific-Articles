# =============================================================================
# storage.py
# DS3294 — PDF Library Organiser for Scientific Articles
# Member 3 — Database & Storage
#
# Responsibilities:
#   - Save article dicts to persistent storage (SQLite or flat CSV)
#   - Retrieve one or all articles back as Python dict / DataFrame
#   - Update specific fields of an existing record
#   - Provide a single unified interface so Member 4's search.py
#     and Member 1's ingestion.py never need to know which backend
#     is active — just call insert(), get(), get_all(), update()
#
# Two backends, switched with one call:
#   set_backend("sqlite")    ← default — fast, queryable with sqlite3 CLI
#   set_backend("flatfile")  ← CSV — human-readable, easy to open in Excel
#
# Usage:
#   from storage import insert, get, get_all, update, set_backend
# =============================================================================

import sqlite3
import pandas as pd
from schema import validate_article

DB_FILE  = "articles.db"
CSV_FILE = "articles.csv"
BACKEND  = "sqlite"   # change to "flatfile" to switch

# All columns that are allowed to be updated via update()
# (article_id, file_path, file_hash, ingested_at are immutable)
VALID_COLUMNS = {
    "title", "authors", "journal", "year", "keywords",
    "abstract", "source", "confidence", "flags",
    "duplicate_of", "field", "doi", "citation_count"
}

# =============================================================================
# HELPERS — serialise list ↔ pipe-separated string
# =============================================================================

def _serialize(val) -> str:
    """Convert a list to a pipe-separated string, or return val as-is."""
    if val is None:
        return ""
    if isinstance(val, list):
        return "|".join(str(x) for x in val)
    return str(val)


def _deserialize(val) -> list:
    """Convert a pipe-separated string back to a list, empty list if blank."""
    if not val:
        return []
    return val.split("|")


def _article_to_row(article: dict) -> tuple:
    """
    Convert an article dict to a flat 15-value tuple for SQLite INSERT.
    Order must exactly match the CREATE TABLE column order.
    """
    return (
        article.get("article_id"),
        article.get("file_path"),
        article.get("file_hash"),
        article.get("ingested_at"),
        article.get("title"),
        _serialize(article.get("authors", [])),
        article.get("journal"),
        article.get("year"),
        _serialize(article.get("keywords", [])),
        article.get("abstract"),
        article.get("source"),
        article.get("confidence", 0.0),
        _serialize(article.get("flags", [])),
        article.get("duplicate_of"),
        article.get("field"),           # research domain — biology/chemistry/etc.
        article.get("doi"),              # DOI if available
        article.get("citation_count"),   # fetched from Semantic Scholar
    )


def _row_to_article(row: dict) -> dict:
    """
    Convert a raw SQLite row dict back to a properly typed article dict.
    Deserialises pipe-separated list fields and casts numeric types.
    """
    row["authors"]      = _deserialize(row.get("authors"))
    row["keywords"]     = _deserialize(row.get("keywords"))
    row["flags"]        = _deserialize(row.get("flags"))
    row["year"]         = int(row["year"]) if row.get("year") is not None else None
    row["confidence"]   = float(row["confidence"]) if row.get("confidence") is not None else 0.0
    row["duplicate_of"] = row.get("duplicate_of") or None
    return row


# =============================================================================
# INITIALISATION
# =============================================================================

def set_backend(name: str):
    """
    Switch between 'sqlite' and 'flatfile'.
    Call this before any insert/get operations if you want non-default backend.
    """
    global BACKEND
    if name not in ("sqlite", "flatfile"):
        raise ValueError(f"Unknown backend: '{name}'. Use 'sqlite' or 'flatfile'.")
    BACKEND = name
    if BACKEND == "sqlite":
        create_table()


def create_table():
    """
    Create the articles table if it doesn't exist yet.
    Idempotent — safe to call multiple times.
    Adds indexes on authors, year, title, and field for fast filtering.
    """
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS articles (
            article_id   TEXT PRIMARY KEY,
            file_path    TEXT,
            file_hash    TEXT,
            ingested_at  TEXT,
            title        TEXT,
            authors      TEXT,
            journal      TEXT,
            year         INTEGER,
            keywords     TEXT,
            abstract     TEXT,
            source       TEXT,
            confidence   REAL,
            flags        TEXT,
            duplicate_of TEXT,
            field        TEXT,
            doi          TEXT,
            citation_count INTEGER
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_author ON articles(authors)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_year   ON articles(year)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_title  ON articles(title)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_field  ON articles(field)")
    conn.commit()
    conn.close()


# =============================================================================
# CRUD
# =============================================================================

def insert(article: dict):
    """
    Save one article dict to storage.
    If an article with the same article_id already exists, it is replaced.

    Strips filename_info (Member 1's hint dict) before saving —
    it is not part of the schema and should not be persisted.

    Args:
        article: fully or partially populated article dict
    """
    # Strip ingestion-time hints — not schema fields
    article = {k: v for k, v in article.items() if k != "filename_info"}

    errors = validate_article(article)
    if errors:
        import logging
        logging.getLogger(__name__).warning(
            f"[storage] Validation warnings on insert: {errors}"
        )

    if BACKEND == "sqlite":
        conn = sqlite3.connect(DB_FILE)
        conn.execute(
            "INSERT OR REPLACE INTO articles VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            _article_to_row(article)
        )
        conn.commit()
        conn.close()

    else:  # flatfile CSV
        article_flat = article.copy()
        for field in ["authors", "keywords", "flags"]:
            article_flat[field] = _serialize(article_flat.get(field, []))
        df_new = pd.DataFrame([article_flat])
        try:
            existing = pd.read_csv(CSV_FILE)
            # Remove old record with same article_id (upsert behaviour)
            existing = existing[existing["article_id"] != article["article_id"]]
            df_new = pd.concat([existing, df_new], ignore_index=True)
        except FileNotFoundError:
            pass
        df_new.to_csv(CSV_FILE, index=False)


def get(article_id: str):
    """
    Fetch one article by its article_id.

    Returns:
        dict with all schema fields, or None if not found.
    """
    if BACKEND == "sqlite":
        conn = sqlite3.connect(DB_FILE)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM articles WHERE article_id = ?", (article_id,)
        ).fetchone()
        conn.close()
        if row is None:
            return None
        return _row_to_article(dict(row))

    else:
        try:
            df = pd.read_csv(CSV_FILE)
            result = df[df["article_id"] == article_id]
            if result.empty:
                return None
            return _row_to_article(result.iloc[0].to_dict())
        except FileNotFoundError:
            return None


def get_all() -> pd.DataFrame:
    """
    Return all articles as a pandas DataFrame.
    List columns (authors, keywords, flags) are deserialised back to Python lists.

    Returns:
        pd.DataFrame — empty DataFrame if no records exist.
    """
    if BACKEND == "sqlite":
        conn = sqlite3.connect(DB_FILE)
        df = pd.read_sql_query("SELECT * FROM articles", conn)
        conn.close()
    else:
        try:
            df = pd.read_csv(CSV_FILE)
        except FileNotFoundError:
            return pd.DataFrame()

    # Deserialise list columns so Member 4 can filter them properly
    for col in ["authors", "keywords", "flags"]:
        if col in df.columns:
            df[col] = df[col].apply(
                lambda x: x.split("|") if isinstance(x, str) and x else []
            )

    return df


def update(article_id: str, fields: dict):
    """
    Update specific fields of an existing record.
    Only columns in VALID_COLUMNS are allowed — others raise ValueError.

    Args:
        article_id: UUID of the record to update
        fields:     dict of {column_name: new_value}

    Example:
        update("abc-123", {"authors": ["Smith, J.", "Doe, A."], "year": 2024})
    """
    invalid = set(fields.keys()) - VALID_COLUMNS
    if invalid:
        raise ValueError(f"Invalid column(s) for update: {invalid}. "
                         f"Allowed: {VALID_COLUMNS}")

    if BACKEND == "sqlite":
        conn = sqlite3.connect(DB_FILE)
        for key, value in fields.items():
            serialised = _serialize(value) if isinstance(value, list) else value
            conn.execute(
                f"UPDATE articles SET {key}=? WHERE article_id=?",
                (serialised, article_id)
            )
        conn.commit()
        conn.close()

    else:
        try:
            df = pd.read_csv(CSV_FILE)
            for key, value in fields.items():
                serialised = _serialize(value) if isinstance(value, list) else value
                df.loc[df["article_id"] == article_id, key] = serialised
            df.to_csv(CSV_FILE, index=False)
        except FileNotFoundError:
            pass


# =============================================================================
# AUTO-INIT — create table when module is imported (SQLite backend only)
# =============================================================================
create_table()