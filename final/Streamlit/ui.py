"""Shared desktop presentation components."""
from html import escape
from pathlib import Path
import streamlit as st

def text(value):
    return escape(str(value or ""))

def styles():
    css = (Path(__file__).parent / "assets" / "desktop.css").read_text(encoding="utf-8")
    if st.session_state.get("dark_ui", False):
        css += """
        :root {--ink:#e5edfa;--muted:#9aabc2;--line:#293b57;--panel:#172840;--canvas:#0d1b30;}
        .hero {background:linear-gradient(115deg,#142f59,#152c48);border-color:#294665;}
        .hero h1 {color:#e5edfa;} .hero p {color:#a7bddd;}
        [data-testid="stTextInput"] input,[data-testid="stTextArea"] textarea {background:#172840;color:#e5edfa;}
        [data-baseweb="select"]>div {background:#172840;color:#e5edfa;}
        .stButton button,.stDownloadButton button,[data-testid="stPopover"] button {background:#172840;color:#e5edfa;}
        [data-testid="stChatInput"] textarea {color:#e5edfa;background:#172840;}
        [data-testid="stChatInput"], [data-testid="stChatInput"]>div {background:#172840;}
        [data-testid="stAlert"] p {color:#f4d482;}
        [data-testid="stDialog"] [role="dialog"] {background:#172840;color:#e5edfa;}
        [data-testid="stPopoverBody"] {background:#172840;color:#e5edfa;}
        [data-testid="stFileUploaderDropzone"] {background:#203652;color:#e5edfa;}
        [data-testid="stBottom"]>div {background:#0d1b30;}
        """
    st.markdown("<style>" + css + "</style>", unsafe_allow_html=True)

def brand():
    st.markdown('<div class="brand"><b>e</b>mongolia</div><div class="brand-sub">МЭДЛЭГИЙН САН</div>', unsafe_allow_html=True)

def header(title):
    st.markdown(f'<div class="topline"><div class="trail">Мэдлэгийн сан &nbsp; / &nbsp; {text(title)}</div></div>', unsafe_allow_html=True)

def author_name(value):
    name = str(value or "").strip()
    if name.casefold() in {"legalinfo.mn сайтаас", "legalinfo.mn сайт-аас"}:
        return "legalinfo.mn"
    return name

def skyline():
    return """<svg class="skyline" viewBox="0 0 800 120" preserveAspectRatio="none" aria-hidden="true"><g fill="#78a9ef"><path d="M0 120V78h30V60h10V78h30V38h45V20h8V38h25V120M170 120V80h15l25-20 25 20h15v40M265 120V58h15V45h35v13h15v62M345 120V75h15V65h10V35h8v30h10v10h15v45M435 120V65h10l30-30 30 30h10v55M550 120V45h15V28h18V10h5v18h18v17h15v75M660 120V65h20V45h25v20h20v55M755 120V65h20V35h25v85"/><path d="M400 93h140v27H400zM390 88l80-18 80 18z"/></g></svg>"""

def hero():
    st.markdown('<div class="hero"><div class="eyebrow">БАЙГУУЛЛАГЫН НЭГДСЭН МЭДЛЭГИЙН САН</div><h1>Баримт бичиг, мэдээллийг<br>нэг дороос.</h1><p>Хэрэгтэй баримтаа олж, мэдлэгээ хуваалцаж, эх сурвалжтай хариулт аваарай.</p>'+skyline()+'</div>', unsafe_allow_html=True)

def doc_card(doc):
    st.caption(f'Баримт #{doc[0]}')
    kind = Path(str(doc[3])).suffix.lstrip(".").upper() or "FILE"
    category = f'<span class="category-badge">{text(doc[7])}</span>' if len(doc) > 7 and doc[7] else ""
    tag_items = doc[8] if len(doc) > 8 else []
    tags = "".join(f'<span class="tag-badge">#{text(tag)}</span>' for tag in tag_items[:3])
    st.markdown(f'<div class="doc-card"><div class="document-labels"><span class="file-badge {kind.lower()}">{text(kind)}</span>{category}</div><h3 title="{text(doc[1])}">{text(doc[1])}</h3><p>{text(doc[2] or "Баримтыг нээж дэлгэрэнгүй мэдээлэлтэй танилцаарай.")}</p><div class="tag-list">{tags}</div></div><div class="doc-meta"><span>{text(doc[5] or "Зохиогч тодорхойгүй")}</span><time>{text(str(doc[6] or "")[:10])}</time></div>', unsafe_allow_html=True)

def empty(title, description):
    icon = '<svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"><path d="M5 4h14a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H9l-6 4V6a2 2 0 0 1 2-2Z"/><path d="M7 9h10M7 13h7"/></svg>'
    st.markdown(f'<div class="empty"><div class="empty-icon">{icon}</div><h2>{text(title)}</h2><p>{text(description)}</p></div>', unsafe_allow_html=True)

def users_table(users):
    roles = {"Admin": "Администратор", "User": "Хэрэглэгч"}
    statuses = {"active": "Идэвхтэй", "inactive": "Идэвхгүй"}
    rows = "".join("<tr>" + "".join(f"<td>{text(v)}</td>" for v in
                  (name, roles.get(role, role), statuses.get(status, status), created)) + "</tr>"
                  for name, role, status, created in users)
    if not rows:
        rows = '<tr><td colspan="4">Бүртгэлтэй хэрэглэгч алга.</td></tr>'
    headings = "".join(f'<th scope="col">{label}</th>' for label in
                       ("Нэвтрэх нэр", "Эрх", "Төлөв", "Бүртгүүлсэн огноо"))
    st.markdown(f'<div class="users-table" role="region" aria-label="Бүртгэлтэй хэрэглэгчид" tabindex="0"><table><thead><tr>{headings}</tr></thead><tbody>{rows}</tbody></table></div>', unsafe_allow_html=True)

def footer():
    st.markdown('<div class="footer"><span>e-Mongolia · Байгууллагын мэдлэгийн сан</span></div>', unsafe_allow_html=True)
