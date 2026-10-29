"""NUMDAM OAI-PMH transport and metadata parsers. No article-page requests."""

import re
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

import requests

ENDPOINT = "https://www.numdam.org/oai"
OAI = "{http://www.openarchives.org/OAI/2.0/}"
DC = "{http://purl.org/dc/elements/1.1/}"
XLINK = "{http://www.w3.org/1999/xlink}href"


class OAIError(RuntimeError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(f"NUMDAM ({code}): {message}")


def text(node):
    if node is None:
        return ""

    def contents(element):
        # JATS often supplies the same formula as MathML AND TeX alternatives.
        # Keep one representation, without duplicating it in the searchable text.
        if element.tag.split("}")[-1] == "alternatives" and len(element):
            for kind in ("tex-math", "math"):
                selected = element.find("{*}" + kind)
                if selected is not None:
                    return contents(selected)
            return contents(element[0])
        parts = [element.text or ""]
        for child in element:
            parts.append(contents(child))
            if child.tag.split("}")[-1] in ("p", "title", "sec", "list-item", "break"):
                parts.append(" ")
            parts.append(child.tail or "")
        return "".join(parts)

    return re.sub(r"\s+", " ", contents(node)).strip()


def publication_date(value):
    """Keep the actual date precision; never derive a date from an identifier."""
    value = (value or "").strip()
    if not re.fullmatch(r"\d{4}(?:-\d{2})?(?:-\d{2})?", value):
        return "", None
    try:
        parts = [int(x) for x in value.split("-")]
        date(parts[0], parts[1] if len(parts) > 1 else 1, parts[2] if len(parts) > 2 else 1)
    except ValueError:
        return "", None
    return value, parts[0]


def header(record):
    head = record.find(OAI + "header")
    ident = text(head.find(OAI + "identifier")) if head is not None else ""
    if not ident.startswith("oai:numdam.org:") or not ident.split(":", 2)[-1]:
        raise OAIError("invalidRecord", "Отсутствует корректный идентификатор записи")
    return {
        "oai_identifier": ident,
        "identifier": "numdam:" + ident.split(":", 2)[-1],
        "oai_datestamp": text(head.find(OAI + "datestamp")),
        "deleted": head.get("status") == "deleted",
    }


def parse_article(record):
    item = header(record)
    if item["deleted"]:
        return item
    article = record.find(".//{*}article")
    if article is None:
        return None  # e.g. a book in the repository's other metadata formats
    meta = article.find("{*}front/{*}article-meta")
    if meta is None:
        raise OAIError("invalidRecord", f"Нет article-meta: {item['identifier']}")
    titles = [text(n) for n in meta.findall("{*}title-group/{*}article-title")]
    titles += [text(n) for n in meta.findall("{*}title-group/{*}trans-title-group/{*}trans-title")]
    title = " / ".join(dict.fromkeys(t for t in titles if t))
    url = ""
    for node in meta.findall("{*}self-uri"):
        candidate = node.get(XLINK, "")
        parts = urlsplit(candidate)
        if (parts.scheme in ("http", "https") and parts.hostname in ("numdam.org", "www.numdam.org")
                and not node.get("content-type") and not parts.path.lower().endswith((".pdf", ".djvu"))):
            url = candidate.replace("http://", "https://", 1)
            break
    if not title or not url:
        raise OAIError("invalidRecord", f"Нет названия или ссылки: {item['identifier']}")
    authors = []
    for group in meta.findall("{*}contrib-group"):
        if group.get("content-type", "authors") not in ("authors", "author"):
            continue
        for contrib in group.findall("{*}contrib"):
            if contrib.get("contrib-type", "author") != "author":
                continue
            name = contrib.find("{*}name")
            if name is not None:
                author = " ".join(filter(None, (text(name.find("{*}given-names")), text(name.find("{*}surname")))))
            else:
                author = text(contrib.find("{*}string-name")) or text(contrib.find("{*}collab"))
            if author:
                authors.append(author)
    abstracts = [text(n) for n in meta if n.tag.split("}")[-1] in ("abstract", "trans-abstract")]
    keywords, msc = [], []
    for group in meta.findall(".//{*}kwd-group"):
        target = msc if "msc" in group.get("kwd-group-type", "").lower() else keywords
        target.extend(text(n) for n in group.findall("{*}kwd") if text(n))
    for group in meta.findall(".//{*}subj-group"):
        target = msc if "msc" in group.get("subj-group-type", "").lower() else keywords
        target.extend(text(n) for n in group.findall("{*}subject") if text(n))
    published, year = "", None
    for pub in meta.findall("{*}pub-date"):
        components = [text(pub.find("{*}" + part)) for part in ("year", "month", "day")]
        candidate = components[0]
        if components[1].isdigit():
            candidate += "-" + components[1].zfill(2)
            if components[2].isdigit():
                candidate += "-" + components[2].zfill(2)
        published, year = publication_date(candidate)
        if year is not None:
            break
    item.update(source="numdam", title=title, url=url, authors=", ".join(authors),
                abstract="\n\n".join(dict.fromkeys(a for a in abstracts if a)),
                published=published, publication_year=year,
                journal=text(article.find("{*}front/{*}journal-meta/{*}journal-title-group/{*}journal-title")),
                language=article.get("{http://www.w3.org/XML/1998/namespace}lang", ""),
                keywords=list(dict.fromkeys(keywords)), msc=list(dict.fromkeys(msc)))
    return item


def parse_date_record(record):
    item = header(record)
    if item["deleted"]:
        return item
    item.update(published="", publication_year=None)
    for node in record.findall(".//" + DC + "date"):
        published, year = publication_date(text(node))
        if year is not None:
            item.update(published=published, publication_year=year)
            break
    return item


class NumdamClient:
    def __init__(self, *, timeout=30, retries=3, interval=1.0, session=None, sleep=time.sleep):
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": "MAS-NUMDAM/1.0 (OAI-PMH metadata client)"})
        self.timeout, self.retries, self.interval, self.sleep = timeout, retries, interval, sleep
        self._last_request = 0.0

    def close(self):
        self.session.close()

    def request(self, **params):
        for attempt in range(self.retries + 1):
            wait = self.interval - (time.monotonic() - self._last_request)
            if wait > 0:
                self.sleep(wait)
            try:
                response = self.session.get(ENDPOINT, params=params, timeout=self.timeout)
            except (requests.Timeout, requests.ConnectionError):
                if attempt == self.retries:
                    raise
                self.sleep(2 ** attempt)
                continue
            finally:
                self._last_request = time.monotonic()
            if response.status_code in (429, 500, 502, 503, 504) and attempt < self.retries:
                delay = 2 ** attempt
                retry_after = response.headers.get("Retry-After")
                if retry_after:
                    try:
                        delay = max(0, float(retry_after))
                    except ValueError:
                        try:
                            delay = max(0, (parsedate_to_datetime(retry_after) - datetime.now(timezone.utc)).total_seconds())
                        except (ValueError, TypeError):
                            pass
                if delay > 60:
                    raise OAIError("retryLater", f"Сервер просит повторить через {delay:.0f} с. Продолжите синхронизацию позднее.")
                self.sleep(delay)
                continue
            response.raise_for_status()
            try:
                root = ET.fromstring(response.content)
            except ET.ParseError as exc:
                raise OAIError("invalidXML", str(exc)) from exc
            if root.tag != OAI + "OAI-PMH":
                raise OAIError("invalidResponse", "Ожидался ответ OAI-PMH")
            error = root.find(OAI + "error")
            if error is not None:
                raise OAIError(error.get("code", "unknown"), text(error))
            return root

    def describe(self):
        identify = self.request(verb="Identify")
        formats = self.request(verb="ListMetadataFormats")
        available = [text(n) for n in formats.findall(".//" + OAI + "metadataPrefix")]
        if not {"eudml-article2", "oai_dc"}.issubset(available):
            raise OAIError("formats", "Нужны форматы eudml-article2 и oai_dc")
        granularity = text(identify.find(".//" + OAI + "granularity"))
        if granularity not in ("YYYY-MM-DD", "YYYY-MM-DDThh:mm:ssZ"):
            raise OAIError("granularity", f"Неизвестная точность дат: {granularity}")
        return {"granularity": granularity, "response_date": text(identify.find(OAI + "responseDate")),
                "deleted_record": text(identify.find(".//" + OAI + "deletedRecord"))}

    def page(self, metadata_prefix, *, token=None, since=None, until=None):
        params = {"verb": "ListRecords"}
        if token:
            params["resumptionToken"] = token
        else:
            params["metadataPrefix"] = metadata_prefix
            if since:
                params["from"] = since
            if until:
                params["until"] = until
        try:
            root = self.request(**params)
        except OAIError as exc:
            if exc.code == "noRecordsMatch":
                return [], None, None
            raise
        listing = root.find(OAI + "ListRecords")
        if listing is None:
            raise OAIError("invalidResponse", "Нет ListRecords")
        token_node = listing.find(OAI + "resumptionToken")
        next_token = text(token_node) or None
        total = token_node.get("completeListSize") if token_node is not None else None
        return listing.findall(OAI + "record"), next_token, int(total) if total and total.isdigit() else None
