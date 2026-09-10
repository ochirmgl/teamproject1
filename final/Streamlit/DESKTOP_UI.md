# Desktop interface

The application remains Streamlit + SQLite. The desktop presentation follows
e-Mongolia's blue/white palette, prominent search, rounded cards, and clear navigation.
The wordmark and skyline are locally drawn approximations, not official brand assets.

## Run

From this `Streamlit` directory, install `requirements.txt` into your Python environment
and run `python -m streamlit run app.py`. Start here so Streamlit loads the included
`.streamlit/config.toml` theme. The local preview is normally at http://localhost:8501.

## Included

- Login and registration forms with validation.
- Document cards, title/author/category/tag search, category/tag/author/type filters, ordering and empty states.
- Administrator category and tag management, with assignment during upload and editing.
- Document preview, downloads and existing administrator editing actions.
- Upload form and administrator user list.
- AI chat with sidebar history visible only on the AI page, document selection and source panels.
- Shared light/dark styling, page headings, account details and footer.

The library uses sidebar navigation without duplicate shortcut cards. Active search
filters have an explicitly labeled clear action and persist when navigating away.
The image-type filter is omitted; existing files and upload support are unchanged.
The users list is a theme-aware, read-only HTML table without export controls.
This removes the convenience download button, not users' ability to copy visible data.
The sidebar shows the signed-in username without repeating its role. When collapsed,
its reopen control stays attached to the left edge at the middle of the screen. Dark
mode explicitly colors previous chat messages for readable contrast.

Shared presentation lives in `ui.py`, `assets/desktop.css` and `.streamlit/config.toml`.
Run `python -m unittest test_ui -v` for isolated UI regression checks. These tests use
temporary files/databases and do not modify the project data or call the AI provider.

## Categories and tags

Administrators can open **Ангилал ба шошго** in the sidebar to create, rename,
or delete labels. Assign one category and multiple tags when uploading or editing
a document. Assigned labels appear on library cards and in search/filter controls.
Deleting a category or tag removes its assignments, not the documents themselves.
Existing documents are left uncategorized until you choose their labels.

## Safe document management and administration

- In a document's **Үйлдлүүд**, choose **Хувилбарын түүх** to download or restore
  previous file versions. Restoration changes the file, not its category or description.
- Deletion moves the document to **Хогийн сав**. Restore it there; no permanent-delete
  control is provided. Deleted documents are excluded from new library/AI retrieval.
  Previously saved chat answers are historical records and are not erased.
- Uploads use unique storage names, so identical original filenames never overwrite
  one another. Empty files, unsupported types, mismatched basic format checks and files
  over 50 MB are rejected. TXT/CSV must be UTF-8. These checks are not malware scanning;
  legacy DOC/XLS checks only verify the container signature.
- **Хэрэглэгчид** allows administrators to change roles and activate/deactivate users.
  Changes require confirmation. The last active administrator cannot be removed.
  Account permissions refresh on app interactions; already delivered content cannot be revoked.
- Document changes and user-access changes are recorded in `activity_logs`.
- Schema upgrades create a SQLite backup in `database_backups`. Original files are
  retained on replacement; filesystem writes complete before database changes commit.
  A process crash can leave an unreferenced new file but does not delete the original.

Run `python -m unittest test_management test_metadata test_ui test_database -q` for isolated checks.

## Boundaries

This is not a completed backend rebuild. Provider selection, OCR and stronger RAG
grounding remain separate work. Access is role-based (Admin/User), not per-document ACLs.
Live AI answers require a configured provider key. Office-to-PDF
preview requires LibreOffice; original file downloads remain available without it.

## Document processing

Open **Баримт боловсруулалт** to check statuses and process existing documents.
On a document card, **Үйлдлүүд → Задалсан агуулга** opens extracted sections and
a retry control. Uploading, replacing, or restoring a file processes its current
content automatically in the foreground; there is no background worker yet.
Processing is local and makes no paid AI calls.

- PDF: page labels and layout-oriented text extraction. Complex tables and diagrams
  still need manual review; text extraction does not guarantee visual fidelity.
- DOCX: headings, paragraphs and tables in body order. Locations use section/paragraph
  references, not invented page numbers. Headers, footers and text boxes are not extracted.
- TXT: UTF-8 content and line ranges. CSV: delimited records with first-row header labels.
- XLSX/XLS: sheet and row references. XLSX includes formulas and stored calculated values;
  missing cached formula values produce a partial-status warning, not invented results.
- Images and textless PDFs are flagged for OCR. Textless PDF pages can also be blank;
  mixed PDFs show a partial result and identify the pages to review. Legacy DOC needs DOCX conversion.

SQLite stores processing status, warnings and extracted sections. Content hashes avoid
unnecessary extraction. Replacement invalidates the old extraction in the same transaction,
and stale processing results cannot publish after replacement or deletion. Failed runs can
be retried. There is a 50 MB input and 5 million extracted-character safety limit.

Run `python -m unittest test_processing test_management test_metadata test_ui test_database -q`
for the full regression suite. `python processing.py` processes active documents locally.
