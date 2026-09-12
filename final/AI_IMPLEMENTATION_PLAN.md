# Free AI and original document preview

## Agreed scope

Streamlit and SQLite remain. Use free AI only. Do not enable billing or silently
switch to a paid provider. The browser must show original pages, images and tables;
extracted text is reserved for processing/search, not the document viewer.

## Implementation order

1. Original viewer: PDF pages, Office-to-PDF conversion (LibreOffice), original images,
   literal TXT/CSV content. Cache conversions by content hash, show page navigation,
   keep an original download. If conversion is unavailable, explicitly report it.
2. Free providers: Gemini free-tier project and local Ollama adapter. One answer call
   per question, bounded context/output, no automatic translation/retry calls, local
   daily request limits and recorded token usage. Never switch providers automatically.
3. Retrieval: enforce selected active documents, improve Mongolian matching and
   paragraph context, combine keyword and optional local semantic retrieval, then
   rerank and deduplicate passages. No match means no generation call.
4. Evidence: structured answers with per-claim source IDs and exact supporting quotes.
   Validate source IDs/quotes before displaying an answer. Identify insufficient or
   conflicting evidence. Cite the saved file version, not a later replacement.
5. Evaluation: Mongolian answerable/unanswerable questions, empty selections,
   malicious document instructions, stale versions, quota failures, bad citations.
   Local deterministic tests first; hosted model accuracy requires user-enabled
   free-tier credentials and a separate live evaluation, not a claim from mocks.

## Provider recommendation (checked 2026-09-11)

For the current public test documents, start with Gemini 2.5 Flash's free tier;
compare Flash-Lite if quota/speed is the constraint. Neither is unlimited. The app
cannot determine billing status from an API key: use a project without billing.
For confidential company documents, prefer a company-approved local Ollama model.
Local inference has hardware costs and requires its own model/language evaluation.
Gemini free-tier content may be used to improve Google's products, so do not treat
the free endpoint as approved for confidential company policies by default.

Sources:
- https://ai.google.dev/gemini-api/docs/pricing
- https://ai.google.dev/gemini-api/docs/structured-output
- https://docs.ollama.com/api/chat

## Acceptance boundaries

LibreOffice preserves page layout much better than extracted text, but missing
fonts or Office-specific features can differ from Microsoft Word. Test representative
files. TXT/CSV have no original paginated layout. No implementation can promise
zero hallucinations; citation validation proves quote presence, not entailment.
Streamlit Cloud's local disk is not durable storage; production persistence remains
a deployment acceptance requirement even while SQLite stays the database engine.

## Implementation status

- Implemented: original-page viewer and converter cache; Gemini/Ollama adapters;
  request accounting/cache; bounded one-call answers; selected-document filtering;
  keyword reranking; evidence validation; version-linked source opening; admin usage page.
- Optional semantic adapter implemented but no model installed or evaluated.
- Local LibreOffice install retry was declined. Full Office preview still needs the
  converter installed locally or a successful deployed dependency rebuild.
- No live generation API requests made. Free-project configuration and live Mongolian
  accuracy evaluation are still required. This file describes completed code and the
  remaining operational checks separately; step 4 is not declared fully finished.
- Verification: 41 automated tests passed. After requiring coverage of at least half
  the meaningful query words in lexical retrieval, the five-question smoke test
  passed 5/5, including the unrelated quantum-computing question. This conservative
  rule can reduce recall; a larger evaluation set and live answer tests are still
  needed before relying on the assistant for company policy.
- The downloaded local LibreOffice installer was permanently removed at the user's
  request. Deployment package configuration remains in the repository.
