# -*- coding: utf-8 -*-
"""
Robô DOE-AL → PMAL
Baixa novas edições do Diário Oficial do Estado de Alagoas,
extrai a seção da Polícia Militar de Alagoas (PMAL) e as menções
à PMAL em outras seções, e gera um site estático em /docs.

Feito para rodar via GitHub Actions (dias úteis) ou manualmente.
"""

import json
import os
import re
import sys
import unicodedata
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests
import pdfplumber

BASE = Path(__file__).parent
STATE_FILE = BASE / "state.json"
DOCS = BASE / "docs"
EDICOES_DIR = DOCS / "edicoes"
DATA_FILE = DOCS / "data" / "edicoes.json"

API_PDF = "https://diario.imprensaoficial.al.gov.br/apinova/api/editions/downloadPdf/{}"
PORTAL = "https://diario.imprensaoficial.al.gov.br/"

# Quantos IDs à frente do último conhecido o robô tenta por execução
PROBE_RANGE = 30
MIN_PDF_BYTES = 40_000  # respostas menores que isso não são uma edição real

TZ_MACEIO = timezone(timedelta(hours=-3))

# Cabeçalhos repetidos em toda página, que poluem o texto extraído
HEADER_PATTERNS = [
    re.compile(r"^Edi[cç][aã]o Eletr[oô]nica Certi\w*cada Digitalmente.*$", re.I),
    re.compile(r"^conforme LEI N[°º]?\s*7\.?397/2012.*$", re.I),
    re.compile(r"^Di[aá]rio O\w*cial$", re.I),
    re.compile(r"^Estado de Alagoas$", re.I),
    re.compile(r"^Maceio?\s*-\s*\w+-feira.*$", re.I),
    re.compile(r"^\d{1,2} de \w+ de \d{4}\s*\d*$", re.I),
]

# Seções que marcam o FIM da seção da PMAL no diário
END_SECTION_MARKERS = [
    "ADMINISTRAÇÃO INDIRETA",
    "Eventos Funcionais",
    "Prefeituras do Interior",
    "PARTICULARES",
]

PMAL_HEADING_RE = re.compile(
    r"Pol[ií]cia Militar do Estado de Alagoas\s*\(PMAL\)", re.I
)

# Termos que indicam menção à PMAL fora da seção própria
MENTION_TERMS = [
    r"\bPMAL\b", r"\bPM/AL\b", r"Pol[ií]cia Militar",
    r"\bCel\.?\s?PM\b", r"\bTen\.?\s?Cel\.?\s?PM\b", r"\bMaj\.?\s?PM\b",
    r"\bCap\.?\s?PM\b", r"\bTen\.?\s?PM\b", r"\bAsp\.?\s?PM\b",
    r"\bSgt\.?\s?PM\b", r"\bCb\.?\s?PM\b", r"\bSd\.?\s?PM\b",
]
MENTION_RE = re.compile("|".join(MENTION_TERMS), re.I)

MESES = {
    "janeiro": 1, "fevereiro": 2, "março": 3, "marco": 3, "abril": 4,
    "maio": 5, "junho": 6, "julho": 7, "agosto": 8, "setembro": 9,
    "outubro": 10, "novembro": 11, "dezembro": 12,
}


# ----------------------------------------------------------------------
# Extração de texto (o DOE é diagramado em DUAS COLUNAS)
# ----------------------------------------------------------------------

def normalize(text: str) -> str:
    """Normaliza ligaduras tipográficas (ﬁ → fi) e espaços."""
    text = unicodedata.normalize("NFKC", text)
    # Corrige ligaduras perdidas na extração ("Oicial" → "Oficial" etc.)
    fixes = {
        "Oicial": "Oficial", "oicial": "oficial",
        "Certiicad": "Certificad", "certiicad": "certificad",
        "especíic": "específic", "Especíic": "Específic",
        "veriicaç": "verificaç", "Veriicaç": "Verificaç",
        "iscal": "fiscal", "Ediicaç": "Edificaç",
        "proission": "profission", "Proission": "Profission",
        "justiicativ": "justificativ", "ratiicad": "ratificad",
        "Ratiicad": "Ratificad", "retiicaç": "retificaç",
        "inanceir": "financeir", "Finanças": "Finanças",
        "eiciência": "eficiência", "eicácia": "eficácia",
        "gratiicaç": "gratificaç", "Gratiicaç": "Gratificaç",
        "notiicaç": "notificaç", "Notiicaç": "Notificaç",
        "classiicaç": "classificaç", "Classiicaç": "Classificaç",
        "qualiicaç": "qualificaç", "Qualiicaç": "Qualificaç",
        "identiicaç": "identificaç", "Identiicaç": "Identificaç",
        "ica ": "fica ", " im ": " fim ",
    }
    for wrong, right in fixes.items():
        text = text.replace(wrong, right)
    return text


def strip_headers(text: str) -> str:
    lines = []
    for line in text.split("\n"):
        stripped = line.strip()
        if any(p.match(stripped) for p in HEADER_PATTERNS):
            continue
        lines.append(line)
    return "\n".join(lines)


def first_page_text(pdf_path: Path) -> str:
    """Texto bruto da primeira página (capa), usado só para data e número."""
    with pdfplumber.open(pdf_path) as pdf:
        page = pdf.pages[0]
        w, h = page.width, page.height
        mid = w / 2
        try:
            left = page.crop((0, 0, mid, h)).extract_text() or ""
            right = page.crop((mid, 0, w, h)).extract_text() or ""
            return normalize(left + "\n" + right)
        except Exception:
            return normalize(page.extract_text() or "")


def extract_pdf_text(pdf_path: Path) -> str:
    """Extrai o texto respeitando as duas colunas de cada página."""
    parts = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            w, h = page.width, page.height
            mid = w / 2
            try:
                left = page.crop((0, 0, mid, h)).extract_text() or ""
                right = page.crop((mid, 0, w, h)).extract_text() or ""
                page_text = left + "\n" + right
            except Exception:
                page_text = page.extract_text() or ""
            parts.append(strip_headers(page_text))
    return normalize("\n".join(parts))


# ----------------------------------------------------------------------
# Interpretação do conteúdo
# ----------------------------------------------------------------------

def parse_metadata(text: str) -> dict:
    """Data e número da edição, lidos da primeira página."""
    meta = {"numero": None, "data": None, "data_texto": None}
    m = re.search(r"Ano\s+\d+\s*-\s*N[uú]mero\s+(\d+)", text)
    if m:
        meta["numero"] = m.group(1)
    m = re.search(r"(\d{1,2})\s+de\s+(\w+)\s+de\s+(\d{4})", text)
    if m:
        dia, mes_nome, ano = m.groups()
        mes = MESES.get(mes_nome.lower())
        if mes:
            meta["data"] = f"{ano}-{mes:02d}-{int(dia):02d}"
            meta["data_texto"] = f"{int(dia)} de {mes_nome.lower()} de {ano}"
    return meta


def find_pmal_section(text: str) -> str:
    """Recorta o trecho entre o título da seção PMAL e a próxima seção."""
    matches = list(PMAL_HEADING_RE.finditer(text))
    if not matches:
        return ""
    # A primeira ocorrência costuma ser o índice; a última é o título real.
    start = matches[-1].end()
    end = len(text)
    for marker in END_SECTION_MARKERS:
        pattern = re.compile(r"^\s*" + re.escape(marker) + r"\s*$", re.M)
        m = pattern.search(text, start)
        if m and m.start() < end:
            end = m.start()
    return text[start:end].strip()


def split_materias(section_text: str) -> list:
    """Divide a seção nas matérias, usando 'Protocolo NNN' como delimitador."""
    if not section_text:
        return []
    materias = []
    chunks = re.split(r"(Protocolo\s+\d+)", section_text)
    buffer = ""
    for chunk in chunks:
        if re.match(r"Protocolo\s+\d+", chunk):
            body = (buffer + chunk).strip()
            if len(body) > 30:
                materias.append(make_materia(body))
            buffer = ""
        else:
            buffer += chunk
    leftover = buffer.strip()
    if len(leftover) > 60:
        materias.append(make_materia(leftover))
    return materias


def make_materia(body: str) -> dict:
    lines = [l.strip() for l in body.split("\n") if l.strip()]
    title = lines[0] if lines else "Matéria"
    # Títulos muito longos ganham reticências
    if len(title) > 120:
        title = title[:117] + "..."
    proto = None
    m = re.search(r"Protocolo\s+(\d+)", body)
    if m:
        proto = m.group(1)
    return {"titulo": title, "protocolo": proto, "texto": body}


def find_mentions(text: str, section_start: int, section_end: int) -> list:
    """Procura menções à PMAL fora da seção própria, com contexto."""
    mentions = []
    seen = set()
    for m in MENTION_RE.finditer(text):
        if section_start <= m.start() < section_end:
            continue  # já está na seção da PMAL
        ctx_start = max(0, text.rfind("\n", 0, m.start() - 400))
        ctx_end = text.find("Protocolo", m.end())
        if ctx_end == -1 or ctx_end - m.start() > 1200:
            ctx_end = min(len(text), m.end() + 600)
        else:
            ctx_end = min(len(text), ctx_end + 20)
        snippet = text[ctx_start:ctx_end].strip()
        snippet = re.sub(r"\s+", " ", snippet)
        key = snippet[:120]
        if key in seen:
            continue
        seen.add(key)
        mentions.append({"termo": m.group(0), "contexto": snippet})
        if len(mentions) >= 40:
            break
    return mentions


def section_bounds(text: str):
    matches = list(PMAL_HEADING_RE.finditer(text))
    if not matches:
        return (len(text), len(text))
    start = matches[-1].start()
    end = len(text)
    for marker in END_SECTION_MARKERS:
        pattern = re.compile(r"^\s*" + re.escape(marker) + r"\s*$", re.M)
        m = pattern.search(text, start)
        if m and m.start() < end:
            end = m.start()
    return (start, end)


# ----------------------------------------------------------------------
# Download e descoberta de edições
# ----------------------------------------------------------------------

def try_download(edition_id: int, dest: Path) -> bool:
    url = API_PDF.format(edition_id)
    try:
        r = requests.get(url, timeout=90, headers={
            "User-Agent": "Mozilla/5.0 (compatible; RoboDOE-PMAL/1.0)",
            "Referer": PORTAL,
        })
    except requests.RequestException as exc:
        print(f"  id {edition_id}: erro de rede ({exc})")
        return False
    if r.status_code != 200:
        print(f"  id {edition_id}: HTTP {r.status_code}")
        return False
    if not r.content.startswith(b"%PDF") or len(r.content) < MIN_PDF_BYTES:
        print(f"  id {edition_id}: resposta não é uma edição em PDF")
        return False
    dest.write_bytes(r.content)
    print(f"  id {edition_id}: PDF baixado ({len(r.content)//1024} KB)")
    return True


# ----------------------------------------------------------------------
# Geração do site (docs/)
# ----------------------------------------------------------------------

CSS = """
:root{
  --tinta:#10151c; --papel:#f7f5f0; --carta:#ffffff;
  --farda:#1e3a5f; --farda-escuro:#14283f; --dourado:#b8860b;
  --cinza:#5c6672; --linha:#e3ded4; --ok:#1e7145;
}
*{box-sizing:border-box}
body{margin:0;background:var(--papel);color:var(--tinta);
  font:16px/1.6 Georgia,'Times New Roman',serif}
header.site{background:var(--farda-escuro);color:#fff;padding:28px 20px;
  border-bottom:4px solid var(--dourado)}
header.site .wrap,main{max-width:900px;margin:0 auto;padding:0 4px}
header.site h1{margin:0;font-size:1.5rem;letter-spacing:.5px;
  font-family:'Trebuchet MS',Verdana,sans-serif;text-transform:uppercase}
header.site p{margin:6px 0 0;opacity:.85;font-size:.9rem}
main{padding:28px 20px 60px}
.card{background:var(--carta);border:1px solid var(--linha);
  border-left:5px solid var(--farda);border-radius:6px;
  padding:18px 22px;margin:0 0 16px}
.card h2{margin:0 0 4px;font-size:1.1rem;color:var(--farda)}
.card .meta{color:var(--cinza);font-size:.85rem;
  font-family:'Trebuchet MS',Verdana,sans-serif}
.badge{display:inline-block;background:var(--farda);color:#fff;
  border-radius:20px;padding:2px 12px;font-size:.78rem;
  font-family:'Trebuchet MS',Verdana,sans-serif;margin-left:8px}
a.card-link{text-decoration:none;color:inherit;display:block}
a.card-link:hover .card{border-left-color:var(--dourado)}
details{background:var(--carta);border:1px solid var(--linha);
  border-radius:6px;margin:0 0 12px;padding:0}
details summary{cursor:pointer;padding:14px 18px;font-weight:bold;
  color:var(--farda);list-style-position:inside}
details[open] summary{border-bottom:1px solid var(--linha)}
details .corpo{padding:14px 18px;white-space:pre-wrap;font-size:.93rem}
.busca{width:100%;padding:12px 16px;font:inherit;border:1px solid var(--linha);
  border-radius:6px;margin:0 0 18px;background:var(--carta)}
h3.secao{font-family:'Trebuchet MS',Verdana,sans-serif;color:var(--farda);
  text-transform:uppercase;font-size:.9rem;letter-spacing:1.5px;
  border-bottom:2px solid var(--dourado);padding-bottom:6px;margin:34px 0 16px}
.aviso{background:#fdf6e3;border:1px solid #e8d9a0;border-radius:6px;
  padding:12px 16px;font-size:.88rem;color:#6b5900;margin:0 0 18px}
.voltar{font-family:'Trebuchet MS',Verdana,sans-serif;font-size:.85rem}
.voltar a{color:var(--farda)}
footer{max-width:900px;margin:0 auto;padding:20px;color:var(--cinza);
  font-size:.8rem;border-top:1px solid var(--linha)}
mark{background:#ffe9a8}
"""


def html_page(title: str, body: str, subtitle: str = "") -> str:
    return f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>{CSS}</style>
</head>
<body>
<header class="site"><div class="wrap">
<h1>DOE-AL &middot; Clipping PMAL</h1>
<p>{subtitle or "Matérias da Polícia Militar de Alagoas no Diário Oficial do Estado — compilação automática"}</p>
</div></header>
<main>
{body}
</main>
<footer>Compilação automática de caráter informativo. O documento oficial é a edição
certificada digitalmente publicada pela Imprensa Oficial Graciliano Ramos em
<a href="https://diario.imprensaoficial.al.gov.br/">diario.imprensaoficial.al.gov.br</a>.</footer>
</body></html>"""


def esc(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def build_edition_page(ed: dict) -> str:
    body = [f'<p class="voltar"><a href="../index.html">&larr; Todas as edições</a> &nbsp;&middot;&nbsp; '
            f'<a href="{ed["numero"]}.pdf">Baixar compilado PMAL (PDF)</a> &nbsp;&middot;&nbsp; '
            f'<a href="{API_PDF.format(ed["id"])}" target="_blank">Edição completa (PDF oficial)</a></p>']
    body.append(f'<h2>Edição nº {ed["numero"]} — {ed["data_texto"]}'
                f'<span class="badge">{len(ed["materias"])} matéria(s) PMAL</span></h2>')

    if not ed["materias"] and not ed["mencoes"]:
        body.append('<div class="aviso">Nenhuma matéria da PMAL foi localizada nesta edição.</div>')

    if ed["materias"]:
        body.append('<input class="busca" id="busca" type="search" '
                    'placeholder="Filtrar matérias por nome, posto, matrícula...">')
        body.append('<h3 class="secao">Seção Polícia Militar do Estado de Alagoas (PMAL)</h3>')
        for mat in ed["materias"]:
            proto = f' &middot; Protocolo {mat["protocolo"]}' if mat["protocolo"] else ""
            body.append(
                f'<details class="mat"><summary>{esc(mat["titulo"])}'
                f'<span class="meta">{proto}</span></summary>'
                f'<div class="corpo">{esc(mat["texto"])}</div></details>'
            )

    if ed["mencoes"]:
        body.append('<h3 class="secao">Menções à PMAL em outras seções</h3>')
        for men in ed["mencoes"]:
            body.append(
                f'<details class="mat"><summary>Menção: {esc(men["termo"])}</summary>'
                f'<div class="corpo">{esc(men["contexto"])}</div></details>'
            )

    body.append("""<script>
const campo=document.getElementById('busca');
if(campo){campo.addEventListener('input',()=>{
  const q=campo.value.toLowerCase();
  document.querySelectorAll('details.mat').forEach(d=>{
    d.style.display=d.textContent.toLowerCase().includes(q)?'':'none';
  });
});}
</script>""")
    subtitle = f'Edição nº {ed["numero"]} — {ed["data_texto"]}'
    return html_page(f'DOE-AL {ed["numero"]} — PMAL', "\n".join(body), subtitle)


def build_index(editions: list) -> str:
    body = []
    if not editions:
        body.append('<div class="aviso">Nenhuma edição processada ainda. '
                    'O robô roda automaticamente nos dias úteis.</div>')
    else:
        body.append('<h3 class="secao">Edições compiladas</h3>')
        for ed in sorted(editions, key=lambda e: (e.get("data") or "", e["numero"]), reverse=True):
            n_mat = ed.get("qtd_materias", 0)
            n_men = ed.get("qtd_mencoes", 0)
            body.append(
                f'<a class="card-link" href="edicoes/{ed["numero"]}.html"><div class="card">'
                f'<h2>Edição nº {ed["numero"]}</h2>'
                f'<div class="meta">{ed["data_texto"]} &middot; {n_mat} matéria(s) na seção PMAL'
                f' &middot; {n_men} menção(ões) em outras seções</div>'
                f'</div></a>'
            )
    return html_page("DOE-AL — Clipping PMAL", "\n".join(body))


# ----------------------------------------------------------------------
# Geração do PDF compilado
# ----------------------------------------------------------------------

def gerar_pdf(ed: dict, dest: Path):
    """Gera um PDF só com o conteúdo da PMAL, pronto para envio por e-mail."""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import cm
    from reportlab.lib import colors
    from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer,
                                    HRFlowable)

    def p(texto: str) -> str:
        texto = (texto.replace("&", "&amp;")
                       .replace("<", "&lt;").replace(">", "&gt;"))
        return texto.replace("\n", "<br/>")

    styles = getSampleStyleSheet()
    st_titulo = ParagraphStyle("titulo", parent=styles["Title"],
                               fontName="Helvetica-Bold", fontSize=15,
                               textColor=colors.HexColor("#14283f"),
                               spaceAfter=2)
    st_sub = ParagraphStyle("sub", parent=styles["Normal"], fontSize=10,
                            textColor=colors.HexColor("#5c6672"),
                            spaceAfter=12)
    st_secao = ParagraphStyle("secao", parent=styles["Heading2"],
                              fontName="Helvetica-Bold", fontSize=11,
                              textColor=colors.HexColor("#1e3a5f"),
                              spaceBefore=14, spaceAfter=6)
    st_mat_titulo = ParagraphStyle("mat", parent=styles["Normal"],
                                   fontName="Helvetica-Bold", fontSize=9.5,
                                   textColor=colors.HexColor("#14283f"),
                                   spaceBefore=10, spaceAfter=3)
    st_corpo = ParagraphStyle("corpo", parent=styles["Normal"],
                              fontName="Helvetica", fontSize=8.5,
                              leading=11.5, spaceAfter=6)

    doc = SimpleDocTemplate(str(dest), pagesize=A4,
                            leftMargin=1.8 * cm, rightMargin=1.8 * cm,
                            topMargin=1.6 * cm, bottomMargin=1.6 * cm,
                            title=f"DOE-AL {ed['numero']} - Clipping PMAL")
    story = [
        Paragraph("DOE-AL · Clipping PMAL", st_titulo),
        Paragraph(f"Edição nº {ed['numero']} — {ed['data_texto']} · "
                  f"{len(ed['materias'])} matéria(s) na seção PMAL · "
                  f"{len(ed['mencoes'])} menção(ões) em outras seções", st_sub),
        HRFlowable(width="100%", thickness=1.2,
                   color=colors.HexColor("#b8860b")),
    ]

    if ed["materias"]:
        story.append(Paragraph(
            "SEÇÃO POLÍCIA MILITAR DO ESTADO DE ALAGOAS (PMAL)", st_secao))
        for i, mat in enumerate(ed["materias"], 1):
            proto = f" · Protocolo {mat['protocolo']}" if mat["protocolo"] else ""
            story.append(Paragraph(f"{i}. {p(mat['titulo'])}{proto}",
                                   st_mat_titulo))
            story.append(Paragraph(p(mat["texto"]), st_corpo))
    else:
        story.append(Paragraph(
            "Nenhuma matéria localizada na seção da PMAL nesta edição.",
            st_corpo))

    if ed["mencoes"]:
        story.append(Paragraph("MENÇÕES À PMAL EM OUTRAS SEÇÕES", st_secao))
        for i, men in enumerate(ed["mencoes"], 1):
            story.append(Paragraph(f"{i}. Termo encontrado: {p(men['termo'])}",
                                   st_mat_titulo))
            story.append(Paragraph(p(men["contexto"]), st_corpo))

    story.append(Spacer(1, 12))
    story.append(HRFlowable(width="100%", thickness=0.6,
                            color=colors.HexColor("#e3ded4")))
    story.append(Paragraph(
        "Compilação automática de caráter informativo. O documento oficial é a "
        "edição certificada digitalmente publicada pela Imprensa Oficial "
        "Graciliano Ramos.", st_sub))
    doc.build(story)


# ----------------------------------------------------------------------
# Fluxo principal
# ----------------------------------------------------------------------

def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"last_id": 51739, "processed": []}


def load_editions() -> list:
    if DATA_FILE.exists():
        return json.loads(DATA_FILE.read_text(encoding="utf-8"))
    return []


def process_pdf(edition_id: int, pdf_path: Path) -> dict:
    text = extract_pdf_text(pdf_path)
    meta = parse_metadata(first_page_text(pdf_path))
    start, end = section_bounds(text)
    section = find_pmal_section(text)
    materias = split_materias(section)
    mencoes = find_mentions(text, start, end)
    hoje = datetime.now(TZ_MACEIO)
    return {
        "id": edition_id,
        "numero": meta["numero"] or str(edition_id),
        "data": meta["data"],
        "data_texto": meta["data_texto"] or "data não identificada",
        "materias": materias,
        "mencoes": mencoes,
        "processado_em": hoje.isoformat(),
    }


def main():
    state = load_state()
    editions_index = load_editions()
    processed_ids = set(state.get("processed", []))

    EDICOES_DIR.mkdir(parents=True, exist_ok=True)
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)

    # Modo de teste local: python scraper.py caminho/edicao.pdf ID
    if len(sys.argv) >= 2 and sys.argv[1].endswith(".pdf"):
        pdf = Path(sys.argv[1])
        eid = int(sys.argv[2]) if len(sys.argv) > 2 else 0
        ed = process_pdf(eid, pdf)
        print(json.dumps({k: (len(v) if isinstance(v, list) else v)
                          for k, v in ed.items()}, indent=2, ensure_ascii=False))
        (EDICOES_DIR / f'{ed["numero"]}.html').write_text(
            build_edition_page(ed), encoding="utf-8")
        gerar_pdf(ed, EDICOES_DIR / f'{ed["numero"]}.pdf')
        summary = {k: ed[k] for k in ("id", "numero", "data", "data_texto")}
        summary["qtd_materias"] = len(ed["materias"])
        summary["qtd_mencoes"] = len(ed["mencoes"])
        editions_index = [e for e in editions_index if e["numero"] != ed["numero"]]
        editions_index.append(summary)
        DATA_FILE.write_text(json.dumps(editions_index, ensure_ascii=False, indent=1),
                             encoding="utf-8")
        (DOCS / "index.html").write_text(build_index(editions_index), encoding="utf-8")
        print("Página gerada em docs/edicoes/")
        return

    last_id = state["last_id"]
    print(f"Último ID conhecido: {last_id}. Sondando até {last_id + PROBE_RANGE}...")
    found_any = False

    for candidate in range(last_id + 1, last_id + PROBE_RANGE + 1):
        if candidate in processed_ids:
            continue
        tmp = BASE / f"_tmp_{candidate}.pdf"
        if not try_download(candidate, tmp):
            continue
        try:
            ed = process_pdf(candidate, tmp)
        finally:
            tmp.unlink(missing_ok=True)

        print(f"  → Edição nº {ed['numero']} ({ed['data_texto']}): "
              f"{len(ed['materias'])} matérias PMAL, {len(ed['mencoes'])} menções")

        (EDICOES_DIR / f'{ed["numero"]}.html').write_text(
            build_edition_page(ed), encoding="utf-8")
        gerar_pdf(ed, EDICOES_DIR / f'{ed["numero"]}.pdf')

        # Sinaliza ao workflow que há novidade para enviar por e-mail
        with open(BASE / "novas.txt", "a", encoding="utf-8") as f:
            f.write(f"docs/edicoes/{ed['numero']}.pdf\n")
        with open(BASE / "resumo.txt", "a", encoding="utf-8") as f:
            f.write(f"Edição nº {ed['numero']} — {ed['data_texto']}: "
                    f"{len(ed['materias'])} matéria(s) na seção PMAL, "
                    f"{len(ed['mencoes'])} menção(ões) em outras seções.\n")

        summary = {k: ed[k] for k in ("id", "numero", "data", "data_texto")}
        summary["qtd_materias"] = len(ed["materias"])
        summary["qtd_mencoes"] = len(ed["mencoes"])
        editions_index = [e for e in editions_index if e["numero"] != ed["numero"]]
        editions_index.append(summary)

        processed_ids.add(candidate)
        state["last_id"] = max(state["last_id"], candidate)
        found_any = True

    state["processed"] = sorted(processed_ids)[-200:]
    STATE_FILE.write_text(json.dumps(state, indent=1), encoding="utf-8")
    DATA_FILE.write_text(json.dumps(editions_index, ensure_ascii=False, indent=1),
                         encoding="utf-8")
    (DOCS / "index.html").write_text(build_index(editions_index), encoding="utf-8")

    if found_any:
        print("Concluído: novas edições compiladas.")
    else:
        print("Nenhuma edição nova encontrada nesta execução.")


if __name__ == "__main__":
    main()
