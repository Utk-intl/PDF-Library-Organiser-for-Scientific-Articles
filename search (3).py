import re, ast, time, logging
import pandas as pd
from collections import Counter
from storage import get_all, set_backend

log = logging.getLogger(__name__)


# ── Helpers ───────────────────────────────────────────────────────────────────
def _load(df=None): return df.copy() if df is not None else get_all()
def _safe_str(val): return " ".join(str(x) for x in val).lower() if isinstance(val, list) else ("" if val is None else str(val).lower())
def _match(val, query): return query.lower() in _safe_str(val)
def _results(df, mask): return df[mask].reset_index(drop=True)

def _safe_parse_list(value):
    if value is None: return []
    if isinstance(value, list): return value
    s = str(value)
    if "|" in s: return [x.strip() for x in s.split("|") if x.strip()]
    try:
        parsed = ast.literal_eval(s); return parsed if isinstance(parsed, list) else []
    except Exception: return []

def _normalise_df(df):
    df = df.copy()
    for col in ["authors", "keywords", "flags"]:
        if col in df.columns: df[col] = df[col].apply(_safe_parse_list)
    return df

def _simple_search(field, query, df):
    if not query or not str(query).strip(): return pd.DataFrame()
    df = _load(df)
    if df.empty: return df
    result = _results(df, df[field].apply(lambda v: _match(v, query)))
    log.info(f"search/filter '{query}' on '{field}' -> {len(result)} result(s)")
    return result


# ── Search functions ──────────────────────────────────────────────────────────
def search_by_author(query, df=None):  return _simple_search("authors", query, df)
def search_by_title(query, df=None):   return _simple_search("title",   query, df)
def search_by_keyword(query, df=None): return _simple_search("keywords", query, df)
def filter_by_journal(query, df=None): return _simple_search("journal", query, df)

def filter_by_field(field, df=None):
    if not field or not str(field).strip(): return pd.DataFrame()
    df = _load(df)
    if df.empty or "field" not in df.columns:
        log.warning("'field' column not found"); return pd.DataFrame()
    result = _results(df, df["field"].apply(lambda v: _match(v, field)))
    log.info(f"filter_by_field('{field}') -> {len(result)} result(s)")
    return result

def filter_by_year(year, df=None):
    if not isinstance(year, int): raise TypeError("year must be an integer")
    df = _load(df)
    if df.empty: return df
    result = _results(df, df["year"].apply(lambda v: pd.notna(v) and int(v) == year))
    log.info(f"filter_by_year({year}) -> {len(result)} result(s)")
    return result

def filter_by_year_range(start, end, df=None):
    if not (isinstance(start, int) and isinstance(end, int)): raise TypeError("start and end must be integers")
    if start > end: raise ValueError("start must be <= end")
    df = _load(df)
    if df.empty: return df
    result = _results(df, df["year"].apply(lambda v: pd.notna(v) and start <= int(v) <= end))
    log.info(f"filter_by_year_range({start}, {end}) -> {len(result)} result(s)")
    return result

def filter_by_confidence(min_conf, df=None):
    df = _load(df)
    if df.empty: return df
    result = _results(df, df["confidence"].apply(lambda v: pd.notna(v) and float(v) >= float(min_conf)))
    log.info(f"filter_by_confidence({min_conf}) -> {len(result)} result(s)")
    return result

def filter_by_flags(flag, df=None):
    if not flag or not str(flag).strip(): return pd.DataFrame()
    df = _normalise_df(_load(df))
    if df.empty: return df
    fl = flag.lower().strip()
    result = _results(df, df["flags"].apply(lambda f: isinstance(f, list) and any(fl in x.lower() for x in f)))
    log.info(f"filter_by_flags('{flag}') -> {len(result)} result(s)")
    return result

def get_flagged_articles(df=None):
    df = _normalise_df(_load(df))
    if df.empty: return df
    result = _results(df, df["flags"].apply(lambda f: isinstance(f, list) and len(f) > 0))
    log.info(f"get_flagged_articles() -> {len(result)} result(s)")
    return result

def get_clean_articles(df=None):
    df = _normalise_df(_load(df))
    if df.empty: return df
    result = _results(df, df["flags"].apply(lambda f: not f or f == []))
    log.info(f"get_clean_articles() -> {len(result)} result(s)")
    return result

def full_text_search(query, df=None):
    if not query or not str(query).strip(): return pd.DataFrame()
    df = _normalise_df(_load(df))
    if df.empty: return df
    terms = [t.lower() for t in re.split(r"\s+", query.strip()) if t]

    def _score(row):
        title = _safe_str(row.get("title"))
        rest  = " ".join(_safe_str(row.get(f)) for f in ("abstract","keywords","authors"))
        score = 0
        for t in terms:
            in_t, in_r = t in title, t in rest
            if not in_t and not in_r: return 0
            score += (2 if in_t else 0) + (1 if in_r else 0)
        return score

    scores = df.apply(_score, axis=1)
    result = df[scores > 0].copy()
    result["_relevance"] = scores[scores > 0]
    result = result.sort_values("_relevance", ascending=False).drop(columns=["_relevance"]).reset_index(drop=True)
    log.info(f"full_text_search('{query}') -> {len(result)} result(s)")
    return result

def combined_search(author=None, title=None, keyword=None, year=None,
                    year_from=None, year_to=None, field=None, journal=None,
                    min_conf=None, flag=None, df=None):
    cur = _load(df)
    if cur.empty: return cur
    if author   is not None: cur = search_by_author(author,         df=cur)
    if title    is not None: cur = search_by_title(title,           df=cur)
    if keyword  is not None: cur = search_by_keyword(keyword,       df=cur)
    if year     is not None: cur = filter_by_year(year,             df=cur)
    if year_from is not None and year_to is not None:
        cur = filter_by_year_range(year_from, year_to,              df=cur)
    if field    is not None: cur = filter_by_field(field,           df=cur)
    if journal  is not None: cur = filter_by_journal(journal,       df=cur)
    if min_conf is not None: cur = filter_by_confidence(min_conf,   df=cur)
    if flag     is not None: cur = filter_by_flags(flag,            df=cur)
    log.info(f"combined_search() -> {len(cur)} result(s)")
    return cur

def library_summary(df=None):
    df = _normalise_df(_load(df))
    if df.empty: return {"total": 0}
    by_field  = df["field"].value_counts().to_dict()  if "field"  in df.columns else {}
    by_source = df["source"].value_counts().to_dict() if "source" in df.columns else {}
    def _decade(y):
        try: return f"{int(int(y) / 10) * 10}s"
        except Exception: return "unknown"
    by_decade       = df["year"].apply(_decade).value_counts().sort_index().to_dict()
    all_flags       = [f for flags in df["flags"] if isinstance(flags, list) for f in flags]
    fully_extracted = int(df["flags"].apply(lambda v: not v).sum())
    return dict(total=len(df), by_field=by_field, by_source=by_source, by_decade=by_decade,
                fully_extracted=fully_extracted, flagged=len(df)-fully_extracted,
                flag_breakdown=dict(Counter(all_flags)))


# ── Smoke-test ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(message)s")
    print("\n" + "=" * 65)
    print("  PDF Library Organiser — Search Module Test")
    print("=" * 65)

    all_articles = get_all()
    print(f"\n  Library size: {len(all_articles)} articles\n")
    if all_articles.empty: print("  No articles found — run ingestion.py first"); exit(0)

    s = library_summary(df=all_articles)
    print("  Library Summary")
    for k, v in [("Total articles", s['total']), ("Fully extracted", s['fully_extracted']),
                 ("Partially extracted", s['flagged']), ("By field", s['by_field']),
                 ("By decade", s['by_decade']), ("Flag breakdown", s['flag_breakdown'])]:
        print(f"     {k:<22}: {v}")

    tests = [
        ("search_by_author",     lambda: search_by_author("berger",             df=all_articles)),
        ("search_by_title",      lambda: search_by_title("quantum",             df=all_articles)),
        ("search_by_keyword",    lambda: search_by_keyword("protein",           df=all_articles)),
        ("filter_by_year",       lambda: filter_by_year(2026,                   df=all_articles)),
        ("filter_by_year_range", lambda: filter_by_year_range(2020, 2026,       df=all_articles)),
        ("filter_by_field",      lambda: filter_by_field("physics",             df=all_articles)),
        ("filter_by_journal",    lambda: filter_by_journal("Nature",            df=all_articles)),
        ("filter_by_confidence", lambda: filter_by_confidence(1.0,              df=all_articles)),
        ("filter_by_flags",      lambda: filter_by_flags("missing_authors",     df=all_articles)),
        ("get_clean_articles",   lambda: get_clean_articles(                    df=all_articles)),
        ("full_text_search",     lambda: full_text_search("hydrogen evolution", df=all_articles)),
        ("combined_search",      lambda: combined_search(field="chemistry", year_from=2020,
                                                         year_to=2026, df=all_articles)),
    ]

    print("\n  Query Results")
    print(f"  {'Function':<25} {'Hits':>5}  {'Example title (first result)'}")
    print(f"  {'-'*25} {'-'*5}  {'-'*40}")
    for name, fn in tests:
        t0 = time.perf_counter(); result = fn(); ms = (time.perf_counter() - t0) * 1000
        first = str(result.iloc[0].get("title") or "(no title)")[:50] if len(result) else ""
        print(f"  {name:<25} {len(result):>5}  {first}  [{ms:.1f}ms]")

    print("\n  Benchmark (1000 full-text searches, in-memory)")
    queries = ["quantum","protein","hydrogen","machine learning","density functional",
               "nonlinear","photocatalytic","chebyshev","entropy","evolution"]
    t0 = time.perf_counter()
    for i in range(1000): full_text_search(queries[i % len(queries)], df=all_articles)
    total_ms = (time.perf_counter() - t0) * 1000
    print(f"     1000 queries in {total_ms:.0f}ms  ({total_ms/1000:.2f}ms avg per query)\n")