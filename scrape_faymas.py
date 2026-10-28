#!/usr/bin/env python3
"""
Scrape prompts from https://faymas.in and write one folder per main category
(men, women, kids, couple; anything unclear goes to "other"),
each holding CSV files of 100 prompts:

    output/<category>/<category>_1-100.csv
    output/<category>/<category>_101-200.csv
    output/<category>/<category>_201-300.csv   (last file may be shorter, e.g. _301-342)

Usage:
    python scrape_faymas.py                 # discover, scrape new prompts, rebuild CSVs
    python scrape_faymas.py --render        # use a real browser (Playwright) if pages are JS-rendered
    python scrape_faymas.py --build-only    # rebuild CSVs from the cache, no network
    python scrape_faymas.py --inspect URL   # debug: parse a single prompt page and save its HTML
    python scrape_faymas.py --group-by site # use faymas.in's own categories instead of men/women/kids/couple
    python scrape_faymas.py --limit 20      # only scrape 20 new prompts (good for a first test)

Scraped data is cached in data/prompts.jsonl, so reruns only fetch new prompts.
"""
import argparse
import csv
import json
import re
import shutil
import sys
import time
import urllib.robotparser
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

BASE = "https://faymas.in"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; faymas-prompt-archiver/1.0)"}
CHUNK = 100
DATA_DIR = Path("data")
OUT_DIR = Path("output")
CACHE = DATA_DIR / "prompts.jsonl"
CSV_FIELDS = ["id", "category", "title", "prompt", "ai_model", "url", "site_category"]


# ----------------------------------------------------------------- helpers
def clean(s):
    return re.sub(r"\s+", " ", s or "").strip()


def slugify(s):
    s = re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")
    return s or "uncategorized"


def norm_url(u):
    return u.split("#")[0].split("?")[0].rstrip("/")


def http_get(session, url, retries=3):
    for i in range(retries):
        try:
            r = session.get(url, headers=HEADERS, timeout=30)
            if r.status_code == 200:
                return r
            if r.status_code in (403, 404, 410):
                return None
        except requests.RequestException:
            pass
        time.sleep(2 * (i + 1))
    return None


class Fetcher:
    """Fetches HTML with requests, or with headless Chromium when --render is set."""

    def __init__(self, render=False, delay=1.0):
        self.session = requests.Session()
        self.render = render
        self.delay = delay
        self.page = None
        if render:
            from playwright.sync_api import sync_playwright

            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch()
            self.page = self._browser.new_page(user_agent=HEADERS["User-Agent"])

    def html(self, url):
        time.sleep(self.delay)
        if self.render:
            try:
                self.page.goto(url, wait_until="networkidle", timeout=45000)
                return self.page.content()
            except Exception:
                return None
        r = http_get(self.session, url)
        return r.text if r else None

    def close(self):
        if self.render:
            self._browser.close()
            self._pw.stop()


# ---------------------------------------------------------------- discovery
def check_robots():
    rp = urllib.robotparser.RobotFileParser()
    try:
        rp.set_url(BASE + "/robots.txt")
        rp.read()
        if not rp.can_fetch(HEADERS["User-Agent"], BASE + "/prompt/x"):
            sys.exit("robots.txt disallows fetching /prompt/ pages. Stopping.")
    except SystemExit:
        raise
    except Exception:
        pass  # robots.txt unreachable: carry on


def urls_from_sitemap(session, url, seen):
    if url in seen:
        return []
    seen.add(url)
    r = http_get(session, url)
    if not r:
        return []
    soup = BeautifulSoup(r.text, "xml")
    out = []
    for loc in soup.find_all("loc"):
        u = loc.text.strip()
        if u.endswith(".xml") or "sitemap" in u.rsplit("/", 1)[-1]:
            out += urls_from_sitemap(session, u, seen)
        elif "/prompt/" in u:
            out.append(norm_url(u))
    return out


def discover_via_sitemap(fetcher):
    session = fetcher.session
    candidates = [BASE + "/sitemap.xml", BASE + "/sitemap_index.xml"]
    r = http_get(session, BASE + "/robots.txt")
    if r:
        for line in r.text.splitlines():
            if line.lower().startswith("sitemap:"):
                candidates.append(line.split(":", 1)[1].strip())
    seen, urls = set(), []
    for c in candidates:
        urls += urls_from_sitemap(session, c, seen)
    return list(dict.fromkeys(urls))


def discover_via_crawl(fetcher, start_urls):
    """Scroll the homepage / category pages in a real browser and collect /prompt/ links."""
    if not fetcher.render:
        sys.exit("Crawling needs a browser. Install Playwright and rerun with --render.")
    page = fetcher.page
    found, queue, visited = {}, list(start_urls), set()
    cat_re = re.compile(r"/(category|categories)/[^/?#]+$")
    while queue:
        start = queue.pop(0)
        if start in visited:
            continue
        visited.add(start)
        print(f"  crawling {start}")
        try:
            page.goto(start, wait_until="networkidle", timeout=45000)
        except Exception:
            continue
        last, stable = 0, 0
        for _ in range(300):
            hrefs = page.eval_on_selector_all("a[href]", "els => els.map(e => e.href)")
            for h in hrefs:
                h = norm_url(h)
                if "/prompt/" in h:
                    found.setdefault(h, len(found))
                elif cat_re.search(h) and h not in visited:
                    queue.append(h)
            page.mouse.wheel(0, 20000)
            try:
                page.get_by_role("button", name=re.compile("load more|show more", re.I)).first.click(timeout=500)
            except Exception:
                pass
            page.wait_for_timeout(1000)
            stable = stable + 1 if len(found) == last else 0
            last = len(found)
            if stable >= 4:
                break
    return list(found)


def discover(fetcher, args):
    urls = []
    if not args.crawl:
        urls = discover_via_sitemap(fetcher)
        print(f"Sitemap: {len(urls)} prompt URLs")
    if not urls:
        print("No sitemap results, crawling the site instead...")
        urls = discover_via_crawl(fetcher, args.start_url or [BASE])
        print(f"Crawl: {len(urls)} prompt URLs")
    return urls


# ------------------------------------------------------------------ parsing
PROMPT_HINT = re.compile(r"prompt", re.I)
JSON_KEYS = {"prompt", "prompttext", "prompt_text", "promptcontent"}


def _walk_json(o):
    if isinstance(o, dict):
        for k, v in o.items():
            if isinstance(v, str):
                if k.lower() in JSON_KEYS and len(v) > 40:
                    yield v
            else:
                yield from _walk_json(v)
    elif isinstance(o, list):
        for i in o:
            yield from _walk_json(i)


def _meta(soup, *names):
    for n in names:
        tag = soup.find("meta", attrs={"property": n}) or soup.find("meta", attrs={"name": n})
        if tag and tag.get("content"):
            return clean(tag["content"])
    return ""


def find_title(soup):
    h1 = soup.find("h1")
    if h1 and clean(h1.get_text(" ")):
        return clean(h1.get_text(" "))
    t = _meta(soup, "og:title") or (soup.title.string if soup.title else "")
    return clean(re.sub(r"\s*[-|]\s*(AI prompt for .*|Faymas)$", "", t or "", flags=re.I))


def find_category(soup):
    # structured hints first
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except Exception:
            continue
        for item in data if isinstance(data, list) else [data]:
            if isinstance(item, dict):
                for key in ("articleSection", "category"):
                    v = item.get(key)
                    if isinstance(v, str) and v.strip():
                        return clean(v)
    v = _meta(soup, "article:section")
    if v:
        return v
    # then links to category pages, ignoring the sidebar / header / footer
    body = BeautifulSoup(str(soup), "lxml")
    for t in body.find_all(["aside", "nav", "header", "footer"]):
        if t.get("aria-label", "").lower() == "breadcrumb":
            continue
        t.decompose()
    for pattern in (r"/categor(y|ies)/", r"/c/", r"/tag/"):
        a = body.find("a", href=re.compile(pattern))
        if a and clean(a.get_text(" ")):
            return clean(a.get_text(" "))
    # breadcrumb as a last resort (second-to-last crumb is usually the category)
    crumbs = soup.select("[aria-label=breadcrumb] a, nav.breadcrumb a, .breadcrumb a")
    texts = [clean(c.get_text(" ")) for c in crumbs if clean(c.get_text(" ")).lower() not in ("home", "")]
    if texts:
        return texts[-1]
    return "uncategorized"


def find_model(soup):
    for el in soup.find_all(string=re.compile(r"^\s*ai\s*model\s*:?\s*$", re.I)):
        parent = el.parent
        sib = parent.find_next_sibling() if parent else None
        if sib and clean(sib.get_text(" ")):
            return clean(sib.get_text(" "))
    text = soup.get_text(" ")
    m = re.search(r"Ai\s*Model\s*:?\s*([A-Za-z][A-Za-z0-9.\-]{1,30})", text)
    if m:
        return m.group(1)
    t = _meta(soup, "og:title") or (soup.title.string if soup.title else "")
    m = re.search(r"AI prompt for ([\w.\- ]+)", t or "", re.I)
    return clean(m.group(1)) if m else ""


def find_prompt_text(soup):
    """Return (text, source). Tries embedded JSON, prompt-ish DOM nodes, then meta description."""
    # 1) JSON embedded in the page
    for script in soup.find_all("script"):
        if script.get("type") == "application/ld+json" or script.get("id") == "__NEXT_DATA__":
            try:
                data = json.loads(script.string or "")
            except Exception:
                continue
            best = max(_walk_json(data), key=len, default="")
            if best:
                return best.strip(), "json"

    # 2) DOM nodes: textarea/pre/code and the innermost elements whose class/id mentions "prompt"
    cands = []
    for el in soup.find_all(["textarea", "pre", "code"]):
        cands.append(el.get_text("\n", strip=True))
    hinted = soup.find_all(attrs={"class": PROMPT_HINT}) + soup.find_all(attrs={"id": PROMPT_HINT})
    for el in hinted:
        if not el.find(attrs={"class": PROMPT_HINT}):
            cands.append(el.get_text("\n", strip=True))
    cands = [c for c in cands if len(c) > 80]
    if cands:
        return max(cands, key=len), "dom"

    # 3) meta description (often truncated)
    return _meta(soup, "og:description", "description", "twitter:description"), "meta"


def parse_prompt_page(html, url):
    soup = BeautifulSoup(html, "lxml")
    text, source = find_prompt_text(soup)
    return {
        "url": url,
        "title": find_title(soup),
        "category": find_category(soup),
        "ai_model": find_model(soup),
        "prompt": text,
        "source": source,
    }


# ------------------------------------------------------- main-category grouping
# Sorts each prompt into men / women / kids / couple (else "other") by keywords in the
# title and prompt text. Runs at CSV-build time, so you can tweak the word lists below
# and rerun with --build-only without scraping again.
COUPLE_STRICT = re.compile(
    r"\b(couples?|bride|groom|boyfriend|girlfriend|husband|wife|newlyweds?|honeymoon|"
    r"lovers?|man and (?:a )?woman|two people|pre-?wedding|engagement)\b")
COUPLE_WEAK = re.compile(r"\b(together|romantic|embrac\w*|hugg?\w*|kiss\w*|holding hands)\b")
KIDS = re.compile(
    r"\b(kids?|child|children|toddlers?|infant|newborn|babies|little (?:boy|girl)|"
    r"schoolboy|schoolgirl|school kids?|cute (?:boy|girl)|"
    r"baby(?![- ]?(?:pink|blue|powder|breath|hair|doll))|"
    r"(?:[1-9]|1[0-2])[- ]?(?:year|yr)s?[- ]old)\b")
WOMEN = re.compile(r"\b(woman|women|girls?|female|lady|ladies|she|her|hers|herself|queen)\b")
MEN = re.compile(r"\b(man|men|boys?|male|guy|guys|gentleman|he|his|him|himself|handsome|beard)\b")


def classify(rec):
    title = (rec.get("title") or "").lower()
    text = title + " " + (rec.get("prompt") or "").lower()

    def score(rx):  # title matches count triple
        return len(rx.findall(text)) + 2 * len(rx.findall(title))

    men, women = score(MEN), score(WOMEN)
    if COUPLE_STRICT.search(text):
        return "couple"
    if men >= 2 and women >= 2 and COUPLE_WEAK.search(text):
        return "couple"
    if KIDS.search(text):
        return "kids"
    if men > women:
        return "men"
    if women > men:
        return "women"
    return "other"


# ----------------------------------------------------------------- pipeline
def load_cache():
    rows = []
    if CACHE.exists():
        with CACHE.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
    return rows


def scrape(args):
    check_robots()
    DATA_DIR.mkdir(exist_ok=True)
    cached = load_cache()
    have = {r["url"] for r in cached}
    seq = max((r["seq"] for r in cached), default=0)

    fetcher = Fetcher(render=args.render, delay=args.delay)
    try:
        urls = discover(fetcher, args)
        todo = [u for u in urls if u not in have]
        if args.limit:
            todo = todo[: args.limit]
        print(f"{len(have)} already cached, {len(todo)} new to scrape")
        with CACHE.open("a", encoding="utf-8") as out:
            for i, url in enumerate(todo, 1):
                html = fetcher.html(url)
                if not html:
                    print(f"[{i}/{len(todo)}] FAILED {url}")
                    continue
                rec = parse_prompt_page(html, url)
                if not rec["prompt"]:
                    print(f"[{i}/{len(todo)}] no prompt text found: {url}")
                    continue
                seq += 1
                rec["seq"] = seq
                rec["scraped_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
                out.write(json.dumps(rec, ensure_ascii=False) + "\n")
                out.flush()
                print(f"[{i}/{len(todo)}] {rec['category']} | {rec['title'][:60]} ({rec['source']})")
    finally:
        fetcher.close()


def build_csvs(args):
    rows = load_cache()
    if not rows:
        print("Nothing in the cache yet, no CSVs written.")
        return
    by_cat = {}
    for r in sorted(rows, key=lambda r: r["seq"]):
        key = classify(r) if args.group_by == "main4" else (r["category"] or "uncategorized")
        by_cat.setdefault(key, []).append(r)

    if OUT_DIR.exists():
        shutil.rmtree(OUT_DIR)
    files = 0
    for cat, items in sorted(by_cat.items()):
        slug = slugify(cat)
        folder = OUT_DIR / slug
        folder.mkdir(parents=True, exist_ok=True)
        for start in range(0, len(items), CHUNK):
            chunk = items[start : start + CHUNK]
            lo, hi = start + 1, start + len(chunk)
            path = folder / f"{slug}_{lo}-{hi}.csv"
            with path.open("w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
                w.writeheader()
                for n, r in enumerate(chunk, lo):
                    prompt = clean(r["prompt"]) if args.flatten else r["prompt"]
                    w.writerow(
                        {
                            "id": n,
                            "category": cat,
                            "site_category": r["category"],
                            "title": r["title"],
                            "prompt": prompt,
                            "ai_model": r["ai_model"],
                            "url": r["url"],
                        }
                    )
            files += 1
        print(f"{cat}: {len(items)} prompts")
    print(f"Wrote {files} CSV files to {OUT_DIR}/")
    if args.group_by == "main4" and "other" in by_cat:
        print(f"{len(by_cat['other'])} prompts landed in 'other' (no clear men/women/kids/couple signal). "
              f"Tweak the word lists in classify() and rerun with --build-only.")
    meta_only = sum(1 for r in rows if r.get("source") == "meta")
    if meta_only:
        print(f"WARNING: {meta_only} prompts came from the meta description and may be truncated. "
              f"Try --render or run --inspect on one prompt URL.")


def inspect(args):
    fetcher = Fetcher(render=args.render, delay=0)
    try:
        html = fetcher.html(args.inspect)
    finally:
        fetcher.close()
    if not html:
        sys.exit("Could not fetch that URL.")
    Path("debug_page.html").write_text(html, encoding="utf-8")
    rec = parse_prompt_page(html, args.inspect)
    print(json.dumps(rec, indent=2, ensure_ascii=False))
    print("\nFull HTML saved to debug_page.html")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--render", action="store_true", help="use headless Chromium (needs playwright)")
    ap.add_argument("--crawl", action="store_true", help="skip the sitemap and crawl pages (needs --render)")
    ap.add_argument("--start-url", action="append", help="extra page to crawl (repeatable)")
    ap.add_argument("--delay", type=float, default=1.0, help="seconds between requests (default 1.0)")
    ap.add_argument("--limit", type=int, default=0, help="max new prompts to scrape this run")
    ap.add_argument("--build-only", action="store_true", help="only rebuild CSVs from the cache")
    ap.add_argument("--group-by", choices=["main4", "site"], default="main4",
                    help="main4 = men/women/kids/couple (default); site = faymas.in's own categories")
    ap.add_argument("--flatten", action="store_true", help="collapse line breaks in prompts to single spaces")
    ap.add_argument("--inspect", metavar="URL", help="parse one prompt page and save its HTML for debugging")
    args = ap.parse_args()

    if args.inspect:
        inspect(args)
        return
    if not args.build_only:
        scrape(args)
    build_csvs(args)


if __name__ == "__main__":
    main()
