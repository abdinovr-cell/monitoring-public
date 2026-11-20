import gzip
import html as html_lib
import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlparse, urljoin
from zoneinfo import ZoneInfo
from bs4 import BeautifulSoup

ROOT = "https://atyrau.edu.kz"
HOSTS = {"atyrau.edu.kz", "www.atyrau.edu.kz"}
NEWS_QUERIES = [
    '"Атырауский университет имени Х. Досмухамедова" OR "Атырауский университет Досмухамедова"',
    '"Атырау университеті Х. Досмұхамедов" OR "Atyrau University Dosmukhamedov"',
]
SOCIAL_PROFILES = [
    ("Instagram", "https://www.instagram.com/atyrau_university/"),
    ("Facebook", "https://www.facebook.com/Atyrauuniversity"),
]
STATE, OUT = Path("state"), Path("output")
STATE.mkdir(exist_ok=True)
OUT.mkdir(exist_ok=True)
LATEST, CYCLE = STATE / "extended_latest.json", STATE / "document_cycle.json"
REPORT = OUT / "extended_report.json"
TIME_LIMIT = int(os.getenv("EXTENDED_TIMEOUT_SECONDS", "900"))
MAX_SITEMAPS = int(os.getenv("MAX_SITEMAPS", "40"))
MAX_DOCUMENTS = int(os.getenv("MAX_DOCUMENTS", "180"))
MAX_NEWS_ITEMS = int(os.getenv("MAX_NEWS_ITEMS", "100"))
DOC_EXT = {".pdf", ".docx", ".doc", ".xlsx", ".xls", ".pptx", ".ppt"}
UA = "AtyrauUniversityMonitor/3.0 (public information monitoring)"


def stamp():
    return datetime.now(timezone.utc).isoformat()


def local_stamp(value):
    if not value:
        return "не указано"
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(ZoneInfo("Asia/Almaty")).strftime("%d.%m.%Y %H:%M:%S") + " (Атырау, UTC+5)"
    except Exception:
        return str(value)


def digest(value):
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()


def load(path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"[WARN] Не удалось прочитать {path}: {exc}", flush=True)
        return default


def save(path, value):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    tmp.replace(path)


def fetch(url, timeout=12, limit=35_000_000):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        data = response.read(limit + 1)
        if len(data) > limit:
            raise ValueError("Ответ превышает установленный размер")
        return data, response.headers.get("Content-Type", "")


def local_official(url):
    p = urlparse(url)
    return p.scheme in ("http", "https") and (p.hostname or "").lower() in HOSTS


def sitemap_inventory():
    queue = [
        ROOT + "/robots.txt", ROOT + "/sitemap.xml",
        ROOT + "/sitemap_index.xml", ROOT + "/sitemap-index.xml",
        ROOT + "/ru/sitemap.xml", ROOT + "/kz/sitemap.xml",
    ]
    visited, found, urls, errors = set(), set(), set(), []
    while queue and len(visited) < MAX_SITEMAPS:
        url = queue.pop(0)
        if url in visited:
            continue
        visited.add(url)
        try:
            raw, _ = fetch(url, timeout=25, limit=12_000_000)
            found.add(url)
            if url.endswith("robots.txt"):
                for line in raw.decode("utf-8", "replace").splitlines():
                    m = re.match(r"\s*Sitemap:\s*(\S+)", line, re.I)
                    if m:
                        candidate = urljoin(url, m.group(1))
                        if local_official(candidate):
                            queue.append(candidate)
                continue
            if url.lower().endswith(".gz"):
                raw = gzip.decompress(raw)
            root = ET.fromstring(raw)
            for loc in root.findall(".//{*}loc"):
                value = (loc.text or "").strip()
                if not value or not local_official(value):
                    continue
                if value.lower().endswith((".xml", ".xml.gz")):
                    queue.append(value)
                else:
                    urls.add(value)
        except urllib.error.HTTPError as exc:
            if exc.code != 404 or url.endswith("robots.txt"):
                errors.append({"url": url, "error": str(exc)[:220]})
        except Exception as exc:
            errors.append({"url": url, "error": str(exc)[:220]})
    if queue:
        errors.append({"url": "(sitemap index)", "error": f"Достигнут лимит карт сайта: {MAX_SITEMAPS}"})
    return sorted(urls), sorted(found), errors


def extract_document(url, raw, content_type=""):
    ext = Path(urlparse(url).path).suffix.lower()
    mime = content_type.split(";")[0].strip().lower()
    mime_map = {
        "application/pdf": ".pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    }
    ext = ext or mime_map.get(mime, "")
    import io
    if ext == ".pdf":
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(raw), strict=False)
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    if ext == ".docx":
        from docx import Document
        return "\n".join(p.text for p in Document(io.BytesIO(raw)).paragraphs)
    if ext == ".xlsx":
        from openpyxl import load_workbook
        book = load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
        parts = []
        for sheet in book.worksheets:
            for row in sheet.iter_rows(values_only=True):
                parts.append(" ".join(str(v) for v in row if v is not None))
        return "\n".join(parts)
    if ext == ".pptx":
        from pptx import Presentation
        deck = Presentation(io.BytesIO(raw))
        return "\n".join(shape.text for slide in deck.slides for shape in slide.shapes if hasattr(shape, "text"))
    return ""


def summarize_document(text, max_chars=800):
    """Create a short source-grounded description from the first readable sentences."""
    if not text:
        return ""
    sentences = [part.strip() for part in re.split(r"(?<=[.!?])\s+", text) if part.strip()]
    summary = " ".join(sentences[:3]) if sentences else text.strip()
    return summary[:max_chars].rstrip()


def collect_candidates(sitemap_urls, page_snapshot):
    candidates = set()
    for url in sitemap_urls:
        if Path(urlparse(url).path).suffix.lower() in DOC_EXT:
            candidates.add(url)
    for item in page_snapshot.values():
        for url in item.get("attachments", []):
            if local_official(url) and (
                Path(urlparse(url).path).suffix.lower() in DOC_EXT
                or item.get("title") == "Ссылка на скачиваемый документ"
            ):
                candidates.add(url)
    return sorted(candidates)


def scan_document_batch(candidates, cycle, deadline):
    known = set(cycle.get("known", []))
    pending = list(cycle.get("pending", []))
    for url in candidates:
        if url not in known:
            known.add(url)
            pending.append(url)
    cycle["known"] = sorted(known)
    cycle["pending"] = pending
    cycle.setdefault("staging", {})
    cycle.setdefault("attempts", {})
    cycle.setdefault("errors", [])
    cycle.setdefault("unavailable", {})
    cycle.setdefault("extraction_warnings", {})
    processed = 0
    while cycle["pending"] and processed < MAX_DOCUMENTS and time.monotonic() < deadline:
        url = cycle["pending"].pop(0)
        processed += 1
        try:
            # Retry network failures, but do not retry unreadable document text.
            raw, ctype = fetch(url, timeout=20, limit=35_000_000)
        except Exception as exc:
            count = cycle["attempts"].get(url, 0) + 1
            cycle["attempts"][url] = count
            error = {"url": url, "error": str(exc)[:220], "attempt": count}
            cycle["errors"] = [e for e in cycle["errors"] if e["url"] != url]
            if count < 5:
                cycle["errors"].append(error)
                cycle["pending"].append(url)
                print(f"[DOC DOWNLOAD ERROR] {url} attempt={count}: {str(exc)[:160]}", flush=True)
            else:
                cycle["unavailable"][url] = str(exc)[:220]
                print(f"[DOC UNAVAILABLE] {url}: {str(exc)[:160]}", flush=True)
        else:
            extraction_error = ""
            try:
                text = extract_document(url, raw, ctype)
            except Exception as exc:
                text = ""
                extraction_error = str(exc)[:220]
                cycle["extraction_warnings"][url] = extraction_error
                print(f"[DOC TEXT WARNING] {url}: {extraction_error[:140]}; binary fingerprint saved", flush=True)
            clean_text = re.sub(r"\s+", " ", text).strip() if text else ""
            cycle["staging"][url] = {
                "hash": hashlib.sha256(raw).hexdigest(),
                "text_hash": digest(clean_text) if clean_text else "",
                "text_excerpt": clean_text[:6000],
                "summary": summarize_document(clean_text) if clean_text else (
                    "Текстовый слой не извлечён; сохранена контрольная сумма файла. "
                    "Для сканированного PDF может потребоваться OCR." if extraction_error else
                    "Текстовое содержимое отсутствует или формат не поддерживает извлечение."
                ),
                "bytes": len(raw), "content_type": ctype,
                "text_chars": len(text), "checked_at": stamp(),
                "title": urllib.parse.unquote(Path(urlparse(url).path).name),
                "text_extraction_warning": extraction_error,
            }
            cycle["attempts"].pop(url, None)
            cycle["unavailable"].pop(url, None)
            cycle["errors"] = [e for e in cycle["errors"] if e["url"] != url]
        if processed % 25 == 0:
            print(f"[DOCS] batch={processed} staged={len(cycle['staging'])} pending={len(cycle['pending'])}", flush=True)
    cycle["updated_at"] = stamp()
    return cycle, processed


def in_last_24_hours(value, now_utc=None):
    """Return True only for timestamps in the rolling 24-hour window."""
    now_utc = now_utc or datetime.now(timezone.utc)
    if isinstance(value, datetime):
        parsed = value
    else:
        raw = str(value or "").strip()
        if not raw:
            return False
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except Exception:
            try:
                parsed = parsedate_to_datetime(raw)
            except Exception:
                return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    parsed = parsed.astimezone(timezone.utc)
    return now_utc - timedelta(hours=24) <= parsed <= now_utc + timedelta(minutes=5)


def news_inventory():
    records, status, errors = {}, [], []
    now_utc = datetime.now(timezone.utc)
    for query in NEWS_QUERIES:
        params = urllib.parse.urlencode({"q": query, "hl": "ru", "gl": "KZ", "ceid": "KZ:ru"})
        url = "https://news.google.com/rss/search?" + params
        try:
            raw, _ = fetch(url, timeout=15, limit=8_000_000)
            root = ET.fromstring(raw)
            count = 0
            for item in root.findall(".//item"):
                title = (item.findtext("title") or "").strip()
                link = (item.findtext("link") or "").strip()
                pub = (item.findtext("pubDate") or "").strip()
                if not pub or not in_last_24_hours(pub, now_utc):
                    continue
                guid = (item.findtext("guid") or link or title).strip()
                source_tag = item.find("source")
                source_name = (source_tag.text or "").strip() if source_tag is not None else "Google News RSS"
                if guid:
                    records[guid] = {
                        "title": title, "url": link, "published": pub,
                        "source": source_name or "Google News RSS",
                        "hash": digest(title + "|" + link + "|" + pub),
                        "checked_at": stamp(),
                    }
                    count += 1
                if len(records) >= MAX_NEWS_ITEMS:
                    break
            status.append({"query": query, "checked": True, "items": count, "source": "Google News RSS"})
        except Exception as exc:
            errors.append({"url": url, "error": str(exc)[:220]})
            status.append({"query": query, "checked": False, "reason": str(exc)[:220]})
    return records, status, errors


def parse_published_at(html_text):
    """Extract a publication timestamp from public post metadata without bypassing access controls."""
    soup = BeautifulSoup(html_text, "html.parser")
    values = []
    wanted = {
        "article:published_time", "og:updated_time", "datepublished", "datecreated",
        "published_time", "parsely-pub-date", "publishdate", "timestamp",
    }
    for tag in soup.find_all("meta"):
        key = (tag.get("property") or tag.get("name") or tag.get("itemprop") or "").lower()
        if key in wanted and tag.get("content"):
            values.append(tag.get("content"))
    for tag in soup.find_all("time"):
        if tag.get("datetime"):
            values.append(tag.get("datetime"))
    decoded = html_lib.unescape(html_text).replace("\\/", "/")
    for pattern in (
        r'"taken_at_timestamp"\s*:\s*"?(\d{10,13})"?',
        r'"publish_time"\s*:\s*"?(\d{10,13})"?',
        r'"created_time"\s*:\s*"?(\d{10,13})"?',
        r'"datePublished"\s*:\s*"([^"]+)"',
    ):
        match = re.search(pattern, decoded, flags=re.I)
        if match:
            values.append(match.group(1))
    for value in values:
        raw = str(value).strip()
        try:
            if re.fullmatch(r"\d{10,13}", raw):
                number = int(raw)
                if len(raw) == 13:
                    number /= 1000
                return datetime.fromtimestamp(number, tz=timezone.utc)
            try:
                parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except Exception:
                parsed = parsedate_to_datetime(raw)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        except Exception:
            continue
    return None


def extract_social_permalink_candidates(platform, html_text, profile_url):
    """Extract only post permalinks actually present in the public profile HTML."""
    decoded = html_lib.unescape(html_text).replace("\\/", "/")
    soup = BeautifulSoup(decoded, "html.parser")
    found = []
    for tag in soup.find_all("a", href=True):
        candidate = urllib.parse.urljoin(profile_url, html_lib.unescape(tag.get("href", "")))
        parsed = urlparse(candidate)
        host = (parsed.hostname or "").lower()
        path = parsed.path or ""
        if platform == "Instagram":
            if host.endswith("instagram.com") and re.search(r"/(?:p|reel|tv)/[A-Za-z0-9_-]+/?$", path):
                found.append(candidate)
        elif platform == "Facebook":
            query = urllib.parse.parse_qs(parsed.query)
            is_post_path = bool(re.search(r"/(?:posts|videos|reel)/[^/?]+", path, flags=re.I))
            is_photo_path = bool(re.search(r"/photos/[^/?]+/[^/?]+", path, flags=re.I))
            is_permalink_query = path.lower().endswith(("/permalink.php", "/photo.php")) and bool(
                query.get("story_fbid") or query.get("fbid")
            )
            if host.endswith("facebook.com") and (is_post_path or is_photo_path or is_permalink_query):
                found.append(candidate)
    return list(dict.fromkeys(found))[:20]


def social_inventory():
    """Check only publicly accessible official profile/post pages; never log in or bypass challenges."""
    records, status, errors = {}, [], []
    now_utc = datetime.now(timezone.utc)
    for platform, profile_url in SOCIAL_PROFILES:
        try:
            raw, _ = fetch(profile_url, timeout=18, limit=6_000_000)
            profile_html = raw.decode("utf-8", errors="replace")
            candidates = extract_social_permalink_candidates(platform, profile_html, profile_url)
            found, missing_timestamp, post_errors = 0, 0, 0
            for post_url in candidates[:12]:
                try:
                    post_raw, _ = fetch(post_url, timeout=18, limit=4_000_000)
                    post_html = post_raw.decode("utf-8", errors="replace")
                    published_at = parse_published_at(post_html)
                    if not published_at:
                        missing_timestamp += 1
                        continue
                    if not in_last_24_hours(published_at, now_utc):
                        continue
                    soup = BeautifulSoup(post_html, "html.parser")
                    title_tag = soup.find("meta", attrs={"property": "og:title"})
                    desc_tag = soup.find("meta", attrs={"property": "og:description"})
                    title = (title_tag.get("content") if title_tag else "") or (
                        soup.title.get_text(" ", strip=True) if soup.title else f"{platform}: публикация"
                    )
                    summary = (desc_tag.get("content") if desc_tag else "") or ""
                    records[post_url] = {
                        "title": title[:500],
                        "url": post_url,
                        "published": published_at.isoformat(),
                        "summary": summary[:1500],
                        "source": platform,
                        "hash": digest(title + "|" + summary + "|" + published_at.isoformat()),
                        "checked_at": stamp(),
                    }
                    found += 1
                except Exception as exc:
                    post_errors += 1
                    errors.append({"url": post_url, "error": str(exc)[:220]})
            status.append({
                "platform": platform, "checked": True, "items": found,
                "candidate_posts": len(candidates), "timestamp_missing": missing_timestamp,
                "post_errors": post_errors,
                "reason": "" if candidates else "Публичная страница не предоставила permalink публикаций в доступном HTML.",
            })
        except Exception as exc:
            reason = str(exc)[:220]
            status.append({"platform": platform, "checked": False, "items": 0, "reason": reason})
            errors.append({"url": profile_url, "error": reason})
    return records, status, errors

def text_change_summary(old_text, new_text, limit=10):
    def fragments(value):
        return [x.strip() for x in re.split(r"(?<=[.!?;])\s+|(?<=:)\s+", value or "") if len(x.strip()) > 12]
    old_parts, new_parts = fragments(old_text), fragments(new_text)
    old_set, new_set = set(old_parts), set(new_parts)
    added = [x for x in new_parts if x not in old_set][:limit]
    removed = [x for x in old_parts if x not in new_set][:limit]
    result = []
    if added:
        result.append("Добавлено/появилось: " + " | ".join(added))
    if removed:
        result.append("Удалено/исчезло: " + " | ".join(removed))
    return result


def compare(current, previous):
    changes = []
    for key, item in current.items():
        old = previous.get(key)
        if old is None:
            changes.append({"type": "new", "key": key, **item,
                            "detected_at_utc": item.get("checked_at"),
                            "change_summary": ["Файл впервые обнаружен в ходе мониторинга."]})
        elif old.get("hash") != item.get("hash"):
            details = text_change_summary(old.get("text_excerpt", ""), item.get("text_excerpt", ""))
            if not details:
                if old.get("text_hash") == item.get("text_hash"):
                    details = ["Бинарное содержимое файла изменилось, но извлечённый текст остался прежним."]
                elif not old.get("text_excerpt"):
                    details = ["Файл изменился. Предыдущая версия в исходном снимке не содержала текстового фрагмента для построчного сравнения."]
                else:
                    details = ["Контрольная сумма файла изменилась; отдельные текстовые фрагменты автоматически не выделены."]
            changes.append({
                "type": "changed", "key": key, **item,
                "previous_checked_at_utc": old.get("checked_at"),
                "detected_at_utc": item.get("checked_at"),
                "old_summary": old.get("summary", ""),
                "change_summary": details,
                "old_text_excerpt": old.get("text_excerpt", "")[:6000],
                "new_text_excerpt": item.get("text_excerpt", "")[:6000],
            })
    return changes


def main():
    started = time.monotonic()
    previous = load(LATEST, {})
    cycle = load(CYCLE, {})
    print("[EXT] Проверка robots.txt и XML-карт сайта…", flush=True)
    sitemap_urls, sitemap_files, sitemap_errors = sitemap_inventory()
    page_snapshot = load(STATE / "latest.json", {})
    candidates = collect_candidates(sitemap_urls, page_snapshot)
    if not cycle:
        cycle = {"known": [], "pending": [], "staging": {}, "attempts": {}, "errors": [],
                 "started_at": stamp()}
    cycle, processed = scan_document_batch(candidates, cycle, started + TIME_LIMIT - 75)
    batch_pending = len(cycle["pending"])
    batch_errors = len(cycle["errors"])
    cycle_discovered = len(cycle["known"])
    cycle_staged = len(cycle["staging"])
    cycle_unavailable = len(cycle.get("unavailable", {}))
    cycle_extraction_warnings = len(cycle.get("extraction_warnings", {}))
    documents_ready = batch_pending == 0 and batch_errors == 0 and (cycle_staged + cycle_unavailable) == len(cycle["known"])
    doc_changes, doc_removed, doc_baseline_created = [], [], False
    if documents_ready:
        old_docs = previous.get("documents", {})
        if previous.get("documents_baseline_ready"):
            doc_changes = compare(cycle["staging"], old_docs)
            doc_removed = sorted(set(old_docs) - set(cycle["staging"]))
        else:
            doc_baseline_created = True
        previous["documents"] = cycle["staging"]
        previous["documents_baseline_ready"] = True
        save(LATEST, previous)
        CYCLE.unlink(missing_ok=True)
        cycle = {}
    else:
        save(CYCLE, cycle)

    if time.monotonic() < started + TIME_LIMIT - 30:
        news, news_status, news_errors = news_inventory()
    else:
        news, news_status, news_errors = {}, [
            {"query": q, "checked": False, "reason": "Не хватило временного бюджета"} for q in NEWS_QUERIES
        ], []
    old_news = previous.get("news", {})
    news_changes = compare(news, old_news)
    previous["news"] = {**old_news, **news}

    if time.monotonic() < started + TIME_LIMIT - 30:
        social, social_status, social_errors = social_inventory()
    else:
        social, social_errors = {}, []
        social_status = [
            {"platform": platform, "checked": False, "items": 0, "reason": "Не хватило временного бюджета"}
            for platform, _ in SOCIAL_PROFILES
        ]
    old_social = previous.get("social", {})
    social_changes = compare(social, old_social)
    previous["social"] = {**old_social, **social}
    previous["social_status"] = social_status

    previous["sitemap_urls"] = sitemap_urls
    previous["sitemap_files"] = sitemap_files
    previous["saved_at_utc"] = stamp()
    save(LATEST, previous)
    save(STATE / "sitemap_urls.json", sitemap_urls)

    # Compare sitemap inventories against the previous persisted inventory.
    old_sitemaps = load(STATE / "sitemap_urls_previous.json", [])
    sitemap_added = sorted(set(sitemap_urls) - set(old_sitemaps)) if old_sitemaps else []
    save(STATE / "sitemap_urls_previous.json", sitemap_urls)

    sitemap_ok = bool(sitemap_files) and not sitemap_errors
    news_ok = all(x.get("checked") for x in news_status) and not news_errors
    social_ok = all(x.get("checked") for x in social_status) and not social_errors
    complete = sitemap_ok and documents_ready and news_ok and social_ok
    report = {
        "generated_at_utc": stamp(), "complete": complete,
        "sitemap_files": sitemap_files, "sitemap_urls_count": len(sitemap_urls),
        "sitemap_added": sitemap_added, "sitemap_errors": sitemap_errors,
        "documents_discovered": cycle_discovered,
        "documents_checked_this_run": processed,
        "documents_checked_total": cycle_staged if cycle else len(previous.get("documents", {})),
        "documents_pending": batch_pending, "documents_errors": batch_errors,
        "documents_extraction_warnings": cycle_extraction_warnings,
        "documents_unavailable": cycle_unavailable,
        "documents_baseline_ready": bool(previous.get("documents_baseline_ready")),
        "documents_baseline_created_this_run": doc_baseline_created,
        "documents_complete_this_run": documents_ready,
        "document_changes": doc_changes, "document_removed": doc_removed,
        "news_status": news_status, "news_errors": news_errors, "news_changes": news_changes,
        "social_status": social_status, "social_errors": social_errors, "social_changes": social_changes,
        "social_monitoring": "public_pages_only",
        "baseline": not bool(previous.get("news")) and not bool(previous.get("documents_baseline_ready")),
        "note": "Социальные сети проверяются только по публично доступным страницам и прямым permalink; авторизация, капча и ограничения доступа не обходятся. СМИ проверяются через Google News RSS, что не является исчерпывающим поиском по всему интернету."
    }
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [
        "# Расширенный мониторинг: карты сайта, документы, социальные сети и СМИ", "",
        f"- Время формирования: {local_stamp(report['generated_at_utc'])}",
        f"- Статус этапа: {'полный' if complete else 'частичный'}",
        f"- Карт сайта/robots обработано: {len(sitemap_files)}; URL в картах: {len(sitemap_urls)}",
        f"- Документов в текущем цикле: {report['documents_discovered']}; проверено в этом запуске: {processed}",
        f"- Осталось документов: {batch_pending}; ошибок загрузки: {batch_errors}; недоступно после повторов: {cycle_unavailable}",
        f"- Предупреждений извлечения текста: {cycle_extraction_warnings} (контрольные суммы файлов сохранены)",
        f"- Исходный снимок документов готов: {report['documents_baseline_ready']}",
        f"- Социальные сети: {'; '.join(x['platform'] + ': ' + ('публичная страница получена' if x.get('checked') else 'ограничение доступа') + '; постов за 24 часа: ' + str(x.get('items', 0)) for x in social_status)}",
        "", "## Документы — новые и изменённые",
    ]
    if doc_changes:
        for item in doc_changes:
            lines += ["", f"### {item.get('title') or item['key']}",
                      f"- Тип: {'Новый файл' if item['type'] == 'new' else 'Изменённый файл'}",
                      f"- Прямая ссылка: {item['key']}",
                      f"- Обнаружено при проверке: {local_stamp(item.get('detected_at_utc') or item.get('checked_at'))}"]
            if item.get("previous_checked_at_utc"):
                lines.append(f"- Предыдущая проверка: {local_stamp(item['previous_checked_at_utc'])}")
            if item.get("summary"):
                lines.append(f"- Кратко о содержании: {item['summary']}")
            for detail in item.get("change_summary", []):
                lines.append(f"- Что изменилось: {detail}")
            if item.get("text_extraction_warning"):
                lines.append(f"- Ограничение: текст не извлечён ({item['text_extraction_warning']}); сравнение выполнено по контрольной сумме.")
    else:
        lines.append("- Новые/изменённые документы не установлены в этом запуске.")
    lines += [f"- Удалён документ: {u}" for u in doc_removed]
    lines += ["", "## Социальные сети — новые публикации за последние 24 часа"]
    for x in social_status:
        lines.append(f"- {x['platform']}: {'публичная страница получена' if x.get('checked') else 'ограничение доступа'}; прямых постов: {x.get('items', 0)}; найдено permalink: {x.get('candidate_posts', 0)}; без времени публикации: {x.get('timestamp_missing', 0)}")
        if x.get("reason"):
            lines.append(f"  - Примечание: {x['reason']}")
    for x in social_changes:
        lines.append(f"- {x.get('source', 'Соцсеть')}: [{x.get('title') or x.get('url')}]({x.get('url') or x.get('key')}) — {x.get('published', 'дата не установлена')}")
    if not social_changes:
        lines.append("- Подтверждённых новых публикаций за последние 24 часа не обнаружено.")
    lines += ["", "## Упоминания в СМИ (Google News RSS)"]
    for x in news_status:
        lines.append(f"- Запрос: {x['query']} — {'успешно' if x['checked'] else 'ошибка'}; публикаций: {x.get('items', 0)}")
    lines += [f"- {x['type']}: [{x.get('title') or x['key']}]({x.get('url') or x['key']})" for x in news_changes]
    if sitemap_errors or cycle.get("errors") or cycle.get("extraction_warnings") or news_errors or social_errors:
        lines += ["", "## Ошибки и ограничения"]
        for e in (sitemap_errors + cycle.get("errors", []) + news_errors + social_errors)[:100]:
            lines.append(f"- {e['url']}: {e['error']}")
    lines += ["", "Примечание: соцсети проверяются только по публично доступному HTML и извлечённым permalink; ограничения доступа не обходятся. СМИ проверяются через Google News RSS и это не исчерпывающий поиск по всему интернету. PDF-сканы без текстового слоя и устаревшие бинарные DOC/XLS/PPT сравниваются по контрольной сумме без OCR."]
    (OUT / "extended_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"[EXT SUMMARY] complete={complete} sitemaps={len(sitemap_files)} sitemap_urls={len(sitemap_urls)} "
          f"docs_batch={processed} docs_pending={batch_pending} docs_errors={batch_errors} "
          f"news_changes={len(news_changes)} social_changes={len(social_changes)} social_errors={len(social_errors)}", flush=True)


if __name__ == "__main__":
    main()
