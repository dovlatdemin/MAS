# NUMDAM XML fixtures

Downloaded from the official NUMDAM OAI-PMH endpoint on 2026-10-29. The two article files contain individual records extracted from the first `ListRecords` response, without editing the metadata. The date fixture contains a record extracted from a `GetRecord` response.

- `numdam_article.xml`: `oai:numdam.org:MSH_1994__126__5_0`, no abstract, format `eudml-article2`.
- `numdam_with_abstract.xml`: `oai:numdam.org:MSH_1994__125__5_0`, French and English abstracts, format `eudml-article2`.
- `numdam_date.xml`: `oai:numdam.org:MSH_1994__125__5_0`, format `oai_dc`, publication year 1994.

Sources:

- https://www.numdam.org/oai?verb=ListRecords&metadataPrefix=eudml-article2
- https://www.numdam.org/oai?verb=GetRecord&identifier=oai:numdam.org:MSH_1994__125__5_0&metadataPrefix=oai_dc
- https://numdam.org/metadata/

The tests also generate small synthetic records to exercise deletion markers, missing fields, filtering, weights, pagination and failures. No test contacts these URLs or downloads article HTML/PDF files.
