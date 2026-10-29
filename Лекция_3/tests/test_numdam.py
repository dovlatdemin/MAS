import json
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import Mock, patch

import requests

import main
import numdam_agent
import scraper
from numdam_client import OAI, NumdamClient, OAIError, parse_article, parse_date_record, text
from numdam_index import NumdamIndex, SyncBusy, sync_index

FIXTURES = Path(__file__).parent / "fixtures"


def record(ident="a", title="Graph", abstract="", year=None, deleted=False):
    root = ET.Element(OAI + "record")
    h = ET.SubElement(root, OAI + "header", {"status": "deleted"} if deleted else {})
    ET.SubElement(h, OAI + "identifier").text = "oai:numdam.org:" + ident
    ET.SubElement(h, OAI + "datestamp").text = "2026-10-28"
    if deleted:
        return root
    m = ET.SubElement(root, OAI + "metadata")
    j = "{http://jats.nlm.nih.gov}"
    a = ET.SubElement(m, j + "article", {"{http://www.w3.org/XML/1998/namespace}lang": "en"})
    front = ET.SubElement(a, j + "front")
    meta = ET.SubElement(front, j + "article-meta")
    group = ET.SubElement(meta, j + "title-group")
    ET.SubElement(group, j + "article-title").text = title
    ET.SubElement(meta, j + "self-uri", {"{http://www.w3.org/1999/xlink}href": "https://www.numdam.org/item/" + ident + "/"})
    if abstract:
        ET.SubElement(meta, j + "abstract").text = abstract
    if year is not None:
        pub = ET.SubElement(meta, j + "pub-date")
        ET.SubElement(pub, j + "year").text = str(year)
    return root


def dc_record(ident="a", year="1994"):
    root = record(ident)
    meta = root.find(OAI + "metadata")
    meta.clear()
    ET.SubElement(meta, "{http://purl.org/dc/elements/1.1/}date").text = year
    return root


def response(body, status=200, headers=None):
    r = requests.Response()
    r.status_code = status
    r._content = body.encode() if isinstance(body, str) else body
    r.headers.update(headers or {})
    return r


def envelope(contents):
    return f'<OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/">{contents}</OAI-PMH>'


class FakeClient:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def describe(self):
        return {"response_date": "2026-10-29T12:00:00Z", "granularity": "YYYY-MM-DD", "deleted_record": "no"}

    def page(self, fmt, *, token=None, since=None, until=None):
        self.calls.append((fmt, token, since, until))
        value = self.pages[(fmt, token)]
        if isinstance(value, Exception):
            raise value
        return value


class IndexCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "numdam.sqlite3"
        self.index = NumdamIndex(self.path)

    def put(self, *records):
        self.index.apply_page("articles", [parse_article(r) for r in records], self.index.state())


class ParsingTests(unittest.TestCase):
    def test_paragraph_boundaries_and_formula_alternatives(self):
        node = ET.fromstring('<abstract><p>First paragraph.</p><p>Order <inline-formula><alternatives>'
                             '<m:math xmlns:m="http://www.w3.org/1998/Math/MathML"><m:mi>r</m:mi></m:math>'
                             '<tex-math>$r$</tex-math></alternatives></inline-formula> is finite.</p></abstract>')
        self.assertEqual(text(node), 'First paragraph. Order $r$ is finite.')

    def test_real_metadata_and_separate_publication_date(self):
        item = parse_article(ET.parse(FIXTURES / "numdam_with_abstract.xml").getroot())
        self.assertEqual(item["identifier"], "numdam:MSH_1994__125__5_0")
        self.assertIn("Pichon", item["authors"])
        self.assertIn("This paper", item["abstract"])
        self.assertIn("Cette étude", item["abstract"])
        self.assertEqual(item["language"], "fr")
        self.assertEqual(item["publication_year"], None)
        self.assertEqual(item["oai_datestamp"], "2025-07-21")
        date_item = parse_date_record(ET.parse(FIXTURES / "numdam_date.xml").getroot())
        self.assertEqual((date_item["published"], date_item["publication_year"]), ("1994", 1994))

    def test_real_no_abstract(self):
        item = parse_article(ET.parse(FIXTURES / "numdam_article.xml").getroot())
        self.assertEqual(item["abstract"], "")
        self.assertEqual(item["title"], "Avant-propos")

    def test_nested_text_keywords_msc_and_year(self):
        r = record(year=1980)
        meta = r.find(".//{*}article-meta")
        title = meta.find("{*}title-group/{*}article-title")
        title.text = "On "
        ET.SubElement(title, "italic").text = "graphs"
        title[0].tail = " and trees"
        group = ET.SubElement(meta, "kwd-group")
        ET.SubElement(group, "kwd").text = "combinatorics"
        msc = ET.SubElement(meta, "kwd-group", {"kwd-group-type": "MSC2020"})
        ET.SubElement(msc, "kwd").text = "05C10"
        parsed = parse_article(r)
        self.assertEqual(parsed["title"], "On graphs and trees")
        self.assertEqual(parsed["keywords"], ["combinatorics"])
        self.assertEqual(parsed["msc"], ["05C10"])
        self.assertEqual(parsed["published"], "1980")


class TransportTests(unittest.TestCase):
    def client(self, replies):
        session = Mock()
        session.get.side_effect = replies
        self.sleep = Mock()
        self.session = session
        return NumdamClient(session=session, interval=0, sleep=self.sleep)

    def test_http_200_oai_error(self):
        client = self.client([response(envelope('<error code="badArgument">invalid</error>'))])
        with self.assertRaisesRegex(OAIError, "badArgument"):
            client.request(verb="Identify")

    def test_no_records_and_token_exclusive_parameters(self):
        client = self.client([response(envelope('<error code="noRecordsMatch">empty</error>'))])
        self.assertEqual(client.page("eudml-article2", token="opaque", since="2000-01-01", until="2026-10-29"), ([], None, None))
        self.assertEqual(self.session.get.call_args.kwargs["params"], {"verb": "ListRecords", "resumptionToken": "opaque"})

    def test_retry_after_and_retry_limit(self):
        client = self.client([response("", 503, {"Retry-After": "3"}), response(envelope("<Identify/>"))])
        client.request(verb="Identify")
        self.sleep.assert_called_once_with(3)
        client = self.client([requests.Timeout()] * 4)
        with self.assertRaises(requests.Timeout):
            client.request(verb="Identify")
        self.assertEqual(self.session.get.call_count, 4)

    def test_large_retry_after_stops_without_early_retry(self):
        client = self.client([response("", 429, {"Retry-After": "1000"})])
        with self.assertRaisesRegex(OAIError, "retryLater"):
            client.request(verb="Identify")
        self.assertEqual(self.session.get.call_count, 1)
        self.sleep.assert_not_called()

    def test_html_and_malformed_xml_rejected(self):
        for body in ("<html>oops</html>", "<bad"):
            with self.subTest(body=body), self.assertRaises(OAIError):
                self.client([response(body)]).request(verb="ListRecords")


class SearchTests(IndexCase):
    def setUp(self):
        super().setUp()
        self.put(record("a", "Graph", "other", 1994), record("b", "Other", "graph", 2020),
                 record("c", "Unrelated", "nothing", 2020), record("d", "Théorie графов", "", None))

    def test_bm25_field_weights_and_or_query(self):
        hits = self.index.search(["graph missing"])
        self.assertEqual([x["identifier"] for x in hits], ["numdam:a", "numdam:b"])
        self.assertGreater(hits[0]["bm25_score"], hits[1]["bm25_score"])
        self.assertEqual(hits[0]["matched_terms"], ["Graph"])
        reversed_weights = self.index.search(["graph"], weights={"title": 1, "abstract": 5})
        self.assertEqual(reversed_weights[0]["identifier"], "numdam:b")

    def test_filters_and_history_before_top_k(self):
        self.assertEqual(self.index.search(["graph"], 1, exclude_ids=["numdam:a"])[0]["identifier"], "numdam:b")
        self.assertEqual(self.index.search(["graph"], 1, year_from=2000)[0]["identifier"], "numdam:b")
        self.assertEqual(len(self.index.search(["theorie"], year_from=2000)), 1)
        self.assertEqual(self.index.search(["theorie"], year_from=2000, include_unknown_year=False), [])

    def test_unicode_punctuation_empty_and_literal_operators(self):
        self.assertEqual(len(self.index.search(['"THÉORIE"--графов ('])), 1)
        self.assertEqual(self.index.search(["   !!!"]), [])
        self.assertEqual(self.index.search(["OR AND NOT"]), [])
        self.assertEqual(self.index.search(['"; DROP TABLE articles; -- graph']), self.index.search(["graph"]))
        self.assertEqual(self.index.status()["count"], 4)

    def test_updates_delete_and_date_enrichment(self):
        self.index.apply_page("dates", [parse_date_record(dc_record("d", "1985"))], self.index.state())
        hit = self.index.search(["theorie"], year_to=1990, include_unknown_year=False)[0]
        self.assertEqual(hit["published"], "1985")
        self.put(record("a", "Topology", "other", 1994))
        self.assertEqual(len(self.index.search(["graph"])), 1)
        self.put(record("b", deleted=True))
        self.assertEqual(self.index.search(["graph"]), [])
        self.assertEqual(self.index.status()["count"], 3)

    def test_stable_order(self):
        self.put(record("a2", "Graph", "other", 1994))
        self.assertEqual([h["identifier"] for h in self.index.search(["graph"], 2)], ["numdam:a", "numdam:a2"])

    def test_restart_cli_uses_saved_index(self):
        run = subprocess.run([sys.executable, "numdam_agent.py", "--db", str(self.path), "search", "graph"],
                             capture_output=True, text=True, encoding="utf-8", timeout=20)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(len(json.loads(run.stdout)), 2)


class SyncTests(IndexCase):
    def pages(self):
        return {("eudml-article2", None): ([record("a")], "next-a", 2),
                ("eudml-article2", "next-a"): ([record("b")], None, 2),
                ("oai_dc", None): ([dc_record("a")], "next-d", 2),
                ("oai_dc", "next-d"): ([dc_record("b", "2001")], None, 2)}

    def test_partial_resume_pagination_and_full_completion(self):
        client = FakeClient(self.pages())
        state = sync_index(self.path, client=client, max_pages=2)
        self.assertFalse(state["complete"])
        self.assertTrue(state["pending"])
        self.assertIsNone(state["last_success"])
        self.assertEqual(state["count"], 1)
        state = sync_index(self.path, client=client)
        self.assertTrue(state["complete"])
        self.assertFalse(state["pending"])
        self.assertEqual(state["count"], 2)
        self.assertEqual(state["unknown_years"], 0)
        self.assertEqual([c[1] for c in client.calls], [None, None, "next-a", "next-d"])

    def test_network_failure_keeps_last_page_and_resumes(self):
        pages = self.pages()
        pages[("oai_dc", None)] = requests.ConnectionError("temporary")
        with self.assertRaises(requests.ConnectionError):
            sync_index(self.path, client=FakeClient(pages))
        state = self.index.status()
        self.assertEqual(state["count"], 1)
        self.assertEqual(state["streams"]["articles"]["token"], "next-a")
        self.assertIsNone(state["last_success"])
        self.assertIn("temporary", state["error"])
        client = FakeClient(self.pages())
        self.assertTrue(sync_index(self.path, client=client)["complete"])
        self.assertEqual(client.calls[0][0], "oai_dc")

    def test_incremental_overlap_and_idempotency(self):
        sync_index(self.path, client=FakeClient(self.pages()))
        client = FakeClient({("eudml-article2", None): ([record("a", "Updated graph")], None, 1),
                             ("oai_dc", None): ([dc_record("a")], None, 1)})
        state = sync_index(self.path, client=client)
        self.assertEqual(state["mode"], "incremental")
        self.assertEqual(client.calls[0][2], "2026-10-28")
        self.assertEqual(state["count"], 2)
        self.assertEqual(self.index.search(["updated"])[0]["title"], "Updated graph")

    def test_failure_does_not_advance_success_watermark(self):
        before = sync_index(self.path, client=FakeClient(self.pages()))
        with self.assertRaises(OAIError):
            sync_index(self.path, client=FakeClient({("eudml-article2", None): OAIError("badArgument", "no")}))
        after = self.index.status()
        self.assertEqual(after["last_success"], before["last_success"])
        self.assertEqual(after["last_until"], before["last_until"])
        self.assertTrue(after["pending"])

    def test_expired_token_restarts_stream_without_duplicates(self):
        sync_index(self.path, client=FakeClient(self.pages()), max_pages=2)
        client = FakeClient({("eudml-article2", "next-a"): OAIError("badResumptionToken", "expired"),
                             ("eudml-article2", None): ([record("a"), record("b")], None, 2),
                             ("oai_dc", "next-d"): ([dc_record("b")], None, 2)})
        self.assertEqual(sync_index(self.path, client=client)["count"], 2)

    def test_page_transaction_rolls_back_records_and_checkpoint(self):
        before = self.index.state()
        good = parse_article(record("a"))
        bad = dict(good, identifier="numdam:bad")
        del bad["title"]
        with self.assertRaises(KeyError):
            self.index.apply_page("articles", [good, bad], {"complete": True})
        self.assertEqual(self.index.status()["count"], 0)
        self.assertEqual(self.index.state(), before)

    def test_deleted_record_removes_fts(self):
        self.put(record("a"))
        client = FakeClient({("eudml-article2", None): ([record("a", deleted=True)], None, 1),
                             ("oai_dc", None): ([], None, 0)})
        self.assertEqual(sync_index(self.path, client=client)["count"], 0)
        self.assertEqual(self.index.search(["graph"]), [])

    def test_sync_lock_and_stop(self):
        with self.index.sync_lock():
            with self.assertRaises(SyncBusy):
                sync_index(self.path, client=FakeClient(self.pages()))
        stop = threading.Event()
        stop.set()
        client = FakeClient(self.pages())
        self.assertTrue(sync_index(self.path, client=client, stop_event=stop)["pending"])
        self.assertEqual(client.calls, [])


class PipelineTests(IndexCase):
    def test_zero_field_weight_preserves_top_k_matches(self):
        self.put(record("a", "Graph", "", 1994))
        cfg = {"db_path": self.path, "weights": {"title": 0, "abstract": 1, "keywords": 1}}
        with patch.object(numdam_agent, "settings", return_value=cfg):
            result = scraper.get_new_items(topics=["graph"], sources=["numdam"],
                                           skip_seen=False, save_history=False)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["bm25_score"], 0)

    def test_summary_failure_keeps_bm25_results(self):
        self.put(record("a", "Graph", "A short abstract.", 1994))
        cfg = {"db_path": self.path, "weights": {}}
        with patch.object(numdam_agent, "settings", return_value=cfg), \
             patch.object(main, "llm_unavailable_message", return_value=None), \
             patch.object(main, "summarize", side_effect=[main.LlmUnavailable("LLM unavailable"), RuntimeError("Sumy failed")]), \
             patch.object(main, "extract_text_from_url", side_effect=AssertionError("HTML forbidden")):
            result = main.process_items(topics=["graph"], sources=["numdam"], skip_seen=False,
                                        save_history=False, engine="ollama", llm={}, send=False)
        self.assertEqual(len(result), 1)
        self.assertGreater(result[0]["bm25_score"], 0)
        self.assertIn("Sumy failed", result[0]["status"])

    def test_numdam_ranking_history_no_html_and_short_or_missing_abstract(self):
        self.put(record("a", "Graph", "Short abstract.", 1994), record("b", "Graph", "", 1994),
                 record("c", "Graph theory", "Graph theory is a mathematical subject.", 1994))
        cfg = {"db_path": self.path, "weights": {"title": 5, "abstract": 1, "keywords": 2}}
        with patch.object(numdam_agent, "settings", return_value=cfg), \
             patch.object(scraper, "get_history", return_value=["numdam:c"]), \
             patch.object(scraper, "validate_item", side_effect=AssertionError("NUMDAM must bypass all-words validation")), \
             patch.object(main, "extract_text_from_url", side_effect=AssertionError("HTML forbidden")), \
             patch("requests.get", side_effect=AssertionError("network forbidden")), \
             patch("requests.post", side_effect=AssertionError("network forbidden")), \
             patch.object(main, "summarize", return_value="Summary") as summary:
            result = main.process_items(topics=["graph missing"], sources=["numdam"], rows=2,
                                        skip_seen=True, save_history=False, engine="sumy", send=False)
        self.assertEqual(len(result), 2)
        self.assertNotIn("numdam:c", [r["identifier"] for r in result])
        self.assertEqual([r["bm25_score"] for r in result], sorted([r["bm25_score"] for r in result], reverse=True))
        self.assertTrue(all(r["publication_year"] == 1994 for r in result))
        empty = next(r for r in result if not r["abstract"])
        self.assertIn("Аннотация отсутствует", empty["status"])
        summary.assert_called_once()
        self.assertEqual(summary.call_args.args[0], "Short abstract.")

    def test_existing_sources_still_receive_dates_and_validate(self):
        item = {"source": "arxiv", "identifier": "arxiv:example", "title": "Graph theory", "abstract": "", "url": "https://example.org"}
        with patch.object(scraper.arxiv_agent, "fetch", return_value=[item]) as arxiv, \
             patch.object(scraper.archive_agent, "fetch", return_value=[dict(item, source="archive", identifier="archive-id")]) as archive, \
             patch("requests.get", side_effect=AssertionError("unexpected network")):
            result = scraper.get_new_items(["graph theory"], ["arxiv", "archive"], skip_seen=False, save_history=False)
        self.assertEqual(len(result), 2)
        self.assertIn("date_from", arxiv.call_args.kwargs)
        self.assertIn("date_to", archive.call_args.kwargs)


if __name__ == "__main__":
    unittest.main()
