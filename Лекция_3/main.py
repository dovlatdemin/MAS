# main.py
"""Точка входа мульти-агентного трекера публикаций (оркестратор).

Планировщик запускает скрапер с настраиваемым интервалом, затем для каждой
новой записи:
  1. Берёт аннотацию или загружает HTML-страницу публикации.
  2. Извлекает текст с помощью BeautifulSoup.
  3. Строит резюме агентом-суммаризатором.
  4. При включённой почте отправляет резюме агентом-нотификатором.

Планировщик — ``APScheduler`` в фоновом потоке, поэтому скрипт можно
запускать напрямую (`python main.py`).
"""

import logging
import sys
import time
from datetime import date
from typing import List, Dict, Optional
import requests
from bs4 import BeautifulSoup
# Попытка импортировать APScheduler; если не установлен, используем простой fallback
try:
    from apscheduler.schedulers.background import BackgroundScheduler
except ImportError:
    # Минимальная заглушка, имитирующая API Scheduler
    class _DummyScheduler:
        def add_job(self, func, trigger, **kwargs):
            # При вызове сразу исполняем функцию (один раз)
            func()
        def start(self):
            pass
        def shutdown(self):
            pass
    BackgroundScheduler = _DummyScheduler

# Локальные модули проекта
from utils import load_config, logger as base_logger, extract_text_from_url, resolve_date_range
from scraper import get_new_items, SOURCE_NAMES
from summarizer import (
    LlmUnavailable,
    llm_unavailable_message,
    normalize_engine,
    resolve_llm,
    summarize,
)
from notifier import send_email

# Дочерний логгер: сообщения передаются родителю «archiv_tracker» (utils.py),
# у которого настроены вывод в терминал и в main.log – свои обработчики не нужны.
logger = logging.getLogger("archiv_tracker.main")


def extract_text_from_url(url: str) -> str:
    """Загрузить *url* и вернуть видимый текст страницы.

    Простейший извлекатель: удаляет теги script/style/noscript и склеивает
    оставшиеся фрагменты текста. Для PDF понадобился бы другой извлекатель,
    но у archive.org обычно есть HTML-страница details, текста которой
    достаточно для короткого резюме.
    """
    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        text = " ".join(soup.stripped_strings)
        return text
    except Exception as e:
        logger.error(f"Failed to extract text from {url}: {e}")
        return ""


def process_items(
    topics: Optional[List[str]] = None,
    sources: Optional[List[str]] = None,
    rows: int = 20,
    skip_seen: bool = True,
    save_history: bool = True,
    max_sentences: int = 3,
    text_limit: int = 2000,
    engine: Optional[str] = None,
    llm: Optional[Dict] = None,
    send: Optional[bool] = None,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    numdam_options: Optional[Dict] = None,
) -> List[Dict]:
    """Основной цикл обработки – вызывается планировщиком и панелью Streamlit.

    1. Получить новые записи от подключённых агентов-источников (archive.org, arXiv …).
    2. Для каждой записи взять аннотацию (или текст страницы) и построить резюме.
    3. При необходимости отправить резюме на почту.

    ``send=None`` означает «взять значение ``send_email`` из config.yaml»
    (по умолчанию ``False`` – письма не отправляются, пока это явно не включено).

    Возвращает список словарей: source, identifier, title, url, summary, status …
    """
    t0 = time.time()
    cfg = load_config()
    if topics is None:
        topics = cfg.get("topics", [])
    if sources is None:
        sources = cfg.get("sources", list(SOURCE_NAMES))
    date_from, date_to = resolve_date_range(date_from=date_from, date_to=date_to)
    engine = normalize_engine(engine if engine is not None else cfg.get("summarizer"))
    if engine != "sumy" and llm is None:
        llm = resolve_llm(cfg, engine)
    process_items.last_llm_notice = None
    process_items.last_source_errors = []
    llm_checked = False
    if send is None:
        send = bool(cfg.get("send_email", False))
    email_cfg = cfg.get("email", {})
    if send and not email_cfg:
        logger.error("Настройки почты не заданы – письма отправляться не будут.")
        send = False

    logger.info("▶️ Запуск цикла обработки")
    logger.info(f"   Темы: {', '.join(topics) or '—'}")
    logger.info(f"   Источники: {', '.join(SOURCE_NAMES.get(s, s) for s in sources) or '—'}")
    logger.info(f"   Даты: {date_from.isoformat()} — {date_to.isoformat()}")
    if engine == "sumy":
        backend = "Sumy"
    else:
        backend = (llm or {}).get("model") or engine
    logger.info(
        f"   Записей с источника: {rows}; предложений в резюме: {max_sentences}; "
        f"резюме: {backend}; лимит текста: {text_limit}; почта: {'вкл' if send else 'выкл'}"
    )

    new_items = get_new_items(
        topics=topics, sources=sources, rows=rows,
        skip_seen=skip_seen, save_history=save_history,
        date_from=date_from, date_to=date_to,
        numdam_options=numdam_options,
    )
    process_items.last_source_errors = getattr(get_new_items, "last_errors", [])
    results: List[Dict] = []
    if not new_items:
        logger.info(f"⏹ Новых публикаций нет. Цикл завершён за {time.time() - t0:.1f} с")
        return results

    logger.info(f"━━━━━━━━ Суммаризация: {len(new_items)} публикаций ━━━━━━━━")
    for n, item in enumerate(new_items, start=1):
        src = SOURCE_NAMES.get(item.get("source"), item.get("source", "?"))
        logger.info(f"📄 [{n}/{len(new_items)}] ({src}) {item['title'][:100]}")
        result = dict(item, summary="", status="")

        abstract = item.get("abstract") or ""
        if item.get("source") == "numdam":
            raw_text = abstract[:text_limit]
            if not raw_text.strip():
                result["status"] = "Аннотация отсутствует — резюме недоступно"
                results.append(result)
                continue
        elif len(abstract) >= 200:
            raw_text = abstract[:text_limit]
            logger.info(f"   Текст: аннотация из метаданных, {len(raw_text)} символов")
        else:
            t1 = time.time()
            logger.info(f"   Загрузка страницы {item['url']} …")
            raw_text = extract_text_from_url(item["url"])[:text_limit]  # обрезаем текст ради скорости
            logger.info(f"   Текст: со страницы, {len(raw_text)} символов ({time.time() - t1:.1f} с)")
        if not raw_text:
            logger.warning(f"   ⚠️ Текст не получен – публикация пропущена")
            result["status"] = "Не удалось извлечь текст"
            results.append(result)
            continue

        try:
            if engine != "sumy" and not llm_checked:
                llm_checked = True
                notice = llm_unavailable_message(llm)
                if notice:
                    process_items.last_llm_notice = notice
                    logger.warning(notice)
                    engine = "sumy"
            try:
                summary = summarize(
                    raw_text, max_sentences=max_sentences, engine=engine, llm=llm,
                )
            except LlmUnavailable as exc:
                if not process_items.last_llm_notice:
                    process_items.last_llm_notice = exc.message
                    logger.warning(exc.message)
                engine = "sumy"
                summary = summarize(raw_text, max_sentences=max_sentences, engine="sumy")
        except Exception as exc:
            logger.warning("Не удалось составить резюме: %s", exc)
            result["status"] = f"Резюме недоступно: {exc}"
            results.append(result)
            continue
        result["summary"] = summary
        logger.info(f"   ✍️ Резюме готово: {len(summary)} символов")
        if send:
            subject = f"Новая публикация ({src}): {item['title']}"
            body = f"URL: {item['url']}\n\nРезюме:\n{summary}\n"
            success = send_email(subject, body, email_cfg)
            result["status"] = "Отправлено на почту" if success else "Ошибка отправки"
            logger.info(f"   ✉️ {result['status']}")
        else:
            result["status"] = "Не отправлялось (тестовый режим)"
            logger.info("   ✉️ Почта выключена – письмо не отправлялось")
        results.append(result)

    logger.info(
        f"⏹ Цикл завершён за {time.time() - t0:.1f} с: обработано {len(results)}, "
        f"с резюме {sum(1 for r in results if r['summary'])}"
    )
    return results


def main():
    cfg = load_config()
    interval = cfg.get("check_interval_seconds", 3600)
    scheduler = BackgroundScheduler()
    # Если используется dummy‑scheduler, add_job сразу выполнит задачу один раз.
    scheduler.add_job(process_items, "interval", seconds=interval, id="archive_check", next_run_time=None)
    logger.info(f"Scheduler started – checking every {interval} seconds.")
    scheduler.start()
    # Если это реальный APScheduler, он будет работать в фоне; иначе – просто завершим процесс.
    try:
        import time
        while True:
            time.sleep(60)
    except (KeyboardInterrupt, SystemExit):
        logger.info("Shutting down scheduler…")
        scheduler.shutdown()

if __name__ == "__main__":
    main()
