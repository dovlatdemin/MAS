"""Streamlit renders and runs the real NUMDAM pipeline against temporary local data."""

import threading
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

import main
import numdam_agent
import utils
from test_numdam import IndexCase, record


class StreamlitTests(IndexCase):
    def app(self):
        stack = self.enterContext(ExitStack())
        cfg = {"sources": ["numdam"], "topics": ["graph"], "summarizer": "sumy"}
        stack.enter_context(patch.object(utils, "load_config", return_value=cfg))
        stack.enter_context(patch.object(main, "load_config", return_value=cfg))
        stack.enter_context(patch.object(numdam_agent, "settings", return_value={"db_path": self.path, "weights": {}}))
        stack.enter_context(patch.object(main, "summarize", return_value="Local summary"))
        stack.enter_context(patch("requests.sessions.Session.request", side_effect=AssertionError("UI search must be offline")))
        app = AppTest.from_file(str(Path(utils.BASE_DIR) / "streamlit_app.py"), default_timeout=15).run()
        self.assertFalse(app.exception)
        return app

    def test_empty_index_prompts_for_sync_and_disables_search(self):
        app = self.app()
        self.assertIn("NUMDAM", app.multiselect[0].options)
        self.assertTrue(app.button(key="run_search").disabled)
        self.assertFalse(app.button(key="numdam_sync").disabled)
        self.assertTrue(app.checkbox(key="numdam_all_years").value)
        self.assertTrue(any("Индекс пуст" in message.value for message in app.info))

    def test_results_years_bm25_and_missing_abstract_render(self):
        self.put(record("a", "Graph", "Short abstract.", 1994), record("b", "Graph", "", 1994))
        app = self.app()
        self.assertTrue(app.selectbox[0].disabled)  # Recent-news dates do not filter NUMDAM.
        self.assertTrue(any("частично" in warning.value.lower() for warning in app.warning))
        app.button(key="run_search").click().run()
        self.assertFalse(app.exception)
        results = app.session_state["results"]
        self.assertEqual(len(results), 2)
        self.assertEqual([r["bm25_score"] for r in results], sorted([r["bm25_score"] for r in results], reverse=True))
        self.assertTrue(all(r["publication_year"] == 1994 for r in results))
        self.assertTrue(any("Оценка BM25" in m.value for m in app.markdown))
        self.assertTrue(any("Аннотация отсутствует" in c.value for c in app.caption))
        self.assertIn("BM25", app.dataframe[0].value.columns)
        app.checkbox(key="numdam_all_years").uncheck().run()
        app.number_input(key="numdam_year_from").set_value(2000)
        app.number_input(key="numdam_year_to").set_value(2025)
        app.checkbox(key="numdam_unknown_year").uncheck()
        app.button(key="run_search").click().run()
        self.assertFalse(app.exception)
        self.assertEqual(app.session_state["results"], [])

    def test_sync_error_and_saved_count_are_visible(self):
        self.put(record("a"))
        self.index.save_state(dict(self.index.state(), pending=True, error="Temporary OAI failure"))
        app = self.app()
        self.assertTrue(any("Temporary OAI failure" in err.value for err in app.error))
        counts = {m.label: m.value for m in app.metric}
        self.assertEqual(counts["Статей в индексе"], "1")
        self.assertFalse(app.button(key="run_search").disabled)


class BackgroundJobTests(unittest.TestCase):
    def test_start_stop_and_error(self):
        started = threading.Event()

        def sync(path, *, max_pages, stop_event):
            started.set()
            stop_event.wait(timeout=5)

        job = numdam_agent.SyncJob("unused.sqlite3")
        with patch.object(numdam_agent, "sync_index", side_effect=sync):
            try:
                self.assertTrue(job.start(max_pages=2))
                self.assertTrue(started.wait(timeout=2))
                self.assertTrue(job.snapshot()["running"])
                self.assertFalse(job.start())
            finally:
                job.stop()
                job.thread.join(timeout=2)
        self.assertFalse(job.snapshot()["running"])
        self.assertIsNone(job.snapshot()["error"])
        with patch.object(numdam_agent, "sync_index", side_effect=RuntimeError("Network failed")):
            self.assertTrue(job.start())
            job.thread.join(timeout=2)
        self.assertFalse(job.snapshot()["running"])
        self.assertEqual(job.snapshot()["error"], "Network failed")


if __name__ == "__main__":
    unittest.main()
