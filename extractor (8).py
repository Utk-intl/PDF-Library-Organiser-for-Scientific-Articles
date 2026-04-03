import re, sys, hashlib, logging, os, time
from pathlib import Path

try:
    import pdfplumber
    PDFPLUMBER_AVAILABLE = True
except ImportError:
    PDFPLUMBER_AVAILABLE = False
    logging.warning("pdfplumber not installed — run: pip install pdfplumber")

try:
    from PyPDF2 import PdfReader
    PYPDF2_AVAILABLE = True
except ImportError:
    PYPDF2_AVAILABLE = False
    logging.warning("PyPDF2 not installed — run: pip install PyPDF2")

from schema import empty_article, validate_article, VALID_FLAGS, VALID_SOURCES

log = logging.getLogger(__name__)
_seen_abstract_hashes: dict = {}

_TITLE_BLACKLIST = [
    r'microsoft\s+word', r'\.docx?$', r'\.tex$', r'^untitled', r'^new\s+document',
    r'libreoffice', r'openoffice', r'^draft$', r'^\s*$', r'manuscript',
    r'^nature\s+communications?$', r'^nature\s+methods$', r'^nature\s+physics$',
    r'^nature\s+chemistry$', r'^communications\s+physics$', r'^scientific\s+reports?$',
    r'^article\s+in\s+press$', r'^bmc\s+', r'^plos\s+', r'^frontiers\s+in',
    r'^doi:\s*10\.', r'^https?://', r'^preprint', r'^accepted\s+(manuscript|article)',
    r'^references?$', r'^supplementary', r'^supporting\s+information',
]
_AUTHOR_BLACKLIST = [r'^administrator$', r'^user$', r'^owner$', r'^unknown$',
                     r'^author$', r'^me$', r'^default\s+user', r'^\s*$']

def _is_garbage_title(t: str) -> bool:
    if not t: return True
    s = t.strip()
    if len(s) < 5: return True
    tl = s.lower()
    if any(re.search(p, tl) for p in _TITLE_BLACKLIST): return True
    if ' ' not in s and s.isupper() and not any(c.isdigit() for c in s): return True
    if sum(1 for c in s if ord(c) > 127 or not c.isprintable()) / len(s) > 0.3: return True
    if re.search(r'\.(doc|docx|tex|pdf|odt|rtf|txt)\b', tl): return True
    return False

def _is_garbage_author(a: str) -> bool:
    return not a or any(re.search(p, a.strip().lower()) for p in _AUTHOR_BLACKLIST)


def _extract_embedded(file_path: str) -> dict:
    result = {}
    if PYPDF2_AVAILABLE:
        try:
            meta = PdfReader(file_path).metadata or {}
            raw = {k: (meta.get(k) or "").strip() for k in ("/Title","/Author","/Keywords","/Subject","/CreationDate")}
            if raw["/Title"] and not _is_garbage_title(raw["/Title"]): result["title"] = raw["/Title"]
            if raw["/Author"] and not _is_garbage_author(raw["/Author"]): result["authors"] = _parse_authors(raw["/Author"])
            if raw["/Keywords"]: result["keywords"] = _parse_keywords(raw["/Keywords"])
            if not result.get("keywords") and raw["/Subject"]: result["keywords"] = _parse_keywords(raw["/Subject"])
            if raw["/CreationDate"]:
                if y := _extract_year_from_date(raw["/CreationDate"]): result["year"] = y
        except Exception as e: log.debug(f"PyPDF2 failed: {e}")
    if PDFPLUMBER_AVAILABLE:
        try:
            with pdfplumber.open(file_path) as pdf:
                meta = pdf.metadata or {}
                if not result.get("title") and meta.get("Title"):
                    t = meta["Title"].strip()
                    if not _is_garbage_title(t): result["title"] = t
                if not result.get("authors") and meta.get("Author"):
                    a = meta["Author"].strip()
                    if not _is_garbage_author(a): result["authors"] = _parse_authors(a)
                if not result.get("keywords") and meta.get("Keywords"): result["keywords"] = _parse_keywords(meta["Keywords"])
                if not result.get("year") and meta.get("CreationDate"):
                    if y := _extract_year_from_date(meta["CreationDate"]): result["year"] = y
        except Exception as e: log.debug(f"pdfplumber embedded failed: {e}")
    return result


def _extract_from_text(file_path: str) -> dict:
    result = {}
    if not PDFPLUMBER_AVAILABLE: return result
    try:
        with pdfplumber.open(file_path) as pdf:
            if not pdf.pages: return result
            p1_text = pdf.pages[0].extract_text() or ""
            p2_text = pdf.pages[1].extract_text() if len(pdf.pages) > 1 else ""
            full    = p1_text + "\n" + p2_text
            lines   = [l.strip() for l in p1_text.splitlines() if l.strip()]

            title = _find_title_from_chars(pdf.pages[0])
            if not title:
                title = next((l for l in lines[:10] if len(l) > 10 and not re.match(r'^[\d\s\.]+$', l)), "")
            if title and _is_garbage_title(_clean_title(title)): title = ""
            if not title and len(pdf.pages) > 1:
                title = _find_title_from_chars(pdf.pages[1])
                if not title:
                    p2l = [l.strip() for l in p2_text.splitlines() if l.strip()]
                    title = next((l for l in p2l[:10] if len(l) > 10 and not re.match(r'^[\d\s\.]+$', l)), "")
            if title:
                cleaned = _clean_title(title)
                if not _is_garbage_title(cleaned): result["title"] = cleaned

            if y := _find_year_in_text("\n".join(lines[:20])): result["year"] = y
            if a := _find_abstract(full): result["abstract"] = a
            if result.get("title"):
                authors = _find_authors_below_title(lines, result["title"])
                if not authors and len(pdf.pages) > 1:
                    authors = _find_authors_below_title([l.strip() for l in p2_text.splitlines() if l.strip()], result["title"])
                if authors: result["authors"] = authors
            if kw := _find_keywords_in_text(full): result["keywords"] = kw
            if j  := _find_journal_in_text(p1_text): result["journal"] = j
    except Exception as e: log.debug(f"Text parsing failed: {e}")
    return result


def _find_title_from_chars(page) -> str:
    try:
        chars = page.chars
        if not chars: return ""
        sizes = [c.get("size", 0) for c in chars if c.get("size")]
        if not sizes: return ""
        threshold = max(sizes) * 0.85
        page_h    = max((c.get("bottom", 0) for c in chars), default=800)
        title_chars = [c for c in chars if c.get("size", 0) >= threshold and c.get("top", 0) > page_h * 0.12]
        if not title_chars:
            title_chars = [c for c in chars if c.get("size", 0) >= threshold]
        if not title_chars: return ""

        title_chars.sort(key=lambda c: (round(c.get("top", 0) / 3), c.get("x0", 0)))
        lines_out, cur, prev_top, prev_x1 = [], [], None, None
        for c in title_chars:
            top, x0, x1, ch = round(c.get("top", 0) / 3), c.get("x0", 0), c.get("x1", 0) + 1, c.get("text", "")
            if not ch: continue
            if prev_top is not None and top != prev_top:
                if cur: lines_out.append("".join(cur).strip())
                cur = [ch]
            else:
                if prev_x1 is not None and (x0 - prev_x1) > 2: cur.append(" ")
                cur.append(ch)
            prev_top, prev_x1 = top, x1
        if cur: lines_out.append("".join(cur).strip())
        title = re.sub(r'\s+', ' ', " ".join(l for l in lines_out if l)).strip()
        return title[:300] if len(title) > 5 else ""
    except Exception: return ""


def _find_abstract(text: str) -> str:
    for pat in [r'[Aa]bstract[\s\n:—–-]+([\s\S]{50,2000}?)(?=\n\s*(?:1[\.\s]|Introduction|Keywords|Key\s+words|Index\s+Terms|\d\.))',
                r'[Aa]bstract[\s\n:—–-]+([\s\S]{50,1500}?)(?=\n\n)']:
        if m := re.search(pat, text):
            a = re.sub(r'\s+', ' ', m.group(1).strip())
            if len(a) > 50: return a
    return ""


def _find_authors_below_title(lines: list, title: str) -> list:
    STOP = re.compile(r'\b(abstract|introduction|keywords|key\s+words|received|doi'
                      r'|corresponding|correspondence|equal\s+contribution)\b', re.IGNORECASE)

    def _looks_like_authors(line: str) -> bool:
        line = line.strip()
        if not (3 < len(line) < 160) or len(line) > 120: return False
        if re.match(r'^[\d\s\*†‡§¶#,\.]+$', line): return False
        if re.search(r'\b(university|institute|department|lab\b|school|college'
                     r'|hospital|center|centre|faculty|division)\b', line, re.IGNORECASE): return False
        if re.match(r'arXiv:|^[\w\.\-]+@[\w\.\-]+$', line, re.IGNORECASE): return False
        words = line.split()
        cap_words = sum(1 for w in words if w and w[0].isupper() and not w.isupper())
        return ',' in line or bool(re.search(r'\band\b', line, re.IGNORECASE)) or (cap_words >= 2 and len(words) <= 10)

    title_idx = -1
    for plen in (80, 60, 40, 25):
        tc = title.lower()[:plen].strip()
        if len(tc) < 10: continue
        for i, line in enumerate(lines):
            if tc in line.lower(): title_idx = i; break
        if title_idx >= 0: break

    if title_idx >= 0:
        author_lines = []
        for line in lines[title_idx + 1: title_idx + 16]:
            if STOP.search(line): break
            clean = re.sub(r'^[\d,\*†‡§¶# ]+', '', line).strip()
            if clean and _looks_like_authors(clean): author_lines.append(clean)
        if author_lines:
            if r := _parse_authors(" ".join(author_lines)): return r

    abs_idx    = next((i for i, l in enumerate(lines) if re.search(r'\bAbstract\b', l, re.IGNORECASE)), len(lines))
    candidates = [c for l in lines[:abs_idx] if (c := re.sub(r'^[\d,\*†‡§¶# ]+', '', l).strip()) and _looks_like_authors(c)]
    if candidates:
        if r := _parse_authors(" ".join(candidates[-3:])): return r
    return []


def _find_keywords_in_text(text: str) -> list:
    for pat in [r'[Kk]ey\s*[Ww]ords?\s*[:\-—–]\s*(.+?)(?=\n\n|\n[A-Z]|\Z)',
                r'[Ii]ndex\s+[Tt]erms\s*[:\-—–]\s*(.+?)(?=\n\n|\n[A-Z]|\Z)',
                r'KEYWORDS\s*[:\-—–]\s*(.+?)(?=\n\n|\n[A-Z]|\Z)']:
        if m := re.search(pat, text, re.DOTALL):
            return _parse_keywords(m.group(1).strip().split('\n')[0])
    return []


def _find_journal_in_text(text: str) -> str:
    if m := re.search(r'arXiv:\d{4}\.\d{4,6}v?\d*\s+\[([^\]]+)\]', text):
        return f"arXiv [{m.group(1)}]"
    if m := re.search(r'(?:published\s+in|journal\s+of|proceedings\s+of)\s+([^\n\.]{5,80})', text, re.IGNORECASE):
        return m.group(1).strip()
    return ""


def _find_year_in_text(text: str):
    return next((int(m) for m in re.findall(r'\b((?:19|20)\d{2})\b', text) if 1900 <= int(m) <= 2030), None)


def _extract_from_filename(filename_info: dict) -> dict:
    result  = {}
    fn_type = filename_info.get("filename_type", "unknown")
    year    = filename_info.get("year_hint") or filename_info.get("arxiv_year")
    if fn_type in ("arxiv_new", "arxiv_old"):
        result["journal"] = "arXiv"
        if year: result["year"] = year
    elif fn_type in ("nature_springer", "ams", "bmc", "mdpi_versioned", "journal_id"):
        result["journal"] = filename_info.get("journal_hint", "")
        if year and fn_type in ("nature_springer", "ams"): result["year"] = year
    elif fn_type == "descriptive":
        if t := filename_info.get("title_hint"): result["title"] = t
    elif fn_type == "author_year":
        if year: result["year"] = year
        if t := filename_info.get("title_hint"): result["title"] = t
    return result


def _extract_from_semantic_scholar(filename_info: dict, article: dict) -> dict:
    try: import requests
    except ImportError: return {}
    SS_PAPER  = "https://api.semanticscholar.org/graph/v1/paper/"
    SS_SEARCH = "https://api.semanticscholar.org/graph/v1/paper/search"
    FIELDS    = "title,authors,year,abstract,externalIds,citationCount,publicationVenue"
    key       = os.environ.get("SEMANTIC_SCHOLAR_API_KEY", "").strip()
    headers   = {"Accept": "application/json", **({"x-api-key": key} if key else {})}

    def _get(url, params=None):
        try:
            r = requests.get(url, params=params, headers=headers, timeout=10)
            if r.status_code == 429:
                log.warning("Stage D: rate limited — sleeping 60s"); time.sleep(60)
                r = requests.get(url, params=params, headers=headers, timeout=10)
            return r if r.status_code == 200 else None
        except Exception as e: log.debug(f"Stage D request error: {e}"); return None

    def _parse(data: dict) -> dict:
        r = {}
        if data.get("title") and not _is_garbage_title(data["title"]): r["title"] = data["title"].strip()
        if authors := data.get("authors"): r["authors"] = [a["name"] for a in authors if a.get("name")]
        if data.get("year"): r["year"] = int(data["year"])
        if data.get("abstract"): r["abstract"] = data["abstract"].strip()
        if (ids := data.get("externalIds", {})).get("DOI"): r["doi"] = ids["DOI"]
        if (v := data.get("publicationVenue") or {}).get("name"): r["journal"] = v["name"]
        if data.get("citationCount") is not None: r["citation_count"] = data["citationCount"]
        return r

    if arxiv_id := filename_info.get("arxiv_id"):
        clean_id = re.sub(r'v\d+$', '', str(arxiv_id))
        if resp := _get(f"{SS_PAPER}arXiv:{clean_id}", {"fields": FIELDS}):
            if result := _parse(resp.json()): log.info(f"    Stage D: found via arXiv ID ({clean_id})"); return result
    search_title = article.get("title") or filename_info.get("title_hint") or ""
    if len(search_title) > 10:
        clean = re.sub(r'[^\w\s]', ' ', search_title).strip()
        if resp := _get(SS_SEARCH, {"query": clean[:200], "fields": FIELDS, "limit": 3}):
            for paper in resp.json().get("data", []):
                pt, qt = (paper.get("title") or "").lower(), clean.lower()
                if pt and qt and (qt[:40] in pt or pt[:40] in qt):
                    if result := _parse(paper): log.info("    Stage D: found via title search"); return result
    return {}


def _parse_authors(raw: str) -> list:
    if not raw: return []
    raw = re.sub(r'\([^)]*\)|\[[^\]]*\]', '', raw)
    raw = re.sub(r'(?<=[a-zA-Z])[0-9,\*†‡§¶#]+(?=[\s,;]|$)', '', raw).strip()
    if not raw: return []
    if ' and ' in raw.lower(): parts = re.split(r'\s+and\s+', raw, flags=re.IGNORECASE)
    elif ';' in raw: parts = raw.split(';')
    elif ',' in raw:
        cp = [p.strip() for p in raw.split(',')]
        if len(cp) >= 2 and len(cp) % 2 == 0 and all(len(p) <= 4 for p in cp[1::2]):
            parts = [f"{cp[i]}, {cp[i+1]}" for i in range(0, len(cp)-1, 2)]
        else: parts = cp
    else:
        words = raw.split()
        parts = _split_concatenated_names(words) if len(words) >= 4 else [raw]
    authors = []
    for part in parts:
        part = part.strip().rstrip(',').strip()
        if len(part) < 2: continue
        if re.search(r'\b(university|institute|department|lab|school|college|hospital|center|centre)\b', part.lower()): continue
        words = part.split()
        if len(words) >= 4:
            sub = _split_concatenated_names(words)
            if len(sub) > 1: authors.extend(sub); continue
        authors.append(part)
    return authors[:20]


def _split_concatenated_names(words: list) -> list:
    names, i = [], 0
    while i < len(words):
        if i + 1 < len(words) and words[i] and words[i+1] and words[i][0].isupper() and words[i+1][0].isupper():
            names.append(f"{words[i]} {words[i+1]}"); i += 2
        else: names.append(words[i]); i += 1
    return names


def _parse_keywords(raw: str) -> list:
    if not raw: return []
    return [kw for p in re.split(r'[,;•·]', raw)
            if 2 < len(kw := re.sub(r'[^\w\s\-]', '', p.strip().lower()).strip()) < 80][:20]


def _extract_year_from_date(date_str: str):
    if m := re.search(r'((?:19|20)\d{2})', date_str):
        y = int(m.group(1)); return y if 1900 <= y <= 2030 else None
    return None


def _clean_title(title: str) -> str:
    return re.sub(r'\s+', ' ', re.sub(r'\x00', '', title.strip(' \t\n\r.,;:'))).strip()[:300]


def _hash_abstract(abstract: str) -> str:
    return hashlib.md5(re.sub(r'\s+', ' ', abstract.strip().lower()).encode()).hexdigest()


def _add_flags(article: dict) -> list:
    flags = list(article.get("flags", []))
    for field, flag in [("title","missing_title"),("authors","missing_authors"),("abstract","missing_abstract")]:
        if not article.get(field): flags.append(flag)
    if article.get("year") is None: flags.append("missing_year")
    seen = set()
    return [f for f in flags if not (f in seen or seen.add(f))]


def _merge(base: dict, update: dict) -> dict:
    for k, v in update.items():
        if v is None: continue
        if isinstance(v, list):
            if not base.get(k): base[k] = v
        elif isinstance(v, (str, int)):
            if not base.get(k): base[k] = v
        elif isinstance(v, float):
            if not base.get(k) or base.get(k) == 0.0: base[k] = v
    return base


def extract_metadata(skeleton: dict) -> dict:
    file_path     = skeleton.get("file_path", "")
    filename_info = skeleton.get("filename_info", {})
    log.info(f"  Extracting: {Path(file_path).name}")
    article = dict(skeleton)

    def _log_stage(label, fields):
        log.info(f"    {label}: " + "  ".join(f"{f}={'✓' if article.get(f) else '✗'}" for f in fields))

    if embedded := _extract_embedded(file_path):
        _merge(article, embedded); article.update(source="embedded", confidence=1.0)
        _log_stage("Stage A (embedded)", ["title","authors","year","keywords"])

    if not all([article.get("title"), article.get("authors"), article.get("year"), article.get("abstract")]):
        if parsed := _extract_from_text(file_path):
            _merge(article, parsed)
            if article.get("source") != "embedded": article.update(source="parsed", confidence=0.7)
            _log_stage("Stage B (text)", ["title","authors","year","abstract"])

    if not all([article.get("title"), article.get("year"), article.get("journal")]) and filename_info:
        if fn_hints := _extract_from_filename(filename_info):
            _merge(article, fn_hints)
            if article.get("source") not in ("embedded","parsed"): article.update(source="filename", confidence=0.4)
            _log_stage("Stage C (filename)", ["title","year","journal"])

    if not all([article.get("title"), article.get("authors"), article.get("abstract")]):
        try:
            if ss := _extract_from_semantic_scholar(filename_info, article):
                _merge(article, ss)
                if article.get("source") not in ("embedded","parsed"): article.update(source="semantic_scholar", confidence=0.6)
                _log_stage("Stage D (Semantic Scholar)", ["title","authors","abstract"])
        except Exception as e: log.debug(f"Stage D failed: {e}")

    fn_year = filename_info.get("year_hint") or filename_info.get("arxiv_year")
    if fn_year and article.get("year") and int(article["year"]) - int(fn_year) > 5:
        log.info(f"    Year correction: {article['year']} → {fn_year} (filename year)")
        article["year"] = int(fn_year)
        article.setdefault("flags", [])
        if "year_uncertain" not in article["flags"]: article["flags"].append("year_uncertain")

    if not article.get("source"): article.update(source="filename", confidence=0.4)

    if article.get("year"):
        try: article["year"] = int(article["year"])
        except (ValueError, TypeError):
            article["year"] = None
            article.setdefault("flags", [])
            if "year_uncertain" not in article["flags"]: article["flags"].append("year_uncertain")
    if isinstance(article.get("authors"), str): article["authors"] = _parse_authors(article["authors"])
    if isinstance(article.get("keywords"), str): article["keywords"] = _parse_keywords(article["keywords"])
    elif article.get("keywords"): article["keywords"] = [kw.lower().strip() for kw in article["keywords"]]
    if article.get("confidence") is not None: article["confidence"] = float(article["confidence"])

    if (abstract := article.get("abstract", "")) and len(abstract) > 50:
        abs_hash = _hash_abstract(abstract)
        if (orig := _seen_abstract_hashes.get(abs_hash)) and orig != article.get("article_id"):
            log.warning(f"    Possible duplicate — same abstract as {orig}")
            article["duplicate_of"] = orig
            article.setdefault("flags", [])
            if "possible_duplicate" not in article["flags"]: article["flags"].append("possible_duplicate")
        else: _seen_abstract_hashes[abs_hash] = article.get("article_id", "")

    article["flags"] = _add_flags(article)
    article.pop("filename_info", None)
    if errors := validate_article(article): log.warning(f"    Schema validation warnings: {errors}")
    log.info(f"    Done — source: {article.get('source')}  "
             f"confidence: {article.get('confidence', 0):.0%}  flags: {article.get('flags', [])}")
    return article


if __name__ == "__main__":
    import json
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(message)s")
    if len(sys.argv) < 2: print("Usage: python extractor.py /path/to/paper.pdf"); sys.exit(1)
    file_path = sys.argv[1]
    if not Path(file_path).exists(): print(f"File not found: {file_path}"); sys.exit(1)
    from schema import generate_id, current_timestamp
    skeleton = dict(article_id=generate_id(), file_path=file_path, file_hash="test",
                    ingested_at=current_timestamp(), title=None, authors=[], journal=None,
                    year=None, keywords=[], abstract=None, source=None, confidence=0.0,
                    flags=[], duplicate_of=None, filename_info={})
    print(f"\nExtracting metadata from: {Path(file_path).name}\n{'=' * 60}")
    result = extract_metadata(skeleton)
    print(f"\n{'=' * 60}\n  EXTRACTION RESULT\n{'=' * 60}")
    for k, v in result.items():
        print(f"  {k:<15}: {str(v)[:120] + '...' if k == 'abstract' and v else v}")
    print()
    errors = validate_article(result)
    print(f"Validation warnings: {errors}" if errors else "Schema validation: ✅ passed")