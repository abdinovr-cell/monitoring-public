import json
import os
import smtplib
import sys
from datetime import datetime
from email.message import EmailMessage
from email.utils import formatdate
from html import escape
from pathlib import Path
from zoneinfo import ZoneInfo

REPORT = Path("output/report.json")
if not REPORT.exists():
    print("[EMAIL] report.json отсутствует; письмо не отправлено.", flush=True)
    sys.exit(0)

required = ["SMTP_HOST", "SMTP_PORT", "SMTP_USERNAME", "SMTP_PASSWORD", "REPORT_EMAIL_TO"]
missing = [key for key in required if not os.getenv(key)]
if missing:
    print("[EMAIL] Не настроены секреты: " + ", ".join(missing) + ". Письмо не отправлено.", flush=True)
    sys.exit(0)

data = json.loads(REPORT.read_text(encoding="utf-8"))
ext_path = Path("output/extended_report.json")
ext = json.loads(ext_path.read_text(encoding="utf-8")) if ext_path.exists() else {}
tz = ZoneInfo("Asia/Almaty")
generated = datetime.now(tz)
daypart = "Утренний" if generated.hour < 12 else "Вечерний"


def local_time(value):
    if not value:
        return "не указано"
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=ZoneInfo("UTC"))
        return parsed.astimezone(tz).strftime("%d.%m.%Y %H:%M:%S") + " (Атырау, UTC+5)"
    except Exception:
        return str(value)


start_raw = data.get("period_start_utc")
try:
    period_start = datetime.fromisoformat(start_raw.replace("Z", "+00:00")).astimezone(tz) if start_raw else None
except Exception:
    period_start = None
period = f"{period_start:%d.%m.%Y %H:%M} — {generated:%d.%m.%Y %H:%M}, UTC+5, Атырау" if period_start else f"С {generated:%d.%m.%Y %H:%M}, UTC+5, Атырау"
subject = f"Мониторинг АГУ им. Х. Досмухамедова — {generated:%d.%m.%Y} — {daypart.lower()} отчёт"

changes = data.get("changes", [])
errors = data.get("errors", [])
doc_changes = ext.get("document_changes", [])
doc_removed = ext.get("document_removed", [])
news = ext.get("news_changes", [])
social = ext.get("social_changes", [])
social_status = ext.get("social_status", [])
complete = bool(data.get("complete"))
status = "Полный обход завершён" if complete else "Обход неполный — проверка продолжается"
run_url = ""
if os.getenv("GITHUB_SERVER_URL") and os.getenv("GITHUB_REPOSITORY") and os.getenv("GITHUB_RUN_ID"):
    run_url = f"{os.environ['GITHUB_SERVER_URL']}/{os.environ['GITHUB_REPOSITORY']}/actions/runs/{os.environ['GITHUB_RUN_ID']}"

plain = [
    "МОНИТОРИНГ АТЫРАУСКОГО УНИВЕРСИТЕТА ИМ. Х. ДОСМУХАМЕДОВА",
    f"Период: {period}",
    f"Статус основного обхода: {status}",
    f"Проверено страниц: {data.get('pages_checked', 0)}; обработано URL: {data.get('attempted_pages', 0)}",
    f"Осталось URL: {data.get('queue_remaining', 0)}; ошибок загрузки: {len(errors)}",
    "",
    "1. САЙТ УНИВЕРСИТЕТА — НОВЫЕ И ИЗМЕНЁННЫЕ СТРАНИЦЫ И ОБЪЯВЛЕНИЯ",
]


def add_plain_entry(item, heading, url_key="url"):
    url = item.get(url_key) or item.get("key", "")
    title = item.get("title") or url or heading
    plain.extend(["", f"{heading}: {title}", f"Прямая ссылка: {url or 'не установлена'}"])
    if item.get("description"):
        plain.append("Кратко о содержании: " + str(item["description"])[:1200])
    if item.get("summary"):
        plain.append("Кратко о содержании файла: " + str(item["summary"])[:1200])
    if item.get("published"):
        plain.append("Дата публикации по источнику: " + str(item["published"]))
    if item.get("source"):
        plain.append("Источник: " + str(item["source"]))
    if item.get("old_checked_at_utc") or item.get("previous_checked_at_utc"):
        plain.append("Предыдущая проверка: " + local_time(item.get("old_checked_at_utc") or item.get("previous_checked_at_utc")))
    if item.get("source_last_modified"):
        plain.append("Дата изменения по заголовку сервера: " + str(item["source_last_modified"]))
    detected = item.get("detected_at_utc") or item.get("checked_at")
    if detected:
        plain.append("Изменение обнаружено при проверке: " + local_time(detected))
    for detail in item.get("change_summary", []):
        plain.append("Что изменилось: " + str(detail)[:1600])
    for link in item.get("added_attachments", []):
        plain.append("Добавленный файл: " + link)
    for link in item.get("removed_attachments", []):
        plain.append("Удалённая ссылка на файл: " + link)
    if item.get("text_extraction_warning"):
        plain.append("Ограничение извлечения текста: " + str(item["text_extraction_warning"]))


if changes:
    for item in changes:
        add_plain_entry(item, {
            "new_page": "НОВАЯ СТРАНИЦА/ПУБЛИКАЦИЯ",
            "changed_page": "ИЗМЕНЁННАЯ СТРАНИЦА/ПУБЛИКАЦИЯ",
            "attachment_links_changed": "ИЗМЕНЕНИЕ СПИСКА ФАЙЛОВ",
        }.get(item.get("type"), "ИЗМЕНЕНИЕ"))
else:
    plain.append("Изменения в проверенных страницах не обнаружены.")

plain += [
    "",
    "1.1. ДОКУМЕНТЫ И ФАЙЛЫ — ДОБАВЛЕННЫЕ И ИЗМЕНЁННЫЕ",
    f"Документов в очереди/инвентаре: {ext.get('documents_discovered', 0)}; обработано в этом запуске: {ext.get('documents_checked_this_run', 0)}; осталось: {ext.get('documents_pending', 0)}",
]
if doc_changes:
    for item in doc_changes:
        add_plain_entry(item, "НОВЫЙ ФАЙЛ" if item.get("type") == "new" else "ИЗМЕНЁННЫЙ ФАЙЛ", "key")
else:
    plain.append("Новых или изменённых файлов не обнаружено.")
for url in doc_removed:
    plain.append("Файл больше не обнаруживается в текущем перечне: " + str(url))

plain += ["", "2. СОЦИАЛЬНЫЕ СЕТИ"]
for item in social_status:
    state_text = "публичная страница получена" if item.get("checked") else "публичное содержимое недоступно"
    plain.append(f"{item.get('platform', 'Соцсеть')}: {state_text}; найдено permalink: {item.get('candidate_posts', 0)}; публикаций за последние 24 часа: {item.get('items', 0)}.")
    if item.get("reason"):
        plain.append("Комментарий: " + str(item["reason"]))
if social:
    for item in social:
        add_plain_entry(item, "НОВАЯ ПУБЛИКАЦИЯ В СОЦИАЛЬНОЙ СЕТИ", "url")
else:
    plain.append("Подтверждённых новых публикаций за последние 24 часа не обнаружено.")

plain += ["", "3. УПОМИНАНИЯ В СМИ И ИНТЕРНЕТЕ"]
if news:
    for item in news:
        add_plain_entry(item, "НОВАЯ ПУБЛИКАЦИЯ В СМИ", "url")
else:
    plain.append("Новые публикации не обнаружены в проверенных RSS-источниках.")

plain += [
    "",
    "4. ИТОГ И ОГРАНИЧЕНИЯ",
    f"Основной обход: {status}",
    f"Документный этап: {'завершён' if ext.get('complete') else 'не завершён/частичный'}",
    "Социальные сети проверяются только по публично доступным страницам; авторизация, капча и ограничения доступа не обходятся.",
    "Время в отчёте — время обнаружения изменения при проверке (Атырау, UTC+5), а не обязательно точное время фактического изменения на сайте.",
    "Если сайт не публикует дату изменения, точное время изменения установить нельзя; предыдущая проверка показывает последнюю дату, когда старая версия ещё была зафиксирована.",
]
if errors:
    plain += ["", "Ошибки основного обхода (первые 20):"]
    plain += [f"- {x.get('url', '')}: {str(x.get('error', ''))[:250]}" for x in errors[:20]]
if run_url:
    plain += ["", f"Полный отчёт и файлы запуска: {run_url}"]

html = [
    '<html><body style="font-family:Arial,sans-serif;line-height:1.5;color:#222">',
    '<h2>Мониторинг Атырауского университета им. Х. Досмухамедова</h2>',
    f'<p><b>Период:</b> {escape(period)}<br><b>Статус:</b> {escape(status)}<br>',
    f'Проверено страниц: {data.get("pages_checked", 0)}; осталось URL: {data.get("queue_remaining", 0)}; ошибок: {len(errors)}</p>',
]


def add_html_entry(item, heading, url_key="url"):
    url = item.get(url_key) or item.get("key", "")
    title = item.get("title") or url or heading
    html.append(f'<h4>{escape(heading)}: {escape(str(title))}</h4>')
    if url.startswith(("http://", "https://")):
        html.append(f'<p><b>Прямая ссылка:</b> <a href="{escape(url, quote=True)}">{escape(url)}</a></p>')
    else:
        html.append(f'<p><b>Прямая ссылка:</b> {escape(url or "не установлена")}</p>')
    details = []
    if item.get("description"):
        details.append("<b>Кратко:</b> " + escape(str(item["description"])[:1200]))
    if item.get("summary"):
        details.append("<b>О содержании файла:</b> " + escape(str(item["summary"])[:1200]))
    if item.get("published"):
        details.append("<b>Дата публикации по источнику:</b> " + escape(str(item["published"])))
    if item.get("source"):
        details.append("<b>Источник:</b> " + escape(str(item["source"])))
    if item.get("old_checked_at_utc") or item.get("previous_checked_at_utc"):
        details.append("<b>Предыдущая проверка:</b> " + escape(local_time(item.get("old_checked_at_utc") or item.get("previous_checked_at_utc"))))
    if item.get("source_last_modified"):
        details.append("<b>Дата изменения по заголовку сервера:</b> " + escape(str(item["source_last_modified"])))
    detected = item.get("detected_at_utc") or item.get("checked_at")
    if detected:
        details.append("<b>Обнаружено:</b> " + escape(local_time(detected)))
    for detail in item.get("change_summary", []):
        details.append("<b>Что изменилось:</b> " + escape(str(detail)[:1600]))
    for link in item.get("added_attachments", []):
        details.append('<b>Добавленный файл:</b> <a href="' + escape(link, quote=True) + '">' + escape(link) + '</a>')
    for link in item.get("removed_attachments", []):
        details.append("<b>Удалённая ссылка на файл:</b> " + escape(link))
    if item.get("text_extraction_warning"):
        details.append("<b>Ограничение извлечения текста:</b> " + escape(str(item["text_extraction_warning"])))
    if details:
        html.append("<p>" + "<br>".join(details) + "</p>")


html.append("<h3>1. Сайт университета — новые и изменённые страницы/объявления</h3>")
if changes:
    for item in changes:
        add_html_entry(item, {
            "new_page": "Новая страница/публикация",
            "changed_page": "Изменённая страница/публикация",
            "attachment_links_changed": "Изменение списка файлов",
        }.get(item.get("type"), "Изменение"))
else:
    html.append("<p>Изменения в проверенных страницах не обнаружены.</p>")

html.append("<h3>1.1. Документы и файлы — добавленные и изменённые</h3>")
html.append(f'<p>Обнаружено: {ext.get("documents_discovered", 0)}; обработано сейчас: {ext.get("documents_checked_this_run", 0)}; осталось: {ext.get("documents_pending", 0)}.</p>')
if doc_changes:
    for item in doc_changes:
        add_html_entry(item, "Новый файл" if item.get("type") == "new" else "Изменённый файл", "key")
else:
    html.append("<p>Новых или изменённых файлов не обнаружено.</p>")
for url in doc_removed:
    html.append("<p>Файл больше не обнаруживается в текущем перечне: " + escape(str(url)) + "</p>")

html.append("<h3>2. Социальные сети</h3>")
for item in social_status:
    state_text = "публичная страница получена" if item.get("checked") else "публичное содержимое недоступно"
    html.append(f'<p><b>{escape(str(item.get("platform", "Соцсеть")))}:</b> {escape(state_text)}; найдено permalink: {item.get("candidate_posts", 0)}; публикаций за последние 24 часа: {item.get("items", 0)}.')
    if item.get("reason"):
        html.append("<br>" + escape(str(item["reason"])))
    html.append("</p>")
if social:
    for item in social:
        add_html_entry(item, "Новая публикация в социальной сети", "url")
else:
    html.append("<p>Подтверждённых новых публикаций за последние 24 часа не обнаружено.</p>")

html.append("<h3>3. Упоминания в СМИ и интернете</h3>")
if news:
    for item in news:
        add_html_entry(item, "Новая публикация в СМИ", "url")
else:
    html.append("<p>Новые публикации не обнаружены в проверенных RSS-источниках.</p>")

html += [
    "<h3>4. Итог и ограничения</h3>",
    f'<p>Основной обход: {escape(status)}<br>Документный этап: {"завершён" if ext.get("complete") else "не завершён/частичный"}<br>',
    "Социальные сети проверяются только по публично доступным страницам; авторизация, капча и ограничения доступа не обходятся.<br>",
    "Время указано как время обнаружения изменения при проверке (Атырау, UTC+5), а не обязательно точное время фактического изменения.<br>",
    "Если сайт не публикует дату изменения, точное время фактического изменения установить нельзя.</p>",
]
if run_url:
    html.append(f'<p><a href="{escape(run_url, quote=True)}">Открыть полный отчёт и файлы запуска в GitHub Actions</a></p>')
html.append("</body></html>")

msg = EmailMessage()
msg["Subject"] = subject
msg["From"] = os.getenv("SMTP_FROM") or os.getenv("SMTP_USERNAME")
msg["To"] = os.environ["REPORT_EMAIL_TO"]
msg["Date"] = formatdate(localtime=True)
msg.set_content("\n".join(plain))
msg.add_alternative("".join(html), subtype="html")

host, port = os.environ["SMTP_HOST"], int(os.environ["SMTP_PORT"])
if port == 465:
    with smtplib.SMTP_SSL(host, port, timeout=30) as server:
        server.login(os.environ["SMTP_USERNAME"], os.environ["SMTP_PASSWORD"])
        server.send_message(msg)
else:
    with smtplib.SMTP(host, port, timeout=30) as server:
        server.starttls()
        server.login(os.environ["SMTP_USERNAME"], os.environ["SMTP_PASSWORD"])
        server.send_message(msg)
print(f"[EMAIL] Подробный отчёт отправлен на {os.environ['REPORT_EMAIL_TO']}; страниц: {len(changes)}, документов: {len(doc_changes)}, публикаций: {len(news)}.", flush=True)
