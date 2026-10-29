# streamlit_app.py
"""Streamlit-интерфейс мульти-агентного трекера публикаций archive.org.

Возможности:
- Настройки запуска находятся в боковой панели, провайдеры модели — на отдельной вкладке.
- Кнопка ручного запуска одного цикла обработки (поиск → извлечение текста → резюме).
- Найденные публикации и их резюме выводятся прямо в интерфейс.
- Схема агентов открывается на отдельной вкладке.
- Отправка на почту в тестовом режиме отключена.
"""

from pathlib import Path
from datetime import date

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components
import numdam_agent
from numdam_index import NumdamIndex

# Импорт основной функции обработки из main.py (файлы лежат в одной папке).
from main import process_items
from summarizer import llm_unavailable_message
from scraper import SOURCE_NAMES
from utils import HISTORY_PATH, get_history, load_config, resolve_date_range, save_model_settings

PROVIDER_LABELS = {
    "sumy": "Sumy",
    "ollama": "Ollama",
    "openai": "OpenAI-совместимый",
}
PROVIDER_DEFAULTS = {
    "ollama": {
        "base_url": "http://localhost:11434/v1",
        "model": "qwen2.5:7b",
        "api_key_env": "",
    },
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
        "api_key_env": "OPENAI_API_KEY",
    },
}


def _drafts_from_config(cfg: dict) -> dict:
    stored = cfg.get("providers") or {}
    llm = cfg.get("llm") or {}
    active = str(cfg.get("summarizer", "sumy")).strip().lower()
    drafts = {}
    for name, defaults in PROVIDER_DEFAULTS.items():
        draft = dict(defaults)
        saved = stored.get(name)
        source = saved if isinstance(saved, dict) and saved else (llm if active == name else {})
        for key in ("base_url", "model", "api_key_env"):
            if source.get(key):
                draft[key] = source[key]
        drafts[name] = draft
    return drafts


def _field(name: str, field: str) -> str:
    key = f"{field}_{name}"
    if key in st.session_state:
        return str(st.session_state[key] or "")
    return str(st.session_state["provider_drafts"][name].get(field) or "")


def _active_llm() -> dict:
    name = st.session_state["provider"]
    if name == "sumy":
        return {}
    data = {
        "base_url": _field(name, "base_url").strip(),
        "model": _field(name, "model").strip(),
        "api_key_env": _field(name, "api_key_env").strip(),
    }
    secret = _field(name, "api_key").strip()
    if secret:
        data["api_key"] = secret
    return data


def _collect_providers() -> dict:
    providers = {}
    for name in PROVIDER_DEFAULTS:
        entry = {
            "base_url": _field(name, "base_url").strip(),
            "model": _field(name, "model").strip(),
        }
        env_name = _field(name, "api_key_env").strip()
        if env_name:
            entry["api_key_env"] = env_name
        providers[name] = entry
    return providers


def _provider_input(label: str, name: str, field: str, **kwargs):
    key = f"{field}_{name}"
    if key not in st.session_state:
        st.session_state[key] = st.session_state["provider_drafts"][name].get(field) or ""
    st.text_input(label, key=key, **kwargs)


@st.cache_resource
def _numdam_job(path):
    return numdam_agent.SyncJob(path)


@st.fragment(run_every=2)
def render_numdam_index():
    st.header("Индекс NUMDAM")
    st.caption("Поиск BM25 работает по сохранённым названиям, аннотациям и ключевым словам. "
               "Сначала загрузите метаданные; последующие поиски используют локальный индекс.")
    path = str(numdam_agent.settings()["db_path"])
    index = NumdamIndex(path)
    state = index.status()
    job = _numdam_job(path)
    runtime = job.snapshot()
    # A fragment rerun must also refresh the sidebar and the search button when
    # the first page arrives or the catalogue finishes importing.
    readiness = (path, state["count"], state.get("complete"), state.get("pending"))
    previous = st.session_state.get("numdam_index_readiness")
    st.session_state["numdam_index_readiness"] = readiness
    if previous is not None and previous != readiness:
        st.rerun()
    c1, c2, c3 = st.columns(3)
    c1.metric("Статей в индексе", state["count"])
    c2.metric("Без аннотации", state["no_abstract"])
    c3.metric("Год неизвестен", state["unknown_years"])
    st.caption("Последняя успешная синхронизация: " + (state.get("last_success") or "ещё не завершалась"))
    if not state["count"]:
        st.info("Индекс пуст. Запустите синхронизацию, чтобы искать статьи NUMDAM.")
    elif not state.get("complete"):
        st.warning("Индекс заполнен частично. Поиск охватывает только загруженные статьи.")
    elif state.get("pending"):
        st.warning("Полный каталог был загружен ранее; текущее обновление ещё не завершено.")
    else:
        st.success("Импорт каталога завершён. Доступен поиск по всем проиндексированным статьям.")
    streams = state.get("streams", {})
    if streams:
        names = {"articles": "Статьи и аннотации", "dates": "Годы публикаций"}
        for name, cursor in streams.items():
            st.caption(f"{names[name]}: обработано {cursor['processed']} из {cursor.get('total') or '?'} записей")
        if all(cursor.get("total") is not None for cursor in streams.values()):
            total = sum(cursor["total"] for cursor in streams.values())
            done = sum(cursor["processed"] for cursor in streams.values())
            st.progress(min(1.0, done / total) if total else 1.0)
    if runtime["running"]:
        st.info("Остановка после текущей страницы…" if runtime["stopping"] else
                "Синхронизация выполняется в фоне. Можно искать по уже сохранённым данным.")
    elif state.get("pending") and not (runtime["error"] or state.get("error")):
        st.caption("Сохранена позиция импорта. Следующий запуск продолжит загрузку.")
    if runtime["error"] or state.get("error"):
        st.error(runtime["error"] or state["error"])
    limited = st.checkbox("Ограниченный импорт для проверки", value=True, disabled=runtime["running"], key="numdam_limited")
    max_pages = None
    if limited:
        max_pages = int(st.number_input("Максимум страниц API за запуск", min_value=1, max_value=1000,
                                       value=2, disabled=runtime["running"], key="numdam_pages",
                                       help="Два формата загружаются по очереди: одна страница статей, одна — годов публикации."))
    else:
        st.caption("Будет загружен весь каталог или завершено его обновление. Загрузку можно остановить и продолжить.")
    if st.button("Синхронизировать метаданные", type="primary", disabled=runtime["running"], key="numdam_sync"):
        job.start(max_pages=max_pages)
        st.rerun()
    if st.button("Остановить после текущей страницы", disabled=not runtime["running"], key="numdam_stop"):
        job.stop()
        st.rerun()

st.set_page_config(
    page_title="Трекер публикаций",
    page_icon="📚",
    layout="wide",
)

cfg = load_config()
if "provider_drafts" not in st.session_state:
    st.session_state["provider_drafts"] = _drafts_from_config(cfg)
    for name in PROVIDER_DEFAULTS:
        for field in ("base_url", "model", "api_key_env", "api_key"):
            st.session_state.pop(f"{field}_{name}", None)
if "provider" not in st.session_state:
    saved_provider = str(cfg.get("summarizer", "sumy")).strip().lower()
    st.session_state["provider"] = saved_provider if saved_provider in PROVIDER_LABELS else "sumy"

# ---------------------------------------------------------------------------
# Боковая панель: настройки
# ---------------------------------------------------------------------------
with st.sidebar:
    st.header("⚙️ Настройки")

    st.subheader("Поиск")
    topics_text = st.text_area(
        "Темы для отслеживания (по одной в строке)",
        value="\n".join(cfg.get("topics", [])),
        height=120,
        help="По умолчанию берутся из config.yaml. Изменения действуют только в этом сеансе.",
    )
    topics = [t.strip() for t in topics_text.splitlines() if t.strip()]

    available_sources = list(SOURCE_NAMES)
    default_sources = [
        sid for sid in cfg.get("sources", available_sources) if sid in SOURCE_NAMES
    ] or available_sources
    sources = st.multiselect(
        "Подключённые источники",
        options=available_sources,
        default=default_sources,
        format_func=lambda sid: SOURCE_NAMES.get(sid, sid),
        help=(
            "Какие агенты-источники опрашивать. "
            "По умолчанию берутся из config.yaml. Изменения действуют только в этом сеансе."
        ),
    )

    has_recent_sources = any(sid != "numdam" for sid in sources)
    period = st.selectbox(
        "Даты archive.org и arXiv",
        options=["week", "month", "custom"],
        index=0,
        disabled=not has_recent_sources,
        format_func=lambda key: {
            "week": "Неделя",
            "month": "Месяц",
            "custom": "Произвольный",
        }[key],
        help=(
            "Ограничение по дате в поисковом запросе. "
            "Неделя — последние 7 дней, месяц — с этой даты месяц назад. "
            "Обе границы включаются. По умолчанию — неделя."
        ),
    )
    if period == "custom":
        default_from, default_to = resolve_date_range(preset="week")
        date_from = st.date_input("С", value=default_from, disabled=not has_recent_sources)
        date_to = st.date_input("По", value=default_to, disabled=not has_recent_sources)
    else:
        date_from, date_to = resolve_date_range(preset=period)
        st.caption(
            f"{date_from.strftime('%d.%m.%Y')} — {date_to.strftime('%d.%m.%Y')}"
        )
    dates_ok = not has_recent_sources or bool(date_from and date_to and date_from <= date_to)
    if not dates_ok:
        st.warning("Дата «С» должна быть не позже даты «По».")

    numdam_options = {}
    numdam_years_ok = True
    numdam_ready = True
    if "numdam" in sources:
        st.subheader("NUMDAM · BM25")
        st.caption("Лексический поиск по метаданным; результаты NUMDAM упорядочены по релевантности.")
        all_years = st.checkbox("Все годы NUMDAM", value=True, key="numdam_all_years")
        if not all_years:
            year_from = int(st.number_input("Год с", min_value=1, max_value=9999, value=1900, key="numdam_year_from"))
            year_to = int(st.number_input("Год по", min_value=1, max_value=9999, value=date.today().year, key="numdam_year_to"))
            numdam_options.update(year_from=year_from, year_to=year_to)
            numdam_years_ok = year_from <= year_to
            if not numdam_years_ok:
                st.warning("Год «с» должен быть не позже года «по».")
        numdam_options["include_unknown_year"] = st.checkbox("Включать статьи с неизвестным годом", value=True,
            key="numdam_unknown_year", help="Такие статьи будут включены даже при ограничении диапазона годов.")
        numdam_state = NumdamIndex(numdam_agent.settings()["db_path"]).status()
        numdam_ready = numdam_state["count"] > 0
        if not numdam_ready:
            st.info("Откройте вкладку «Индекс NUMDAM» и загрузите метаданные.")
        elif not numdam_state.get("complete"):
            st.warning(f"Частичный индекс: {numdam_state['count']} статей. Продолжить загрузку можно на вкладке «Индекс NUMDAM».")

    rows = st.slider(
        "Количество результатов с источника",
        min_value=5, max_value=100, value=20, step=5,
        help="Для NUMDAM — лучшие совпадения BM25. Для archive.org и arXiv — лимит запрашиваемых записей.",
    )

    st.subheader("Резюме")
    engine = st.session_state["provider"]
    if engine == "sumy":
        provider_caption = "Провайдер: Sumy"
    else:
        provider_caption = (
            f"Провайдер: {PROVIDER_LABELS[engine]} · {_field(engine, 'model') or '—'}"
        )
    st.caption(f"{provider_caption}. Меняется на вкладке «Провайдеры».")
    max_sentences = st.slider(
        "Предложений в резюме",
        1,
        10,
        3,
        help=(
            "Сколько предложений вырезать из текста."
            if engine == "sumy"
            else "Лимит предложений в инструкции для модели."
        ),
    )
    text_limit = st.slider(
        "Лимит текста для резюме (символов)",
        min_value=500, max_value=10000, value=2000, step=500,
        help="Сколько символов аннотации или текста страницы передавать суммаризатору. NUMDAM использует только аннотацию.",
    )

    st.subheader("История")
    skip_seen = st.checkbox(
        "Пропускать уже просмотренные",
        value=False,
        help="Если включено – публикации из history.json не показываются повторно.",
    )
    save_history = st.checkbox(
        "Сохранять найденное в историю",
        value=False,
        help="Для проверки лучше выключить, чтобы одни и те же результаты можно было получать снова.",
    )
    st.caption(f"В истории сейчас записей: **{len(get_history())}**")
    if st.button("🗑 Очистить историю", use_container_width=True):
        HISTORY_PATH.write_text("[]", encoding="utf-8")
        st.success("История очищена.")
        st.rerun()

    st.subheader("Почта")
    st.checkbox(
        "Отправлять резюме на почту",
        value=False,
        disabled=True,
        help="Отключено на время проверки. Результаты выводятся только в интерфейс.",
    )
    st.caption(f"Получатель: {cfg.get('email', {}).get('to_address', '—')}")

    st.subheader("Планировщик")
    st.caption(
        f"Интервал автоматической проверки: "
        f"**{cfg.get('check_interval_seconds', 3600)} с** (задаётся в config.yaml)"
    )

    with st.expander("Полная конфигурация (config.yaml)"):
        safe_cfg = dict(cfg)
        if "email" in safe_cfg:
            safe_cfg["email"] = {
                k: ("••••••" if k == "password" else v)
                for k, v in safe_cfg["email"].items()
            }
        st.json(safe_cfg)

# ---------------------------------------------------------------------------
# Основная область
# ---------------------------------------------------------------------------
st.title("📚 Трекер публикаций")
st.caption(
    "Поиск публикаций в выбранных источниках и создание резюме. "
    "🧪 Тестовый режим: письма не отправляются."
)

tab_search, tab_index, tab_providers, tab_diagram = st.tabs(
    ["🔎 Поиск", "🗃 Индекс NUMDAM", "🤖 Провайдеры", "🗺 Схема агентов"]
)

with tab_index:
    render_numdam_index()

with tab_search:
    run = st.button(
        "🚀 Запустить поиск сейчас",
        key="run_search",
        type="primary",
        disabled=not topics or not sources or not dates_ok or not numdam_years_ok or (not has_recent_sources and not numdam_ready),
    )
    if not topics:
        st.warning("Укажите хотя бы одну тему в боковой панели.")
    elif not sources:
        st.warning("Подключите хотя бы один источник в боковой панели.")
    elif not dates_ok:
        st.warning("В боковой панели дата «С» должна быть не позже даты «По».")

    if run:
        with st.spinner("Агенты работают: ищем публикации и составляем резюме…"):
            try:
                st.session_state["llm_notice"] = None
                st.session_state["results"] = process_items(
                    topics=topics,
                    sources=sources,
                    rows=rows,
                    skip_seen=skip_seen,
                    save_history=save_history,
                    max_sentences=max_sentences,
                    text_limit=text_limit,
                    engine=engine,
                    llm=_active_llm() or None,
                    date_from=date_from,
                    date_to=date_to,
                    numdam_options=numdam_options,
                    send=False,  # пока только вывод в интерфейс
                )
                st.session_state["run_error"] = None
                st.session_state["llm_notice"] = process_items.last_llm_notice
                st.session_state["source_errors"] = process_items.last_source_errors
            except Exception as e:
                st.session_state["results"] = []
                st.session_state["run_error"] = str(e)

    if st.session_state.get("run_error"):
        st.error(f"Ошибка при обработке: {st.session_state['run_error']}")
    if st.session_state.get("llm_notice"):
        st.warning(st.session_state["llm_notice"])
    for source_error in st.session_state.get("source_errors", []):
        st.warning(source_error)

    results = st.session_state.get("results")
    if results is not None and not st.session_state.get("run_error"):
        st.header("Найденные публикации")
        with_summary = sum(1 for r in results if r.get("summary"))
        c1, c2, c3 = st.columns(3)
        c1.metric("Найдено", len(results))
        c2.metric("С резюме", with_summary)
        c3.metric("Отправлено на почту", 0)

        if not results:
            st.info(
                "Новых релевантных публикаций не найдено. "
                "Попробуйте выключить «Пропускать уже просмотренные» или изменить темы."
            )
        else:
            tab_cards, tab_table = st.tabs(["🗂 Карточки", "📋 Таблица"])
            with tab_cards:
                for i, r in enumerate(results, start=1):
                    title = r.get("title") or "(без названия)"
                    if isinstance(title, list):
                        title = " ".join(title)
                    with st.expander(f"{i}. {title}", expanded=i <= 3):
                        src = SOURCE_NAMES.get(r.get("source"), r.get("source") or "—")
                        st.markdown(f"🔗 [{r['url']}]({r['url']})")
                        st.markdown(f"**Источник:** {src}")
                        st.markdown(f"**Идентификатор:** `{r['identifier']}`")
                        if r.get("source") == "numdam":
                            st.write("Авторы:", r.get("authors") or "не указаны")
                            st.write("Год:", r.get("publication_year") or "неизвестен")
                            st.write("Журнал:", r.get("journal") or "не указан")
                            st.write(f"Оценка BM25: {r['bm25_score']:.6g}")
                            st.caption("Больше — релевантнее для этого запроса. Оценка не является вероятностью или процентом.")
                            st.write("Совпавшие термины:", ", ".join(r.get("matched_terms", [])))
                            st.markdown("**Аннотация:**")
                            st.write(r.get("abstract") or "Аннотация отсутствует в метаданных.")
                        st.markdown("**Резюме:**")
                        if r.get("summary"):
                            st.write(r["summary"])
                        else:
                            st.caption("Резюме отсутствует.")
                        st.caption(f"Статус: {r.get('status', '')}")
            with tab_table:
                df = pd.DataFrame(results).rename(
                    columns={
                        "source": "Источник",
                        "identifier": "Идентификатор",
                        "title": "Название",
                        "url": "Ссылка",
                        "summary": "Резюме",
                        "status": "Статус",
                        "authors": "Авторы",
                        "publication_year": "Год",
                        "journal": "Журнал",
                        "abstract": "Аннотация",
                        "bm25_score": "BM25",
                    }
                )
                if "Источник" in df.columns:
                    df["Источник"] = df["Источник"].map(
                        lambda sid: SOURCE_NAMES.get(sid, sid)
                    )
                st.dataframe(
                    df,
                    use_container_width=True,
                    hide_index=True,
                    column_config={"Ссылка": st.column_config.LinkColumn("Ссылка")},
                    column_order=[col for col in ("Источник", "Название", "Авторы", "Год", "Журнал", "BM25",
                                                  "Ссылка", "Аннотация", "Резюме", "Статус") if col in df.columns],
                )
    elif results is None:
        st.info("Нажмите «Запустить поиск сейчас», чтобы агенты выполнили один цикл обработки.")

    with st.expander("📜 Последние записи журнала"):
        log_path = Path(__file__).parent / "main.log"
        if log_path.exists():
            try:
                tail = log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-20:]
                st.text("\n".join(tail))
            except Exception as exc:
                st.warning(f"Не удалось прочитать файл журнала: {exc}")
        else:
            st.caption("Файл журнала не найден — записи выводятся в терминал, где запущен Streamlit.")

with tab_providers:
    st.header("Провайдеры модели")
    st.caption(
        "Выбранный провайдер строит резюме в этом сеансе сразу. "
        "Планировщик читает config.yaml после сохранения."
    )
    st.radio(
        "Активный провайдер",
        options=list(PROVIDER_LABELS),
        format_func=lambda key: PROVIDER_LABELS[key],
        horizontal=True,
        key="provider",
    )
    current = st.session_state["provider"]
    if current == "sumy":
        st.info(
            "Sumy строит резюме алгоритмом LSA на этой машине и не вызывает языковую модель."
        )
    else:
        _provider_input("Адрес API", current, "base_url")
        _provider_input("Модель", current, "model")
        _provider_input(
            "Переменная окружения с ключом",
            current,
            "api_key_env",
            help="Для Ollama поле можно оставить пустым. Сам ключ в файл не записывается.",
        )
        _provider_input(
            "Ключ для этого сеанса",
            current,
            "api_key",
            type="password",
            help="Попадает только в текущий процесс. В config.yaml записывается имя переменной.",
        )
        if st.button("Проверить подключение", key="check_provider"):
            notice = llm_unavailable_message(_active_llm(), with_fallback=False)
            model_name = st.session_state.get(f"model_{current}", "")
            st.session_state["provider_check"] = notice or f"Сервер отвечает. Модель: {model_name}."
            st.session_state["provider_check_ok"] = notice is None
            st.session_state["provider_check_for"] = current
        if (
            st.session_state.get("provider_check_for") == current
            and st.session_state.get("provider_check")
        ):
            if st.session_state.get("provider_check_ok"):
                st.success(st.session_state["provider_check"])
            else:
                st.warning(st.session_state["provider_check"])

    if st.button("Сохранить в config.yaml", type="primary", key="save_providers"):
        save_model_settings(st.session_state["provider"], _collect_providers())
        st.success("Провайдеры сохранены в config.yaml.")

with tab_diagram:
    diagram_path = Path(__file__).parent / "diagr" / "agents.html"
    if not diagram_path.is_file():
        st.warning("Файл схемы diagr/agents.html не найден.")
    else:
        components.html(
            diagram_path.read_text(encoding="utf-8"),
            height=900,
            scrolling=True,
        )
