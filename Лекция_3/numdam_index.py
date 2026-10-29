"""Persistent NUMDAM catalogue, resumable OAI import, and SQLite FTS5 BM25."""

import json
import math
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from numdam_client import NumdamClient, OAIError, parse_article, parse_date_record

FORMATS = {"articles": "eudml-article2", "dates": "oai_dc"}
DEFAULT_WEIGHTS = {"title": 5.0, "abstract": 1.0, "keywords": 2.0}


class SyncBusy(RuntimeError):
    pass


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class NumdamIndex:
    def __init__(self, path):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS articles (
                    id INTEGER PRIMARY KEY, identifier TEXT NOT NULL UNIQUE,
                    title TEXT NOT NULL, abstract TEXT NOT NULL, keywords TEXT NOT NULL,
                    publication_year INTEGER, published TEXT NOT NULL, payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS publication_dates (
                    identifier TEXT PRIMARY KEY, publication_year INTEGER, published TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sync_state (id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL);
                CREATE VIRTUAL TABLE IF NOT EXISTS articles_fts USING fts5(
                    title, abstract, keywords, content='articles', content_rowid='id',
                    tokenize='unicode61 remove_diacritics 2'
                );
                CREATE TRIGGER IF NOT EXISTS articles_ai AFTER INSERT ON articles BEGIN
                    INSERT INTO articles_fts(rowid,title,abstract,keywords) VALUES(new.id,new.title,new.abstract,new.keywords);
                END;
                CREATE TRIGGER IF NOT EXISTS articles_ad AFTER DELETE ON articles BEGIN
                    INSERT INTO articles_fts(articles_fts,rowid,title,abstract,keywords)
                    VALUES('delete',old.id,old.title,old.abstract,old.keywords);
                END;
                CREATE TRIGGER IF NOT EXISTS articles_au AFTER UPDATE ON articles BEGIN
                    INSERT INTO articles_fts(articles_fts,rowid,title,abstract,keywords)
                    VALUES('delete',old.id,old.title,old.abstract,old.keywords);
                    INSERT INTO articles_fts(rowid,title,abstract,keywords) VALUES(new.id,new.title,new.abstract,new.keywords);
                END;
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @contextmanager
    def sync_lock(self):
        """OS lock survives UI reruns; the OS releases it if the process dies."""
        with self.path.with_suffix(".sync.lock").open("a+b") as lock:
            if lock.tell() == 0:
                lock.write(b"0")
                lock.flush()
            lock.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise SyncBusy("Синхронизация NUMDAM уже выполняется") from exc
            try:
                yield
            finally:
                lock.seek(0)
                if os.name == "nt":
                    msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def state(self):
        with self.connect() as db:
            row = db.execute("SELECT payload FROM sync_state WHERE id=1").fetchone()
        return json.loads(row[0]) if row else {"complete": False, "pending": False, "last_success": None}

    @staticmethod
    def _save_state(db, state):
        db.execute("INSERT INTO sync_state VALUES (1,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload",
                   (json.dumps(state, ensure_ascii=False),))

    def save_state(self, state):
        with self.connect() as db:
            self._save_state(db, state)

    def status(self):
        state = self.state()
        with self.connect() as db:
            row = db.execute("""SELECT count(*) AS count,
                sum(CASE WHEN coalesce(a.publication_year,d.publication_year) IS NULL THEN 1 ELSE 0 END) AS unknown_years,
                sum(CASE WHEN a.abstract='' THEN 1 ELSE 0 END) AS no_abstract
                FROM articles a LEFT JOIN publication_dates d USING(identifier)""").fetchone()
        return dict(state, count=row["count"], unknown_years=row["unknown_years"] or 0,
                    no_abstract=row["no_abstract"] or 0)

    def apply_page(self, stream, items, state):
        """Records, FTS triggers and cursor share one atomic page transaction."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for item in items:
                if item is None:
                    continue
                if item["deleted"]:
                    db.execute("DELETE FROM articles WHERE identifier=?", (item["identifier"],))
                    db.execute("DELETE FROM publication_dates WHERE identifier=?", (item["identifier"],))
                elif stream == "articles":
                    db.execute("""INSERT INTO articles(identifier,title,abstract,keywords,publication_year,published,payload)
                        VALUES (?,?,?,?,?,?,?) ON CONFLICT(identifier) DO UPDATE SET
                        title=excluded.title,abstract=excluded.abstract,keywords=excluded.keywords,
                        publication_year=excluded.publication_year,published=excluded.published,payload=excluded.payload""",
                        (item["identifier"], item["title"], item["abstract"], " ".join(item["keywords"]),
                         item["publication_year"], item["published"], json.dumps(item, ensure_ascii=False)))
                else:
                    db.execute("""INSERT INTO publication_dates VALUES (?,?,?) ON CONFLICT(identifier) DO UPDATE SET
                        publication_year=excluded.publication_year,published=excluded.published""",
                        (item["identifier"], item["publication_year"], item["published"]))
            self._save_state(db, state)

    def search(self, topics, rows=20, *, year_from=None, year_to=None, include_unknown_year=True,
               exclude_ids=(), weights=None):
        if rows <= 0:
            return []
        if year_from is not None and year_to is not None and year_from > year_to:
            raise ValueError("Год «с» должен быть не позже года «по»")
        weighted = dict(DEFAULT_WEIGHTS, **(weights or {}))
        values = [float(weighted[field]) for field in DEFAULT_WEIGHTS]
        if any(not math.isfinite(v) or v < 0 for v in values) or not any(values):
            raise ValueError("Веса BM25 должны быть конечными и неотрицательными; хотя бы один вес больше нуля")
        query = topics if isinstance(topics, str) else " ".join(topics)
        with self.connect() as db:
            # Let the SAME SQLite tokenizer normalize both query and corpus.
            db.execute("CREATE VIRTUAL TABLE temp.query_fts USING fts5(text, tokenize='unicode61 remove_diacritics 2')")
            db.execute("CREATE VIRTUAL TABLE temp.query_vocab USING fts5vocab(query_fts, 'row')")
            db.execute("INSERT INTO query_fts VALUES (?)", (query,))
            terms = [row[0] for row in db.execute("SELECT term FROM query_vocab ORDER BY term")]
            if not terms:
                return []
            if len(terms) > 256:
                raise ValueError("Запрос NUMDAM должен содержать не более 256 разных слов")
            match = " OR ".join('"' + term.replace('"', '""') + '"' for term in terms)
            db.execute("CREATE TEMP TABLE excluded(identifier TEXT PRIMARY KEY)")
            db.executemany("INSERT OR IGNORE INTO excluded VALUES (?)", ((v,) for v in exclude_ids))
            conditions = ["articles_fts MATCH ?", "NOT EXISTS (SELECT 1 FROM excluded e WHERE e.identifier=a.identifier)"]
            parameters = [*values, match]
            year = "coalesce(a.publication_year,d.publication_year)"
            bounds, bound_params = [], []
            if year_from is not None:
                bounds.append(f"{year} >= ?")
                bound_params.append(int(year_from))
            if year_to is not None:
                bounds.append(f"{year} <= ?")
                bound_params.append(int(year_to))
            if bounds:
                bounded = " AND ".join(bounds)
                conditions.append(f"(({bounded}) OR {year} IS NULL)" if include_unknown_year else f"({bounded})")
                parameters.extend(bound_params)
            if not include_unknown_year:
                conditions.append(f"{year} IS NOT NULL")
            parameters.append(int(rows))
            result = db.execute(f"""SELECT a.payload, {year} AS year,
                CASE WHEN a.published<>'' THEN a.published ELSE coalesce(d.published,'') END AS published,
                -bm25(articles_fts,?,?,?) AS bm25_score,
                highlight(articles_fts,0,'\ue000','\ue001') || ' ' ||
                highlight(articles_fts,1,'\ue000','\ue001') || ' ' ||
                highlight(articles_fts,2,'\ue000','\ue001') AS matched
                FROM articles_fts JOIN articles a ON a.id=articles_fts.rowid
                LEFT JOIN publication_dates d ON d.identifier=a.identifier
                WHERE {' AND '.join(conditions)}
                ORDER BY bm25_score DESC, a.identifier ASC LIMIT ?""", parameters).fetchall()
        items = []
        for row in result:
            item = json.loads(row["payload"])
            item.update(publication_year=row["year"], published=row["published"],
                        bm25_score=row["bm25_score"],
                        matched_terms=sorted(set(re.findall("\ue000(.*?)\ue001", row["matched"]))))
            items.append(item)
        return items


def sync_index(path, *, max_pages=None, full=False, client=None, progress=None, stop_event=None):
    """Interleave two paginated formats: rich articles and missing publication dates."""
    if max_pages is not None and max_pages < 1:
        raise ValueError("Лимит страниц должен быть положительным")
    index = NumdamIndex(path)
    own_client = client is None
    client = client or NumdamClient()
    try:
        with index.sync_lock():
            state = index.state()
            try:
                description = client.describe()
                if not state.get("pending") or full:
                    daily = description["granularity"] == "YYYY-MM-DD"
                    server_time = datetime.fromisoformat(description["response_date"].replace("Z", "+00:00"))
                    until = server_time.strftime("%Y-%m-%d" if daily else "%Y-%m-%dT%H:%M:%SZ")
                    mode = "full" if full or not state.get("complete") else "incremental"
                    since = None
                    if mode == "incremental":
                        boundary = datetime.fromisoformat(state["last_until"].replace("Z", "+00:00")) - timedelta(days=1)
                        since = boundary.strftime("%Y-%m-%d" if daily else "%Y-%m-%dT%H:%M:%SZ")
                    state = dict(state, pending=True, mode=mode, since=since, until=until, started=utc_now(),
                                 streams={name: {"token": None, "done": False, "processed": 0, "total": None}
                                          for name in FORMATS}, next_stream="articles", skipped=0,
                                 deleted_record=description["deleted_record"], error="")
                    index.save_state(state)
                state["error"] = ""
                pages = 0
                restarted = set()
                while not all(s["done"] for s in state["streams"].values()):
                    if (max_pages is not None and pages >= max_pages) or (stop_event and stop_event.is_set()):
                        break
                    stream = state["next_stream"]
                    if state["streams"][stream]["done"]:
                        stream = next(name for name, s in state["streams"].items() if not s["done"])
                    cursor = state["streams"][stream]
                    try:
                        records, token, total = client.page(FORMATS[stream], token=cursor["token"],
                                                           since=state["since"], until=state["until"])
                    except OAIError as exc:
                        if exc.code == "badResumptionToken" and cursor["token"] and stream not in restarted:
                            restarted.add(stream)
                            cursor.update(token=None, processed=0, total=None)
                            index.save_state(state)
                            continue
                        raise
                    if token and token == cursor["token"]:
                        raise OAIError("repeatedToken", "Сервер повторил токен страницы; продолжение остановлено")
                    parser = parse_article if stream == "articles" else parse_date_record
                    items = [parser(record) for record in records]
                    state["skipped"] += sum(item is None for item in items)
                    cursor.update(token=token, done=not token, processed=cursor["processed"] + len(records),
                                  total=total if total is not None else cursor["total"])
                    state["next_stream"] = "dates" if stream == "articles" else "articles"
                    if all(s["done"] for s in state["streams"].values()):
                        state.update(pending=False, complete=True, last_success=utc_now(), last_until=state["until"])
                    index.apply_page(stream, items, state)
                    pages += 1
                    if progress:
                        progress(index.status())
                return index.status()
            except Exception as exc:
                # Reload the committed cursor: in-memory state may describe an uncommitted page.
                saved = index.state()
                saved["error"] = str(exc)
                index.save_state(saved)
                raise
    finally:
        if own_client:
            client.close()
