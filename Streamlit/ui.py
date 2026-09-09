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

def header(title, username="Зочин"):
    st.markdown(f'<div class="topline"><div class="trail">Мэдлэгийн сан &nbsp; / &nbsp; {text(title)}</div><div class="account">● &nbsp; {text(username)}</div></div>', unsafe_allow_html=True)

def skyline():
    return """<svg class="skyline" viewBox="0 0 800 120" preserveAspectRatio="none" aria-hidden="true"><g fill="#78a9ef"><path d="M0 120V78h30V60h10V78h30V38h45V20h8V38h25V120M170 120V80h15l25-20 25 20h15v40M265 120V58h15V45h35v13h15v62M345 120V75h15V65h10V35h8v30h10v10h15v45M435 120V65h10l30-30 30 30h10v55M550 120V45h15V28h18V10h5v18h18v17h15v75M660 120V65h20V45h25v20h20v55M755 120V65h20V35h25v85"/><path d="M400 93h140v27H400zM390 88l80-18 80 18z"/></g></svg>"""

def hero():
    st.markdown('<div class="hero"><div class="eyebrow">БАЙГУУЛЛАГЫН НЭГДСЭН МЭДЛЭГИЙН САН</div><h1>Баримт бичиг, мэдээллийг<br>нэг дороос.</h1><p>Хэрэгтэй баримтаа олж, мэдлэгээ хуваалцаж, эх сурвалжтай хариулт аваарай.</p>'+skyline()+'</div>', unsafe_allow_html=True)

def action_card(title, subtitle, symbol, variant=""):
    st.markdown(f'<div class="action-card {variant}"><span class="watermark">{symbol}</span><small>ХЯЛБАР ХАНДАЛТ</small><strong>{text(title)}</strong><small>{text(subtitle)}</small></div>', unsafe_allow_html=True)

def doc_card(doc):
    kind = Path(str(doc[3])).suffix.lstrip(".").upper() or "FILE"
    st.markdown(f'<div class="doc-card"><span class="file-badge {kind.lower()}">{text(kind)}</span><h3 title="{text(doc[1])}">{text(doc[1])}</h3><p>{text(doc[2] or "Баримтыг нээж дэлгэрэнгүй мэдээлэлтэй танилцаарай.")}</p></div><div class="doc-meta"><span>{text(doc[5] or "Зохиогч тодорхойгүй")}</span><time>{text(str(doc[6] or "")[:10])}</time></div>', unsafe_allow_html=True)

def empty(title, description):
    st.markdown(f'<div class="empty"><div class="spark">✦</div><h2>{text(title)}</h2><p>{text(description)}</p></div>', unsafe_allow_html=True)

def footer():
    st.markdown('<div class="footer"><span>e-Mongolia · Байгууллагын мэдлэгийн сан</span><span>Дадлагын төсөл · 2026</span></div>', unsafe_allow_html=True)
