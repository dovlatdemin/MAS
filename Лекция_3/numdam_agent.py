"""NUMDAM agent: offline BM25 search and a background metadata synchronizer."""

import argparse
import json
import threading
from pathlib import Path

from numdam_index import DEFAULT_WEIGHTS, NumdamIndex, sync_index
from utils import BASE_DIR, load_config

SOURCE_ID = "numdam"
SOURCE_NAME = "NUMDAM"


def settings():
    cfg = load_config().get("numdam") or {}
    path = Path(cfg.get("database", "data/numdam.sqlite3"))
    return {
        "db_path": path if path.is_absolute() else BASE_DIR / path,
        "weights": dict(DEFAULT_WEIGHTS, **(cfg.get("bm25_weights") or {})),
    }


def fetch(topics, rows=20, date_from=None, date_to=None, *, year_from=None, year_to=None,
          include_unknown_year=True, exclude_ids=(), db_path=None, weights=None):
    """Dates are deliberately not inherited from the recent-news sources; use year filters."""
    cfg = settings()
    index = NumdamIndex(db_path or cfg["db_path"])
    if not index.status()["count"]:
        raise ValueError("Индекс NUMDAM пуст. Откройте вкладку «Индекс NUMDAM» и загрузите метаданные.")
    return index.search(topics, rows, year_from=year_from, year_to=year_to,
                        include_unknown_year=include_unknown_year, exclude_ids=exclude_ids,
                        weights=weights if weights is not None else cfg["weights"])


class SyncJob:
    """One cached job per database; workers never call Streamlit APIs."""
    def __init__(self, path):
        self.path = str(path)
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.thread = None
        self.error = None

    def start(self, max_pages=None):
        with self.lock:
            if self.thread and self.thread.is_alive():
                return False
            self.error = None
            self.stop_event.clear()
            self.thread = threading.Thread(target=self._run, args=(max_pages,), daemon=True)
            self.thread.start()
            return True

    def _run(self, max_pages):
        try:
            sync_index(self.path, max_pages=max_pages, stop_event=self.stop_event)
        except Exception as exc:
            with self.lock:
                self.error = str(exc)

    def stop(self):
        self.stop_event.set()

    def snapshot(self):
        with self.lock:
            return {"running": bool(self.thread and self.thread.is_alive()), "error": self.error,
                    "stopping": self.stop_event.is_set()}


def cli():
    parser = argparse.ArgumentParser(description="NUMDAM: OAI-PMH sync and offline BM25 search")
    parser.add_argument("--db", type=Path, default=settings()["db_path"])
    sub = parser.add_subparsers(dest="command", required=True)
    sync = sub.add_parser("sync", help="Resume import or update the existing catalogue")
    sync.add_argument("--max-pages", type=int, help="Total pages across both metadata formats; omitted = all")
    sync.add_argument("--full", action="store_true", help="Restart a full metadata pass without clearing saved records")
    search = sub.add_parser("search", help="Search the saved catalogue, without network access")
    search.add_argument("query")
    search.add_argument("--rows", type=int, default=20)
    search.add_argument("--year-from", type=int)
    search.add_argument("--year-to", type=int)
    search.add_argument("--exclude-unknown-year", action="store_true")
    sub.add_parser("status")
    args = parser.parse_args()
    if args.command == "sync":
        def progress(state):
            print(f"Saved: {state['count']} articles; " + ", ".join(
                f"{name}: {s['processed']} / {s['total'] or '?'}" for name, s in state["streams"].items()), flush=True)
        result = sync_index(args.db, max_pages=args.max_pages, full=args.full, progress=progress)
    elif args.command == "search":
        result = fetch([args.query], args.rows, year_from=args.year_from, year_to=args.year_to,
                       include_unknown_year=not args.exclude_unknown_year, db_path=args.db)
    else:
        result = NumdamIndex(args.db).status()
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    cli()
