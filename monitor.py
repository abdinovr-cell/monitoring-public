import asyncio
import difflib
import hashlib
import gzip
import json
import os
import re
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from pathlib import Path
from urllib.parse import urlparse, urldefrag, urlunparse, parse_qsl, urlencode
from collections import deque

from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

BASES = [
    "https://atyrau.edu.kz/ru",
    "https://atyrau.edu.kz/kz",
]
ALLOWED_LANGS = {"ru", "kz"}
HOSTS = {"atyrau.edu.kz", "www.atyrau.edu.kz"}
DOC_EXT = (".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx")
MAX_PAGES = int(os.getenv("MAX_PAGES", "3000"))
PAGE_TIMEOUT_MS = int(os.getenv("PAGE_TIMEOUT_MS", "18000"))
PAGE_WALL_TIMEOUT_SECONDS = int(os.getenv("PAGE_WALL_TIMEOUT_SECONDS", "25"))
MAX_CONCURRENCY = max(1, int(os.getenv("MAX_CONCURRENCY", "4")))
RUN_TIMEOUT_SECONDS = int(os.getenv("RUN_TIMEOUT_SECONDS", "3000"))
PROGRESS_EVERY = max(1, int(os.getenv("PROGRESS_EVERY", "25")))
OUT, STATE = Path("output"), Path("state")
OUT.mkdir(exist_ok=True)
STATE.mkdir(exist_ok=True)
LATEST = STATE / "latest.json"
CHECKPOINT = STATE / "checkpoint.json"
CHECKPOINT_GZ = STATE / "checkpoint.json.gz"
BASELINE_META = STATE / "baseline_meta.json"
BASELINE_SCHEMA_VERSION = 1


def now():
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


def clean_url(url):
    """Canonical URL for a public RU/KZ HTML page."""
    if not url:
        return None
    url, _ = urldefrag(url.strip())
    p = urlparse(url)
    if p.scheme not in ("http", "https") or (p.hostname or "").lower() not in HOSTS:
        return None
    path = re.sub(r"/{2,}", "/", p.path or "/").rstrip("/") or "/"
    parts = [part for part in path.split("/") if part]
    if not parts or parts[0].lower() not in ALLOWED_LANGS:
        return None
    if path.lower().endswith(DOC_EXT):
        return None
    # Keep functional parameters (pagination, filters, item IDs); discard only common
    # analytics/session parameters so distinct content pages are not collapsed.
    tracking = {"fbclid", "gclid", "dclid", "yclid", "mc_cid", "mc_eid", "sessionid", "phpsessid"}
    query = []
    for key, value in parse_qsl(p.query, keep_blank_values=True):
        low = key.lower()
        if low in tracking or low.startswith("utm_") or low.startswith("_pk_"):
            continue
        query.append((key, value))
    query.sort()
    return urlunparse(("https", "atyrau.edu.kz", path, "", urlencode(query, doseq=True), ""))

def is_document(url):
    return urlparse(url).path.lower().endswith(DOC_EXT)


def compact_save(path, data):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    tmp.replace(path)


def load_checkpoint():
    """Read compressed checkpoints; transparently migrate legacy JSON on next save."""
    if CHECKPOINT_GZ.exists():
        try:
            raw = gzip.decompress(CHECKPOINT_GZ.read_bytes()).decode("utf-8")
            return json.loads(raw)
        except Exception as exc:
            print(f"[WARN] Не удалось прочитать сжатую контрольную точку: {exc}", flush=True)
    return load_json(CHECKPOINT, {})


def save_checkpoint(data):
    """Atomically store the checkpoint as gzip JSON and remove the oversized legacy file."""
    raw = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    compressed = gzip.compress(raw, compresslevel=6, mtime=0)
    tmp = CHECKPOINT_GZ.with_suffix(CHECKPOINT_GZ.suffix + ".tmp")
    tmp.write_bytes(compressed)
    tmp.replace(CHECKPOINT_GZ)
    # Delete the 90+ MB legacy JSON only after the compressed checkpoint is safely written.
    CHECKPOINT.unlink(missing_ok=True)
    print(f"[CHECKPOINT] Сохранено gzip: {len(raw):,} -> {len(compressed):,} байт.", flush=True)


def normalize_text(html):
    soup = BeautifulSoup(html, "html.parser")
    for node in soup(["script", "style", "noscript", "svg"]):
        node.decompose()
    return re.sub(r"\s+", " ", soup.get_text(" ", strip=True)).strip()


async def inspect_page(context, url):
    page = await context.new_page()
    try:
        response = await page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
        if response and response.status >= 400:
            raise RuntimeError(f"HTTP {response.status}")
        await page.wait_for_timeout(250)
        html = await page.content()
        links = await page.locator("a[href]").evaluate_all(
            "(els) => els.map(a => a.href).filter(Boolean)"
        )
        last_modified = response.headers.get("last-modified") if response else None
        return await page.title(), normalize_text(html), links, last_modified
    finally:
        await page.close()


def load_json(path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"[WARN] Не удалось прочитать {path}: {exc}", flush=True)
        return default


def text_change_summary(old_text, new_text, limit=8):
    """Return explicit added/removed text fragments without dumping whole pages."""
    def fragments(value):
        return [x.strip() for x in re.split(r"(?<=[.!?;])\s+|(?<=:)\s+", value or "") if len(x.strip()) > 12]
    old_parts, new_parts = fragments(old_text), fragments(new_text)
    old_set, new_set = set(old_parts), set(new_parts)
    removed = [x for x in old_parts if x not in new_set][:limit]
    added = [x for x in new_parts if x not in old_set][:limit]
    result = []
    if added:
        result.append("Добавлено/появилось: " + " | ".join(added))
    if removed:
        result.append("Удалено/исчезло: " + " | ".join(removed))
    return result or ["Текст страницы изменился; автоматическое сравнение фрагментов не выделило отдельные предложения."]


def build_report(timestamp, pages, errors, queue, seen, deadline_reached, previous, skipped, baseline_ready):
    changes = []
    # Первый полный обход формирует исходный снимок. Не выдаём все страницы за
    # новые публикации: сравнение изменений начинается после сохранения baseline.
    if baseline_ready and previous:
        for url, cur in pages.items():
            old = previous.get(url)
            if old is None:
                changes.append({
                    "type": "new_page", "url": url, "title": cur.get("title", ""),
                    "new_excerpt": cur.get("text", "")[:5000],
                    "source_last_modified": cur.get("source_last_modified"),
                    "description": (cur.get("text", "")[:900] or "Текст страницы не извлечён."),
                    "detected_at_utc": cur.get("checked_at", timestamp),
                    "change_summary": ["Страница впервые обнаружена в ходе мониторинга."],
                })
            elif old.get("hash") != cur.get("hash") or old.get("title") != cur.get("title"):
                changes.append({
                    "type": "changed_page", "url": url, "title": cur.get("title", ""),
                    "old_excerpt": old.get("text", "")[:5000], "new_excerpt": cur.get("text", "")[:5000],
                    "old_checked_at_utc": old.get("checked_at"),
                    "detected_at_utc": cur.get("checked_at", timestamp),
                    "source_last_modified": cur.get("source_last_modified"),
                    "old_title": old.get("title", ""),
                    "change_summary": text_change_summary(old.get("text", ""), cur.get("text", "")) + (
                        [f"Заголовок изменился: «{old.get('title', '')}» → «{cur.get('title', '')}»."] if old.get("title") != cur.get("title") else []
                    ),
                })
            if old and old.get("attachments", []) != cur.get("attachments", []):
                old_links, new_links = set(old.get("attachments", [])), set(cur.get("attachments", []))
                changes.append({
                    "type": "attachment_links_changed", "url": url, "title": cur.get("title", ""),
                    "attachments": cur.get("attachments", []),
                    "added_attachments": sorted(new_links-old_links),
                    "removed_attachments": sorted(old_links-new_links),
                    "old_checked_at_utc": old.get("checked_at"),
                    "detected_at_utc": cur.get("checked_at", timestamp),
                    "change_summary": ["Изменился список прикреплённых файлов/ссылок."],
                })

    complete = not queue and not deadline_reached and not errors
    report = {
        "generated_at_utc": timestamp,
        "period_start_utc": (load_json(BASELINE_META, {}).get("last_full_scan_utc") or load_json(BASELINE_META, {}).get("created_at_utc")),
        "pages_checked": len(pages),
        "attempted_pages": len(seen),
        "queue_remaining": len(queue),
        "page_limit": MAX_PAGES,
        "page_timeout_ms": PAGE_TIMEOUT_MS,
        "page_wall_timeout_seconds": PAGE_WALL_TIMEOUT_SECONDS,
        "run_timeout_seconds": RUN_TIMEOUT_SECONDS,
        "errors_count": len(errors),
        "errors": errors,
        "skipped_count": len(skipped),
        "skipped": skipped,
        "changes": changes,
        "baseline_ready": baseline_ready,
        "baseline_created_this_run": complete and not baseline_ready,
        "deadline_reached": deadline_reached,
        "complete": complete,
        "note": ("Исходный снимок ещё формируется; обнаруженные URL не интерпретируются как новые публикации или изменения текущего дня. "
                 if not baseline_ready else "") +
                "Этот этап охватывает общедоступные HTML-страницы сайта на русском и казахском языках. Документы и СМИ проверяются отдельным этапом; социальные сети исключены по решению пользователя."
    }
    compact_save(OUT / "report.json", report)
    lines = [
        "# Отчёт мониторинга", "",
        f"- Время UTC: {timestamp}",
        f"- Успешно проверено страниц в этом запуске и восстановленной сессии: {len(pages)}",
        f"- Обработано URL: {len(seen)}",
        f"- Осталось в очереди: {len(queue)}",
        f"- Ошибок: {len(errors)}",
        f"- Полный обход: {'да' if complete else 'нет'}",
        f"- Режим: {'формирование исходного снимка' if not baseline_ready else 'сравнение с предыдущим полным снимком'}",
        f"- Исходный снимок: {'готов' if baseline_ready else ('будет создан после полного обхода' if complete else 'формируется')}",
        "- Охват: только общедоступные HTML-страницы разделов /ru и /kz; query-параметры и фрагменты нормализуются.",
        "", "## Изменения"
    ]
    if not baseline_ready:
        lines.append("- Исходный снимок ещё формируется; впервые обнаруженные URL не считаются публикациями или изменениями текущего дня.")
    elif changes:
        for c in changes:
            lines += ["", f"### {c.get('title') or c['url']}", f"- Тип: {c['type']}", f"- Прямая ссылка: {c['url']}",
                      f"- Обнаружено при проверке: {local_stamp(c.get('detected_at_utc'))}"]
            if c.get("old_checked_at_utc"):
                lines.append(f"- Предыдущая проверка: {local_stamp(c['old_checked_at_utc'])}")
            if c.get("source_last_modified"):
                lines.append(f"- Дата изменения по заголовку сервера: {c['source_last_modified']}")
            if c.get("description"):
                lines.append(f"- Кратко: {c['description']}")
            for detail in c.get("change_summary", []):
                lines.append(f"- Что изменилось: {detail}")
            for link in c.get("added_attachments", []):
                lines.append(f"- Добавленный файл: {link}")
            for link in c.get("removed_attachments", []):
                lines.append(f"- Удалённая ссылка на файл: {link}")
    elif complete:
        lines.append("- Изменений относительно предыдущего полного снимка не обнаружено.")
    else:
        lines.append("- В проверенной части изменения не установлены. Обход неполный — отсутствие изменений по всему сайту не подтверждено.")
    if errors:
        lines += ["", "## Временные ошибки (будут повторены)"] + [f"- {e['url']}: {e['error']}" for e in errors]
    if skipped:
        lines += ["", "## Недоступные страницы и файловые ссылки (не блокируют снимок)"] + [f"- {e['url']}: {e['reason']}" for e in skipped]
    if queue:
        lines += ["", "## Первые 100 URL, ожидающих проверки"]
        lines += [f"- {u}" for u in queue[:100]]
    (OUT / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report


def should_resume_checkpoint(saved):
    """Resume only genuinely unfinished crawls; an empty completed checkpoint starts a fresh scan."""
    return bool(saved) and saved.get("queue") is not None and saved.get("pages") is not None and (
        bool(saved.get("queue")) or bool(saved.get("errors"))
    )


async def main():
    started = time.monotonic()
    deadline = started + RUN_TIMEOUT_SECONDS
    timestamp = now()
    previous = load_json(LATEST, {})
    baseline_meta = load_json(BASELINE_META, {})
    baseline_ready = bool(previous) and baseline_meta.get("schema_version") == BASELINE_SCHEMA_VERSION and baseline_meta.get("complete") is True
    saved = load_checkpoint()
    skipped = list(saved.get("skipped", [])) if saved else []
    retry_counts = dict(saved.get("retry_counts", {})) if saved else {}
    if should_resume_checkpoint(saved):
        pages = {}
        for old_url, value in saved.get("pages", {}).items():
            canonical = clean_url(old_url)
            if canonical:
                pages[canonical] = value
        seen = set()
        for raw in saved.get("seen_urls", []):
            canonical = clean_url(raw)
            if canonical:
                seen.add(canonical)
        queue_list, queued = [], set()
        for raw in saved.get("queue", []):
            canonical = clean_url(raw)
            if canonical and canonical not in seen and canonical not in queued:
                queue_list.append(canonical)
                queued.add(canonical)
        retry_urls = []
        for err in saved.get("errors", []):
            canonical = clean_url(err.get("url", ""))
            if canonical:
                seen.discard(canonical)
                if canonical not in queued:
                    retry_urls.append(canonical)
                    queued.add(canonical)
        queue = deque(retry_urls + queue_list)
        errors = []
        print(f"[RESUME] Возобновление: {len(pages)} уникальных результатов, в очереди {len(queue)} уникальных URL.", flush=True)
    else:
        # A completed checkpoint has no pending queue or errors. Do not reuse its pages
        # as if they were fetched now: start a new crawl against the previous full snapshot.
        skipped, retry_counts = [], {}
        queue = deque(dict.fromkeys(u for u in (clean_url(x) for x in BASES) if u))
        seen, pages, errors = set(), {}, []
        print("[START] Новый обход RU/KZ с базовых языковых разделов.", flush=True)
    queued = set(queue)
    # Feed public HTML URLs discovered through robots.txt/XML sitemaps into the
    # same resumable crawl queue. Document URLs remain handled by extended_sources.py.
    sitemap_file = STATE / "sitemap_urls.json"
    sitemap_urls = load_json(sitemap_file, []) if sitemap_file.exists() else []
    added_from_sitemap = 0
    for raw in sitemap_urls:
        canonical = clean_url(raw)
        if canonical and canonical not in seen and canonical not in queued:
            queue.append(canonical)
            queued.add(canonical)
            added_from_sitemap += 1
    if added_from_sitemap:
        print(f"[SITEMAP SEED] Добавлено URL из карт сайта: {added_from_sitemap}", flush=True)

    def checkpoint(reason):
        elapsed = round(time.monotonic() - started, 1)
        payload = {
            "saved_at_utc": now(), "reason": reason, "elapsed_seconds": elapsed,
            "pages_checked": len(pages), "attempted_pages": len(seen),
            "attempted_this_run": attempted_this_run,
            "errors": errors, "skipped": skipped, "retry_counts": retry_counts,
            "seen_urls": sorted(seen), "queue": list(queue),
            "pages": pages,
        }
        save_checkpoint(payload)
        compact_save(OUT / "progress.json", {
            "saved_at_utc": payload["saved_at_utc"], "reason": reason,
            "elapsed_seconds": elapsed, "pages_checked": len(pages),
            "attempted_pages": len(seen), "attempted_this_run": attempted_this_run,
            "errors_count": len(errors), "queue_remaining": len(queue),
        })
        print(f"[PROGRESS] попыток={len(seen)} успешно={len(pages)} ошибок={len(errors)} "
              f"в_очереди={len(queue)} время={elapsed}s", flush=True)

    deadline_reached = False
    attempted_this_run = 0
    browser = None
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            context = await browser.new_context(
                user_agent="AtyrauUniversityMonitor/1.2 (public-page change monitoring)"
            )
            try:
                last_checkpoint_at = 0
                while queue and attempted_this_run < MAX_PAGES:
                    if time.monotonic() >= deadline:
                        deadline_reached = True
                        print("[STOP] Достигнут лимит времени запуска.", flush=True)
                        break

                    # Обрабатываем небольшие группы страниц параллельно. Очередь,
                    # дедупликация ссылок и сохранение состояния остаются общими.
                    batch = []
                    while queue and len(batch) < MAX_CONCURRENCY and attempted_this_run < MAX_PAGES:
                        url = queue.popleft()
                        queued.discard(url)
                        if url in seen:
                            continue
                        seen.add(url)
                        attempted_this_run += 1
                        batch.append(url)
                    if not batch:
                        break

                    remaining = max(0.1, deadline - time.monotonic())
                    budget = min(PAGE_WALL_TIMEOUT_SECONDS, remaining)
                    results = await asyncio.gather(
                        *(asyncio.wait_for(inspect_page(context, url), timeout=budget) for url in batch),
                        return_exceptions=True,
                    )

                    for url, result in zip(batch, results):
                        if isinstance(result, BaseException):
                            if isinstance(result, asyncio.TimeoutError):
                                message = f"Тайм-аут страницы ({budget:.1f} с)"
                                print(f"[PAGE TIMEOUT] {url}", flush=True)
                            else:
                                message = str(result)[:300]
                            if "Download is starting" in message:
                                pages[url] = {
                                    "title": "Ссылка на скачиваемый документ",
                            "hash": hashlib.sha256(page_text.encode("utf-8")).hexdigest(),
                                    "attachments": [url], "checked_at": now(),
                                }
                                print(f"[DOWNLOAD LINK] Учтена ссылка на скачивание: {url}", flush=True)
                            elif re.search(r"HTTP 4\d\d", message):
                                skipped.append({"url": url, "reason": message})
                                print(f"[SKIP PERMANENT] {url}: {message}", flush=True)
                            else:
                                retry_counts[url] = retry_counts.get(url, 0) + 1
                                if retry_counts[url] < 2:
                                    errors.append({"url": url, "error": message})
                                else:
                                    skipped.append({"url": url, "reason": f"Повторная ошибка: {message}"})
                                print(f"[PAGE ERROR] {url} (попытка {retry_counts[url]}): {message[:180]}", flush=True)
                            continue

                        title, page_text, links, source_last_modified = result
                        attachments = []
                        for link in links:
                            if is_document(link):
                                attachments.append(link)
                            else:
                                target = clean_url(link)
                                if target and target not in seen and target not in queued:
                                    queue.append(target)
                                    queued.add(target)
                        pages[url] = {
                            "title": title,
                            "hash": hashlib.sha256(page_text.encode("utf-8")).hexdigest(),
                            "text": page_text[:3000],
                            "attachments": sorted(set(attachments)),
                            "checked_at": now(),
                            "source_last_modified": source_last_modified,
                        }

                    if attempted_this_run - last_checkpoint_at >= PROGRESS_EVERY:
                        checkpoint("periodic")
                        last_checkpoint_at = attempted_this_run
                    if time.monotonic() >= deadline:
                        deadline_reached = True
                        print("[STOP] Достигнут лимит времени запуска.", flush=True)
                        break
            finally:
                await context.close()
    except Exception as exc:
        errors.append({"url": "(browser/session)", "error": str(exc)[:300]})
        print(f"[FATAL] {str(exc)[:300]}", flush=True)
    finally:
        if browser:
            await browser.close()

    if queue and time.monotonic() >= deadline:
        deadline_reached = True
    checkpoint("final" if not deadline_reached else "time_limit")
    report = build_report(timestamp, pages, errors, list(queue), seen, deadline_reached, previous, skipped, baseline_ready)

    if report["complete"]:
        compact_save(LATEST, pages)
        if not baseline_ready:
            compact_save(BASELINE_META, {
                "schema_version": BASELINE_SCHEMA_VERSION,
                "complete": True,
                "created_at_utc": now(),
                "page_count": len(pages),
            })
            print(f"[BASELINE] Создан первый полный исходный снимок: {len(pages)} страниц.", flush=True)
        else:
            baseline_meta["last_full_scan_utc"] = now()
            baseline_meta["page_count"] = len(pages)
            compact_save(BASELINE_META, baseline_meta)
        CHECKPOINT.unlink(missing_ok=True)
        CHECKPOINT_GZ.unlink(missing_ok=True)
        print("[STATE] Полный снимок сохранён; JSON и gzip контрольные точки удалены.", flush=True)
    else:
        print("[STATE] Обход неполный. Компактная контрольная точка сохранена для продолжения в следующем запуске.", flush=True)

    print(f"[SUMMARY] complete={report['complete']} checked={len(pages)} attempted={len(seen)} "
          f"errors={len(errors)} remaining={len(queue)}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
