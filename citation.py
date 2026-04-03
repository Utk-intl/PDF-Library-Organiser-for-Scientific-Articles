# =============================================================================
# citation.py
# DS3294 — PDF Library Organiser for Scientific Articles
# Project #12
#
# Citation features — run via:  python main.py --citations
#
# Two features:
#   1. Citation count  — fetches citation count for each article from the
#                        Semantic Scholar API and writes it back to the DB
#   2. Citation network — builds a directed NetworkX graph where nodes are
#                         article_ids and edges represent citations parsed
#                         from each PDF's reference section
#
# API key:
#   Set the environment variable SEMANTIC_SCHOLAR_API_KEY before running.
#   The API works without a key but is rate-limited to 1 req/s.
#   With a key the limit rises to 10 req/s.
#
#   export SEMANTIC_SCHOLAR_API_KEY="your_key_here"
#
# Dependencies:
#   pip install requests networkx
# =============================================================================

import os
import re
import time
import logging
import requests
import pdfplumber
import pandas as pd

log = logging.getLogger(__name__)

# Semantic Scholar search endpoint
_SS_SEARCH_URL = "https://api.semanticscholar.org/graph/v1/paper/search"
_SS_FIELDS     = "title,authors,year,externalIds,citationCount"

# Delay between API calls (seconds) — 1.0 without key, 0.15 with key
_DELAY_NO_KEY  = 1.1
_DELAY_WITH_KEY = 0.15


# =============================================================================
# HELPERS
# =============================================================================

def _api_headers() -> dict:
    """Build request headers — includes API key if set in environment."""
    headers = {"Accept": "application/json"}
    key = os.environ.get("SEMANTIC_SCHOLAR_API_KEY", "").strip()
    if key:
        headers["x-api-key"] = key
        log.info("Semantic Scholar: using API key")
    else:
        log.warning("SEMANTIC_SCHOLAR_API_KEY not set — rate limited to 1 req/s")
    return headers


def _request_delay() -> float:
    key = os.environ.get("SEMANTIC_SCHOLAR_API_KEY", "").strip()
    return _DELAY_WITH_KEY if key else _DELAY_NO_KEY


def _clean_title(title: str) -> str:
    """Strip LaTeX, special chars, and extra whitespace for API queries."""
    if not title:
        return ""
    title = re.sub(r'\$[^$]+\$', '', title)    # remove LaTeX math
    title = re.sub(r'[^\w\s\-]', ' ', title)   # keep word chars and hyphens
    title = re.sub(r'\s+', ' ', title).strip()
    return title


# =============================================================================
# FEATURE 1 — CITATION COUNT
# =============================================================================

def fetch_citation_count(title: str, year: int = None) -> tuple:
    """
    Query the Semantic Scholar API for a paper by title and return
    (citation_count, doi).

    Args:
        title:  Paper title string
        year:   Publication year (optional — used to disambiguate results)

    Returns:
        (citation_count: int, doi: str) — both None if not found
    """
    query = _clean_title(title)
    if not query or len(query) < 10:
        return None, None

    params = {
        "query":  query,
        "fields": _SS_FIELDS,
        "limit":  5,
    }

    try:
        headers = _api_headers()
        resp = requests.get(_SS_SEARCH_URL, params=params,
                            headers=headers, timeout=10)

        if resp.status_code == 429:
            log.warning("Rate limited by Semantic Scholar — sleeping 60s")
            time.sleep(60)
            resp = requests.get(_SS_SEARCH_URL, params=params,
                                headers=headers, timeout=10)

        if resp.status_code != 200:
            log.debug(f"Semantic Scholar returned {resp.status_code} for: {query[:60]}")
            return None, None

        data = resp.json()
        papers = data.get("data", [])
        if not papers:
            return None, None

        # Pick best match: prefer exact title match, then year match
        query_lower = query.lower()
        best = None
        for paper in papers:
            pt = _clean_title(paper.get("title", "")).lower()
            if pt == query_lower:
                best = paper
                break
            if year and paper.get("year") == year and best is None:
                best = paper

        if best is None:
            best = papers[0]

        citation_count = best.get("citationCount")
        ext_ids = best.get("externalIds", {})
        doi = ext_ids.get("DOI")

        return citation_count, doi

    except requests.RequestException as e:
        log.error(f"Semantic Scholar request failed: {e}")
        return None, None


def enrich_citations(df: pd.DataFrame) -> pd.DataFrame:
    """
    Fetch citation counts for all articles in the DataFrame and return
    an updated DataFrame with citation_count and doi columns filled in.

    Also writes updated values back to the DB via storage.update().

    Args:
        df:  Full library DataFrame from storage.get_all()

    Returns:
        Updated DataFrame with citation_count and doi columns.
    """
    from storage import update

    df = df.copy()
    if "citation_count" not in df.columns:
        df["citation_count"] = None
    if "doi" not in df.columns:
        df["doi"] = None

    delay = _request_delay()
    total  = len(df)
    found  = 0
    failed = 0

    log.info(f"Fetching citation counts for {total} articles "
             f"(delay: {delay}s per request)...")

    for idx, row in df.iterrows():
        title = row.get("title")
        year  = row.get("year")
        art_id = row.get("article_id")

        if not title:
            log.debug(f"  Skipping {art_id} — no title")
            failed += 1
            continue

        count, doi = fetch_citation_count(title, year)
        time.sleep(delay)

        if count is not None:
            df.at[idx, "citation_count"] = count
            df.at[idx, "doi"] = doi
            found += 1
            log.info(f"  [{found}/{total}] {str(title)[:50]:<50} "
                     f"citations: {count}  doi: {doi or 'N/A'}")
            # Write back to DB
            update_fields = {"citation_count": count}
            if doi:
                update_fields["doi"] = doi
            try:
                update(art_id, update_fields)
            except Exception as e:
                log.warning(f"  DB update failed for {art_id}: {e}")
        else:
            failed += 1
            log.debug(f"  Not found: {str(title)[:60]}")

    log.info(f"Citation enrichment done — "
             f"{found} found, {failed} not found / skipped")
    return df


# =============================================================================
# FEATURE 2 — CITATION NETWORK
# =============================================================================

def _extract_references_from_pdf(file_path: str) -> list:
    """
    Extract reference titles from the reference section of a PDF.

    Strategy:
      - Find the page containing "References" or "Bibliography"
      - Extract lines that look like citation entries
      - Return a list of raw reference strings

    Returns:
        List of reference text strings (may be noisy)
    """
    refs = []
    try:
        with pdfplumber.open(file_path) as pdf:
            ref_started = False
            for page in pdf.pages:
                text = page.extract_text() or ""
                lines = [l.strip() for l in text.splitlines() if l.strip()]

                for line in lines:
                    # Detect start of references section
                    if re.match(r'^(References|Bibliography|REFERENCES|BIBLIOGRAPHY)\s*$',
                                line):
                        ref_started = True
                        continue

                    if not ref_started:
                        continue

                    # Skip very short lines, page numbers, URLs
                    if len(line) < 15:
                        continue
                    if re.match(r'^https?://', line):
                        continue
                    if re.match(r'^\d+$', line):
                        continue

                    refs.append(line)

    except Exception as e:
        log.debug(f"Reference extraction failed for {file_path}: {e}")

    return refs


def build_citation_network(df: pd.DataFrame) -> object:
    """
    Build a directed citation network from the library.

    Nodes  — article_id for each paper in the library
    Edges  — directed edge from A to B if A's reference section
             contains text matching B's title

    Node attributes stored:
      title, field, year, citation_count

    Args:
        df:  Full library DataFrame from storage.get_all()

    Returns:
        networkx.DiGraph — the citation graph
    """
    try:
        import networkx as nx
    except ImportError:
        log.error("networkx not installed. Run: pip install networkx")
        return None

    G = nx.DiGraph()

    # Add all papers as nodes
    for _, row in df.iterrows():
        art_id = row.get("article_id")
        if not art_id:
            continue
        G.add_node(art_id,
                   title=row.get("title") or "",
                   field=row.get("field") or "",
                   year=row.get("year"),
                   citation_count=row.get("citation_count"))

    # Build a lookup: normalised title -> article_id
    title_to_id = {}
    for _, row in df.iterrows():
        t = row.get("title")
        art_id = row.get("article_id")
        if t and art_id:
            title_to_id[_clean_title(t).lower()] = art_id

    # For each paper, parse its references and add edges
    log.info(f"Building citation network for {len(df)} articles...")
    edges_added = 0

    for _, row in df.iterrows():
        src_id    = row.get("article_id")
        file_path = row.get("file_path")

        if not src_id or not file_path:
            continue

        refs = _extract_references_from_pdf(file_path)

        for ref_text in refs:
            ref_clean = _clean_title(ref_text).lower()
            # Check if any known paper title appears in this reference line
            for known_title, tgt_id in title_to_id.items():
                if tgt_id == src_id:
                    continue
                if len(known_title) > 15 and known_title in ref_clean:
                    G.add_edge(src_id, tgt_id)
                    edges_added += 1
                    break

    log.info(f"Citation network built — "
             f"{G.number_of_nodes()} nodes, {G.number_of_edges()} edges")
    return G


def print_network_summary(G) -> None:
    """
    Print a summary of the citation network to the terminal.
    Shows top cited papers, isolated papers, and domain-level stats.
    """
    try:
        import networkx as nx
    except ImportError:
        return

    print("\n" + "=" * 65)
    print("  CITATION NETWORK SUMMARY")
    print("=" * 65)
    print(f"  Nodes (papers)  : {G.number_of_nodes()}")
    print(f"  Edges (citations): {G.number_of_edges()}")

    # In-degree = how many times this paper is cited within the corpus
    in_degrees = sorted(G.in_degree(), key=lambda x: x[1], reverse=True)

    print("\n  Most cited papers within corpus:")
    print(f"  {'Title':<50} {'Cited by':>8}")
    print("  " + "-" * 60)
    for node_id, deg in in_degrees[:10]:
        if deg == 0:
            break
        title = G.nodes[node_id].get("title", "(no title)")[:48]
        print(f"  {title:<50} {deg:>8}")

    isolated = [n for n in G.nodes if G.degree(n) == 0]
    print(f"\n  Papers with no citations in corpus: {len(isolated)}")
    print("=" * 65 + "\n")