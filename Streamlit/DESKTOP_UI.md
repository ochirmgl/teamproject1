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
- Document cards, title/author search, author/type filters, ordering and empty states.
- Document preview, downloads and existing administrator editing actions.
- Upload form and administrator user list.
- AI chat with sidebar history, document selection and source panels.
- Shared light/dark styling, page headings, account details and footer.

Shared presentation lives in `ui.py`, `assets/desktop.css` and `.streamlit/config.toml`.
Run `python -m unittest test_ui -v` for isolated UI regression checks. These tests use
temporary files/databases and do not modify the project data or call the AI provider.

## Boundaries

This is a desktop UI pass, not a completed backend rebuild. Category/tag management,
file versioning and safer replacement, provider selection, OCR and stronger RAG
grounding remain separate work. Existing file replacement still needs the planned
safety repair. Live AI answers require a configured provider key. Office-to-PDF
preview requires LibreOffice; original file downloads remain available without it.
