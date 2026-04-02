import os, re, hashlib, logging, argparse, json
from pathlib import Path
from schema import empty_article, generate_id, current_timestamp, validate_article
from extractor import extract_metadata
from storage import insert, create_table

LOG_FILE, HASH_STORE_FILE = "ingestion.log", "seen_hashes.json"

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()]
)
log = logging.getLogger(__name__)


def load_seen_hashes() -> set:
    try:
        return set(json.loads(Path(HASH_STORE_FILE).read_text()).get("hashes", []))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()

def save_seen_hashes(seen: set) -> None:
    Path(HASH_STORE_FILE).write_text(json.dumps({"hashes": list(seen)}, indent=2))

def compute_md5(file_path: str) -> str:
    h = hashlib.md5()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()

def scan_folder(folder_path: str) -> list:
    folder = Path(folder_path)
    if not folder.exists():
        raise FileNotFoundError(f"Folder does not exist: {folder_path}")
    if not folder.is_dir():
        raise NotADirectoryError(f"Not a directory: {folder_path}")
    pdfs = sorted(str(p.resolve()) for p in folder.rglob("*.pdf"))
    log.info(f"Scanned '{folder_path}' — found {len(pdfs)} PDF(s)")
    if not pdfs:
        log.warning("No PDFs found. Check your folder path.")
    return pdfs

def build_skeleton(file_path: str, file_hash: str) -> dict:
    article = empty_article()
    article.update({
        "article_id": generate_id(), "file_path": file_path,
        "file_hash": file_hash, "ingested_at": current_timestamp(),
        "filename_info": detect_filename_type(file_path),
    })
    return article

def detect_filename_type(file_path: str) -> dict:
    stem = Path(file_path).stem.strip()
    STRIP_SUFFIXES = ["_reference","_supplementary","_supplement","_si","_supporting",
                      "_appendix","_erratum","_correction","_retraction","_preprint"]
    ARXIV_COPY_RE = re.compile(r'^(\d{4}\.\d{4,6}(?:v\d+)?)-\d+$')

    clean_stem = next((stem[:-len(s)] for s in STRIP_SUFFIXES if stem.lower().endswith(s)), stem)
    if m := ARXIV_COPY_RE.match(clean_stem):
        clean_stem = m.group(1)

    result = dict(filename_type="unknown", confidence=0.0, original_stem=stem,
                  clean_stem=clean_stem,
                  stripped_suffix=stem[len(clean_stem):] if clean_stem != stem else None,
                  title_hint=None, arxiv_id=None, arxiv_year=None, arxiv_month=None,
                  arxiv_url=None, journal_hint=None, volume_hint=None, article_hint=None,
                  author_hint=None, year_hint=None, doi_hint=None, pubmed_id=None,
                  publisher_hint=None)

    def _arxiv_new(m, s):
        yy, mm = int(m.group(1)[:2]), int(m.group(1)[2:])
        if not 1 <= mm <= 12: return None
        yr = 2000 + yy
        return dict(arxiv_id=s, arxiv_year=yr, arxiv_month=mm,
                    arxiv_url=f"https://arxiv.org/abs/{s}", year_hint=yr)

    def _arxiv_old(m, s):
        yymm, yy, mm = m.group(2), int(m.group(2)[:2]), int(m.group(2)[2:4])
        if not 1 <= mm <= 12: return None
        yr = (2000 + yy) if yy <= 30 else (1900 + yy)
        aid = f"{m.group(1)}/{m.group(2)}{m.group(3) or ''}"
        return dict(arxiv_id=aid, arxiv_year=yr, arxiv_month=mm,
                    arxiv_url=f"https://arxiv.org/abs/{aid}", year_hint=yr)

    def _doi(m, s):
        return dict(doi_hint=s.replace("_", "/", 1), title_hint=None)

    def _pubmed(m, s):
        return dict(pubmed_id=m.group(1))

    def _journal_id(m, s):
        if len(m.group(1).split("-")) > 4: return None
        return dict(journal_hint=m.group(1).replace("-"," ").title(),
                    volume_hint=int(m.group(2)), article_hint=m.group(3))

    def _author_year(m, s):
        author = m.group(1).capitalize()
        year   = int(f"{m.group(2)}{m.group(3)}")
        rest   = m.group(4).replace("_"," ").replace("-"," ").strip()
        return dict(author_hint=author, year_hint=year,
                    title_hint=f"{author} ({year}) {rest.capitalize()}" if rest else None)

    stop = {"and","or","of","the","a","an","in","for","to","by","with","on","from","at","via"}
    def _descriptive(m, s):
        words = s.replace("-"," ").split()
        return dict(title_hint=" ".join(w if w in stop else w.capitalize() for w in words))

    def _underscore(m, s):
        return dict(title_hint=" ".join(w.capitalize() for w in s.replace("_"," ").split()))

    def _camel(m, s):
        return dict(title_hint=re.sub(r'([A-Z]{2,})', r' \1',
                    re.sub(r'([A-Z][a-z]+)', r' \1', s)).strip())

    def _bmc(m, s):
        BMC = {"1471-2105":"BMC Bioinformatics","1471-2091":"BMC Biochemistry",
               "1471-2148":"BMC Evolutionary Biology","1471-2164":"BMC Genomics",
               "1471-2180":"BMC Microbiology","1471-2407":"BMC Cancer",
               "1472-6750":"BMC Biotechnology","1471-2199":"BMC Molecular Biology",
               "1471-2229":"BMC Plant Biology"}
        ip = f"{m.group(1)}-{m.group(2)}"
        return dict(journal_hint=BMC.get(ip, f"BMC Journal ({ip})"),
                    volume_hint=int(m.group(3)), article_hint=m.group(4), publisher_hint="bmc")

    def _nature_springer(m, s):
        NATURE = {"41598":"Scientific Reports","41586":"Nature","41467":"Nature Communications",
                  "42005":"Communications Physics","41565":"Nature Nanotechnology",
                  "41560":"Nature Energy","41557":"Nature Chemistry",
                  "41562":"Nature Human Behaviour","41551":"Nature Biomedical Engineering"}
        jc = m.group(1)
        return dict(journal_hint=NATURE.get(jc, f"Nature/Springer (s{jc})"),
                    year_hint=2000+int(m.group(2)), article_hint=m.group(3),
                    publisher_hint="nature_springer")

    def _mdpi_versioned(m, s):
        j, vol, art, ver = m.group(1).capitalize(), int(m.group(2)), m.group(3), m.group(4)
        return dict(journal_hint=j, volume_hint=vol, article_hint=art, publisher_hint="mdpi",
                    title_hint=f"{j} vol.{vol} article {art} ({ver})")

    def _ams(m, s):
        AMS = {"0002-9904":"Bulletin of the AMS","0002-9939":"Proceedings of the AMS",
               "0002-9947":"Transactions of the AMS","0025-5718":"Mathematics of Computation",
               "0273-0979":"Bulletin of the AMS (New Series)",
               "1088-6850":"Transactions of the AMS (Electronic)",
               "0894-0347":"Journal of the AMS","1088-6826":"Proceedings of the AMS (Electronic)"}
        ip, raw = f"{m.group(1)}-{m.group(2)}", m.group(3)
        yy = int(raw)
        yr = int(raw) if len(raw)==4 else (2000+yy if yy<=30 else 1900+yy)
        return dict(journal_hint=AMS.get(ip, f"AMS Journal ({ip})"), year_hint=yr,
                    article_hint=m.group(4), publisher_hint="ams")

    PATTERNS = [
        ("arxiv_new",       re.compile(r'^(\d{4})\.(\d{4,6})(v\d+)?$'),                              _arxiv_new,       1.00),
        ("arxiv_old",       re.compile(r'^([a-z][a-z\-]+[a-z])(\d{7})(v\d+)?$'),                     _arxiv_old,       0.95),
        ("doi_file",        re.compile(r'^10\.\d{4,9}[_\-].+$'),                                      _doi,             0.95),
        ("pubmed",          re.compile(r'^PMC(\d{4,10})$', re.IGNORECASE),                             _pubmed,          0.95),
        ("nature_springer", re.compile(r'^s(\d{5})-(\d{3})-(\d{5})-([a-z0-9]+)$'),                   _nature_springer, 0.95),
        ("ams",             re.compile(r'^S(\d{4})-(\d{4})-(\d{2,4})-(.+)$', re.IGNORECASE),          _ams,             0.95),
        ("bmc",             re.compile(r'^(\d{4})-(\d{4})-(\d{1,4})-(\d+)$'),                         _bmc,             0.90),
        ("mdpi_versioned",  re.compile(r'^([a-z][a-z\-]*[a-z])-(\d{1,4})-(\d{3,8})-(v\d+)$'),        _mdpi_versioned,  0.90),
        ("journal_id",      re.compile(r'^([a-z][a-z\-]*[a-z])-(\d{1,4})-(\d{3,8})$'),               _journal_id,      0.85),
        ("author_year",     re.compile(r'^([a-z]{2,20})[-_]?(19|20)(\d{2})[-_]?(.*)$', re.IGNORECASE),_author_year,     0.75),
        ("descriptive",     re.compile(r'^[a-z][a-z0-9]*(-[a-z0-9]+){3,}$'),                         _descriptive,     0.65),
        ("underscore",      re.compile(r'^[a-z][a-z0-9]*(_[a-z0-9]+){2,}$'),                         _underscore,      0.60),
        ("camel_case",      re.compile(r'^[A-Z][a-z]+([A-Z][a-z]+){2,}(\d{4})?$'),                   _camel,           0.55),
        ("numeric_id",      re.compile(r'^\d{4,12}$'),                                                lambda m,s: dict(article_hint=s), 0.40),
    ]

    best_name, best_conf, best_extras = "unknown", 0.0, {}
    for name, pat, handler, conf in PATTERNS:
        if (m := pat.match(clean_stem)) and (extras := handler(m, clean_stem)) is not None:
            if conf > best_conf:
                best_name, best_conf, best_extras = name, conf, extras

    result.update({"filename_type": best_name, "confidence": best_conf, **best_extras})
    return result


def detect_field_from_path(file_path: str) -> str:
    path_lower = "/".join(p.lower() for p in Path(file_path).parts[:-1])
    FIELDS = [
        ("mathematics", ["mathematics", "maths", "/math"]),
        ("physics",     ["physics", "hep-exp", "hep_exp", "hepexp", "high-energy", "hep"]),
        ("biology",     ["biology", "biomed", "biochem", "life-science", "/bio"]),
        ("chemistry",   ["chemistry", "chemical", "/chem"]),
    ]
    return next((f for f, kws in FIELDS if any(kw in path_lower for kw in kws)), "unknown")


def print_report(results: dict) -> None:
    total = results["processed"] + results["duplicates"] + results["failed"]
    sep = "=" * 55
    print(f"\n{sep}\n  INGESTION REPORT\n{sep}")
    print(f"  Total PDFs found         : {total}")
    print(f"  ✅ Successfully ingested : {results['processed']}")
    print(f"  ⏭️  Duplicates skipped   : {results['duplicates']}")
    print(f"  ❌ Failed                : {results['failed']}")
    print(sep)
    if results["skipped_files"]:
        print("\n  Skipped (duplicates):")
        for f in results["skipped_files"]: print(f"    - {Path(f).name}")
    if results["failed_files"]:
        print("\n  Failed (errors):")
        for f, r in results["failed_files"]: print(f"    - {Path(f).name}: {r}")
    print(f"\n  Full log: {LOG_FILE}\n")


def ingest_folder(folder_path, dry_run=False, extract_fn=None, store_fn=None) -> list:
    log.info("=" * 55)
    log.info(f"Starting ingestion  folder={folder_path}  dry_run={dry_run}")
    log.info("=" * 55)

    seen_hashes = load_seen_hashes()
    pdf_files   = scan_folder(folder_path)
    results     = dict(processed=0, duplicates=0, failed=0, skipped_files=[], failed_files=[])
    ingested    = []

    for file_path in pdf_files:
        filename = Path(file_path).name
        log.info(f"Processing: {filename}")
        try:
            file_hash = compute_md5(file_path)
            log.info(f"  Hash: {file_hash}")

            if file_hash in seen_hashes:
                log.warning(f"  SKIPPED — duplicate: {filename}")
                results["duplicates"] += 1; results["skipped_files"].append(file_path)
                continue

            skeleton          = build_skeleton(file_path, file_hash)
            skeleton["field"] = detect_field_from_path(file_path)
            fn_info           = skeleton["filename_info"]

            log.info(f"  Research field   : {skeleton['field']}")
            log.info(f"  Filename type    : {fn_info['filename_type']} "
                     f"(confidence: {fn_info['confidence']:.0%})")

            _FN_LOG = {
                "arxiv_new":      lambda i: [log.info(f"  arXiv ID         : {i['arxiv_id']}"),
                                             log.info(f"  arXiv year/month : {i['arxiv_year']}/{i['arxiv_month']:02d}"),
                                             log.info(f"  arXiv URL        : {i['arxiv_url']}")],
                "arxiv_old":      lambda i: [log.info(f"  arXiv ID (old)   : {i['arxiv_id']}"),
                                             log.info(f"  arXiv URL        : {i['arxiv_url']}")],
                "doi_file":       lambda i: log.info(f"  DOI hint         : {i['doi_hint']}"),
                "pubmed":         lambda i: log.info(f"  PubMed ID        : {i['pubmed_id']}"),
                "nature_springer":lambda i: [log.info(f"  Journal          : {i['journal_hint']}"),
                                             log.info(f"  Year hint        : {i['year_hint']}"),
                                             log.info(f"  Article no.      : {i['article_hint']}")],
                "ams":            lambda i: [log.info(f"  Journal          : {i['journal_hint']}"),
                                             log.info(f"  Year hint        : {i['year_hint']}"),
                                             log.info(f"  Article no.      : {i['article_hint']}")],
                "mdpi_versioned": lambda i: log.info(f"  Journal hint     : {i['journal_hint']}  "
                                                     f"vol: {i['volume_hint']}  article: {i['article_hint']}"),
                "bmc":            lambda i: [log.info(f"  Journal          : {i['journal_hint']}"),
                                             log.info(f"  Volume           : {i['volume_hint']}"),
                                             log.info(f"  Article no.      : {i['article_hint']}")],
                "journal_id":     lambda i: log.info(f"  Journal hint     : {i['journal_hint']}  "
                                                     f"vol: {i['volume_hint']}  article: {i['article_hint']}"),
                "author_year":    lambda i: [log.info(f"  Author hint      : {i['author_hint']}"),
                                             log.info(f"  Year hint        : {i['year_hint']}"),
                                             i['title_hint'] and log.info(f"  Title hint       : {i['title_hint']}")],
            }
            if fn := _FN_LOG.get(fn_info["filename_type"]):
                fn(fn_info)
            elif fn_info["filename_type"] in ("descriptive","underscore","camel_case"):
                log.info(f"  Title hint       : {fn_info['title_hint']}")
            elif fn_info["filename_type"] == "numeric_id":
                log.info(f"  Numeric ID       : {fn_info['article_hint']}")
            if fn_info.get("stripped_suffix"):
                log.info(f"  Stripped suffix  : {fn_info['stripped_suffix']}")

            log.info(f"  article_id       : {skeleton['article_id']}")
            log.info(f"  ingested_at      : {skeleton['ingested_at']}")

            seen_hashes.add(file_hash)

            if dry_run:
                log.info("  DRY RUN — skeleton built, not passing to extractor/storage")
                ingested.append(skeleton); results["processed"] += 1; continue

            article = extract_fn(skeleton) if extract_fn else (
                log.info("  Extractor not connected — using skeleton only") or skeleton)

            for e in (validate_article(article) or []):
                log.warning(f"  Validation warning: {e}")

            if store_fn:
                store_fn(article); log.info("  Saved successfully")
            else:
                log.info("  Storage not connected — record not saved to DB")

            ingested.append(article); results["processed"] += 1
            log.info(f"  ✅ Done: {filename}")

        except (FileNotFoundError, PermissionError, Exception) as e:
            log.error(f"  ❌ {type(e).__name__} on {filename}: {e}")
            results["failed"] += 1; results["failed_files"].append((file_path, str(e)))

    save_seen_hashes(seen_hashes)
    log.info(f"Hash store updated — {len(seen_hashes)} total hashes on record")
    print_report(results)
    return ingested


def parse_args():
    p = argparse.ArgumentParser(description="PDF Library Organiser — Ingestion Module")
    p.add_argument("--input", default="/Users/utkarsh8022/Downloads/DSP_data")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--reset-hashes", action="store_true")
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    if args.reset_hashes and Path(HASH_STORE_FILE).exists():
        os.remove(HASH_STORE_FILE)
        log.info("Hash store cleared — all files will be treated as new")

    articles = ingest_folder(args.input, dry_run=args.dry_run,
                             extract_fn=extract_metadata, store_fn=insert)

    if articles:
        full, flagged, dups = (
            sum(1 for a in articles if not a.get("flags")),
            sum(1 for a in articles if a.get("flags")),
            sum(1 for a in articles if a.get("duplicate_of")),
        )
        sep = "=" * 65
        print(f"\n{sep}\n  PIPELINE COMPLETE — {len(articles)} articles ingested & saved\n{sep}")
        print(f"\n  Extraction quality:")
        print(f"    ✅ Fully extracted (no flags) : {full}")
        print(f"    ⚠️  Partially extracted (flags): {flagged}")
        print(f"    🔁 Content duplicates found   : {dups}")
        print(f"\n  Per-article summary:")
        print(f"  {'#':<4} {'Filename':<45} {'Source':<10} {'Conf':>5} {'Flags'}")
        print(f"  {'-'*4} {'-'*45} {'-'*10} {'-'*5} {'-'*30}")
        for i, a in enumerate(articles, 1):
            fname = Path(a['file_path']).name[:44]
            print(f"  {i:<4} {fname:<45} {a.get('source') or '—':<10} "
                  f"{a.get('confidence', 0.0):>4.0%} {', '.join(a.get('flags', [])) or '—'}")
        print(f"\n  Backend: {__import__('storage').BACKEND}  |  DB/CSV saved to working directory\n")