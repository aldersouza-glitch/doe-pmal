# -*- coding: utf-8 -*-
"""
Robô DOE-AL → PMAL
Baixa novas edições do Diário Oficial do Estado de Alagoas,
extrai a seção da Polícia Militar de Alagoas (PMAL) e matérias
de outras seções que mencionem a PMAL, com indicação de página.
"""

import json
import re
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

PROBE_RANGE = 30
MIN_PDF_BYTES = 40_000
TZ_MACEIO = timezone(timedelta(hours=-3))

HEADER_PATTERNS = [
    re.compile(r"^Edi[cç][aã]o Eletr[oô]nica Certi\w*cada Digitalmente.*$", re.I),
    re.compile(r"^conforme LEI N[°º]?\s*7\.?397/2012.*$", re.I),
    re.compile(r"^Di[aá]rio O\w*cial$", re.I),
    re.compile(r"^Estado de Alagoas$", re.I),
    re.compile(r"^Maceio?\s*-\s*\w+-feira.*$", re.I),
    re.compile(r"^\d{1,2} de \w+ de \d{4}\s*\d*$", re.I),
]

MESES = {
    "janeiro": 1, "fevereiro": 2, "março": 3, "marco": 3, "abril": 4,
    "maio": 5, "junho": 6, "julho": 7, "agosto": 8, "setembro": 9,
    "outubro": 10, "novembro": 11, "dezembro": 12,
}

FIXES = {
    "Oicial": "Oficial", "oicial": "oficial",
    "Certiicad": "Certificad", "certiicad": "certificad",
    "especíic": "específic", "veriicaç": "verificaç",
    "iscal": "fiscal", "proission": "profission",
    "justiicativ": "justificativ", "ratiicad": "ratificad",
    "retiicaç": "retificaç", "inanceir": "financeir",
    "eiciência": "eficiência", "eicácia": "eficácia",
    "gratiicaç": "gratificaç", "notiicaç": "notificaç",
    "classiicaç": "classificaç", "qualiicaç": "qualificaç",
    "identiicaç": "identificaç", "ica ": "fica ", " im ": " fim ",
}

# Regex pra encontrar a PMAL no índice do diário
INDEX_PMAL_RE = re.compile(
    r"Pol[ií]cia\s+Militar.*?\(PMAL\).*?(\d+)\s*$", re.I | re.M
)

# Seções que vêm DEPOIS da PMAL no índice
NEXT_SECTIONS_RE = re.compile(
    r"(?:ADMINISTRA[CÇ][AÃ]O\s+INDIRETA|Eventos\s+Funcionais|"
    r"Prefeituras\s+do\s+Interior|PARTICULARES).*?(\d+)\s*$",
    re.I | re.M
)

# Termos que identificam matéria da PMAL em outras seções
PMAL_TERMS_RE = re.compile(
    r"\bPMAL\b|\bPM/AL\b|\bPM\-AL\b|\bPM\s+AL\b"
    r"|Pol[ií]cia\s+Militar\s+d[eo]\s+(?:Estado|Alagoas)"
    r"|Policia\s+Militar\s+do\s+Estado"
    r"|Comando\s*Geral\s*da\s*PM"
    # Abreviações com PM
    r"|\bCel\.?\s*PM\b|\bTen\.?\s*Cel\.?\s*PM\b|\bMaj\.?\s*PM\b"
    r"|\bCap\.?\s*PM\b|\bTen\.?\s*PM\b|\bAsp\.?\s*PM\b"
    r"|\bSubten\.?\s*PM\b|\bSgt\.?\s*PM\b|\bCb\.?\s*PM\b|\bSd\.?\s*PM\b"
    # Abreviações com QP/QO PM
    r"|\bQP\s*PM\b|\bQO\s*PM\b"
    # Patentes por extenso (captura o contexto PM AL no bloco)
    r"|PRIMEIRO\s+SARGENTO|SEGUNDO\s+SARGENTO|TERCEIRO\s+SARGENTO"
    r"|SUB\s*TENENTE|SUBTENENTE|PRIMEIRO\s+TENENTE|SEGUNDO\s+TENENTE"
    r"|CAPIT[AÃ]O|MAJOR|TENENTE\s+CORONEL|CORONEL"
    r"|CABO\s+PM|SOLDADO\s+PM"
    # Diárias militar (DETRAN, SSP, etc.)
    r"|DI[AÁ]RIAS\s*[-–]?\s*MILITAR",
    re.I
)

# Termos que indicam Corpo de Bombeiros (pra EXCLUIR)
BM_EXCLUDE_RE = re.compile(
    r"\bCBM\b|\bCBMAL\b|\bCBM/AL\b|Bombeiro|\bBM\b"
    r"|Corpo\s+de\s+Bombeiros",
    re.I
)


def normalize(text):
    text = unicodedata.normalize("NFKC", text)
    for wrong, right in FIXES.items():
        text = text.replace(wrong, right)
    return text


def strip_headers(text):
    lines = []
    for line in text.split("\n"):
        stripped = line.strip()
        if any(p.match(stripped) for p in HEADER_PATTERNS):
            continue
        lines.append(line)
    return "\n".join(lines)


def extract_page_text(page):
    w, h = page.width, page.height
    mid = w / 2
    try:
        left = page.crop((0, 0, mid, h)).extract_text() or ""
        right = page.crop((mid, 0, w, h)).extract_text() or ""
        return normalize(strip_headers(left + "\n" + right))
    except Exception:
        return normalize(strip_headers(page.extract_text() or ""))


def first_page_text(pdf_path):
    with pdfplumber.open(pdf_path) as pdf:
        return extract_page_text(pdf.pages[0])


def parse_metadata(text):
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


def find_pmal_page_range(index_text):
    m = INDEX_PMAL_RE.search(index_text)
    if not m:
        return None, None
    pag_inicio = int(m.group(1))
    pag_fim = None
    for m2 in NEXT_SECTIONS_RE.finditer(index_text):
        p = int(m2.group(1))
        if p > pag_inicio:
            pag_fim = p
            break
    if pag_fim is None:
        pag_fim = pag_inicio + 15
    return pag_inicio, pag_fim


def make_materia(body, pagina_ini, pagina_fim, origem="PMAL"):
    lines = [l.strip() for l in body.split("\n") if l.strip()]
    titulo = lines[0] if lines else "Matéria"
    if len(titulo) > 120:
        titulo = titulo[:117] + "..."
    proto = None
    pm = re.search(r"Protocolo\s+(\d+)", body)
    if pm:
        proto = pm.group(1)
    return {
        "titulo": titulo,
        "protocolo": proto,
        "texto": body,
        "pagina_ini": pagina_ini,
        "pagina_fim": pagina_fim,
        "origem": origem,
    }


def extract_all_pmal(pdf_path):
    """Extrai:
    1) Todas as matérias da seção oficial da PMAL (pelo índice)
    2) Matérias de OUTRAS seções que mencionem a PMAL
    """
    with pdfplumber.open(pdf_path) as pdf:
        total_pages = len(pdf.pages)

        # --- Ler índice (primeiras 5 páginas) ---
        index_text = ""
        for p in pdf.pages[:5]:
            index_text += extract_page_text(p) + "\n"

        pag_inicio, pag_fim = find_pmal_page_range(index_text)
        pmal_range = set()
        if pag_inicio:
            pmal_range = set(range(pag_inicio, pag_fim))
            print(f"  Seção PMAL no índice: páginas {pag_inicio} a {pag_fim - 1}")
        else:
            print("  AVISO: Seção PMAL não encontrada no índice.")

        # --- Extrair texto de TODAS as páginas ---
        all_pages = []  # [(num_pagina, texto)]
        for i, page in enumerate(pdf.pages, 1):
            all_pages.append((i, extract_page_text(page)))

    # --- Montar texto completo com rastreio de posição por página ---
    full_text = ""
    page_offsets = []
    for num, txt in all_pages:
        page_offsets.append((num, len(full_text)))
        full_text += txt + "\n"

    def pos_to_page(pos):
        pagina = page_offsets[0][0]
        for p, off in page_offsets:
            if pos >= off:
                pagina = p
            else:
                break
        return pagina

    # --- Dividir TODO o diário em blocos por "Protocolo N" ---
    proto_positions = [m.end() for m in re.finditer(r"Protocolo\s+\d+", full_text)]

    materias_pmal = []
    materias_mencoes = []
    cursor = 0

    for proto_end in proto_positions:
        body = full_text[cursor:proto_end].strip()
        cursor = proto_end
        if len(body) < 30:
            continue

        pagina_ini = pos_to_page(proto_end - len(body))
        pagina_fim = pos_to_page(proto_end - 1)

        # Verificar se está na faixa de páginas da seção PMAL
        if pmal_range and pagina_ini in pmal_range:
            materias_pmal.append(make_materia(body, pagina_ini, pagina_fim, "PMAL"))

        # Se NÃO está na seção PMAL e NÃO está no índice (pags 1-5),
        # verifica se menciona a PMAL
        elif pagina_ini > 5 and pagina_ini not in pmal_range:
            if PMAL_TERMS_RE.search(body) and not BM_EXCLUDE_RE.search(body):
                # Patentes por extenso só contam se o bloco também tiver "PM AL" ou "PM" no contexto
                rank_only = re.search(
                    r"PRIMEIRO SARGENTO|SEGUNDO SARGENTO|TERCEIRO SARGENTO"
                    r"|SUB.?TENENTE|CAPIT.O|MAJOR|TENENTE CORONEL|CORONEL"
                    r"|DI.RIAS.*MILITAR", body, re.I)
                pm_context = re.search(r"\bPM\s*AL\b|\bPMAL\b|\bPM/AL\b|\d+\s*PM\s*AL", body, re.I)
                # Se achou só patente por extenso, exige que também tenha "PM AL" no bloco
                if rank_only and not pm_context:
                    has_pm_abbrev = re.search(
                        r"\bPM\b(?!\s*(?:AL|/|-))", body)
                    if not has_pm_abbrev:
                        pass  # Ignora: patente genérica sem vínculo com PM
                    else:
                        materias_mencoes.append(
                            make_materia(body, pagina_ini, pagina_fim, "Menção"))
                else:
                    materias_mencoes.append(
                        make_materia(body, pagina_ini, pagina_fim, "Menção"))

    print(f"  Matérias na seção PMAL: {len(materias_pmal)}")
    print(f"  Menções à PMAL em outras seções: {len(materias_mencoes)}")

    return materias_pmal, materias_mencoes


def try_download(edition_id, dest):
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


CSS = """
:root{
  --tinta:#10151c; --papel:#f7f5f0; --carta:#ffffff;
  --farda:#1e3a5f; --farda-escuro:#14283f; --dourado:#b8860b;
  --cinza:#5c6672; --linha:#e3ded4;
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
.badge-m{display:inline-block;background:var(--dourado);color:#fff;
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
details.mencao{border-left:4px solid var(--dourado)}
.busca{width:100%;padding:12px 16px;font:inherit;border:1px solid var(--linha);
  border-radius:6px;margin:0 0 18px;background:var(--carta)}
h3.secao{font-family:'Trebuchet MS',Verdana,sans-serif;color:var(--farda);
  text-transform:uppercase;font-size:.9rem;letter-spacing:1.5px;
  border-bottom:2px solid var(--dourado);padding-bottom:6px;margin:34px 0 16px}
.aviso{background:#fdf6e3;border:1px solid #e8d9a0;border-radius:6px;
  padding:12px 16px;font-size:.88rem;color:#6b5900;margin:0 0 18px}
.pag{background:var(--dourado);color:#fff;border-radius:4px;
  padding:1px 8px;font-size:.75rem;
  font-family:'Trebuchet MS',Verdana,sans-serif;margin-left:6px}
.voltar{font-family:'Trebuchet MS',Verdana,sans-serif;font-size:.85rem}
.voltar a{color:var(--farda)}
footer{max-width:900px;margin:0 auto;padding:20px;color:var(--cinza);
  font-size:.8rem;border-top:1px solid var(--linha)}
"""


def html_page(title, body, subtitle=""):
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
<p>{subtitle or "Matérias da Polícia Militar de Alagoas no Diário Oficial do Estado"}</p>
</div></header>
<main>
{body}
</main>
<footer>Compilação automática de caráter informativo. O documento oficial é a edição
certificada digitalmente publicada pela Imprensa Oficial Graciliano Ramos em
<a href="https://diario.imprensaoficial.al.gov.br/">diario.imprensaoficial.al.gov.br</a>.</footer>
</body></html>"""


def esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def pag_label(mat):
    if mat["pagina_ini"] == mat["pagina_fim"]:
        return f'pag. {mat["pagina_ini"]}'
    return f'pags. {mat["pagina_ini"]}-{mat["pagina_fim"]}'


def build_edition_page(ed):
    body = [
        f'<p class="voltar"><a href="../index.html">&larr; Todas as edições</a> &nbsp;&middot;&nbsp; '
        f'<a href="{ed["numero"]}.pdf">Baixar compilado PMAL (PDF)</a> &nbsp;&middot;&nbsp; '
        f'<a href="{API_PDF.format(ed["id"])}" target="_blank">Edição completa (PDF oficial)</a></p>',
        f'<h2>Edição nº {ed["numero"]} — {ed["data_texto"]}'
        f'<span class="badge">{ed.get("qtd_secao", 0)} na seção</span>'
        f'<span class="badge-m">{ed.get("qtd_mencoes", 0)} menções</span></h2>',
    ]
    if not ed["materias"] and not ed["mencoes"]:
        body.append('<div class="aviso">Nenhuma matéria da PMAL foi localizada nesta edição.</div>')
    else:
        body.append('<input class="busca" id="busca" type="search" '
                    'placeholder="Filtrar por nome, posto, matrícula...">')

    if ed["materias"]:
        body.append('<h3 class="secao">Seção Oficial da PMAL</h3>')
        for mat in ed["materias"]:
            proto = f' &middot; Protocolo {mat["protocolo"]}' if mat["protocolo"] else ""
            body.append(
                f'<details class="mat"><summary>{esc(mat["titulo"])}'
                f'<span class="pag">{pag_label(mat)}</span>'
                f'<span class="meta">{proto}</span></summary>'
                f'<div class="corpo">{esc(mat["texto"])}</div></details>')

    if ed["mencoes"]:
        body.append('<h3 class="secao">Menções à PMAL em outras seções</h3>')
        for mat in ed["mencoes"]:
            proto = f' &middot; Protocolo {mat["protocolo"]}' if mat["protocolo"] else ""
            body.append(
                f'<details class="mat mencao"><summary>{esc(mat["titulo"])}'
                f'<span class="pag">{pag_label(mat)}</span>'
                f'<span class="meta">{proto}</span></summary>'
                f'<div class="corpo">{esc(mat["texto"])}</div></details>')

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


def build_index(editions):
    body = []
    if not editions:
        body.append('<div class="aviso">Nenhuma edição processada ainda.</div>')
    else:
        body.append('<h3 class="secao">Edições compiladas</h3>')
        for ed in sorted(editions, key=lambda e: (e.get("data") or "", e["numero"]), reverse=True):
            body.append(
                f'<a class="card-link" href="edicoes/{ed["numero"]}.html"><div class="card">'
                f'<h2>Edição nº {ed["numero"]}</h2>'
                f'<div class="meta">{ed["data_texto"]} &middot; '
                f'{ed.get("qtd_secao", 0)} na seção PMAL &middot; '
                f'{ed.get("qtd_mencoes", 0)} menções em outras seções</div>'
                f'</div></a>')
    return html_page("DOE-AL — Clipping PMAL", "\n".join(body))


def gerar_pdf(ed, dest):
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import cm
    from reportlab.lib import colors
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, HRFlowable

    def p(texto):
        return (texto.replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;").replace("\n", "<br/>"))

    styles = getSampleStyleSheet()
    st_titulo = ParagraphStyle("titulo", parent=styles["Title"],
                               fontName="Helvetica-Bold", fontSize=15,
                               textColor=colors.HexColor("#14283f"), spaceAfter=2)
    st_sub = ParagraphStyle("sub", parent=styles["Normal"], fontSize=10,
                            textColor=colors.HexColor("#5c6672"), spaceAfter=12)
    st_secao = ParagraphStyle("secao", parent=styles["Heading2"],
                              fontName="Helvetica-Bold", fontSize=11,
                              textColor=colors.HexColor("#1e3a5f"),
                              spaceBefore=14, spaceAfter=6)
    st_mat = ParagraphStyle("mat", parent=styles["Normal"],
                            fontName="Helvetica-Bold", fontSize=12,
                            textColor=colors.HexColor("#14283f"),
                            spaceBefore=12, spaceAfter=4)
    st_pag = ParagraphStyle("pag", parent=styles["Normal"],
                            fontName="Helvetica-Oblique", fontSize=10,
                            textColor=colors.HexColor("#b8860b"), spaceAfter=6)
    st_corpo = ParagraphStyle("corpo", parent=styles["Normal"],
                              fontName="Helvetica", fontSize=12,
                              leading=16, spaceAfter=8,
                              alignment=4)  # 4 = TA_JUSTIFY

    doc = SimpleDocTemplate(str(dest), pagesize=A4,
                            leftMargin=1.8*cm, rightMargin=1.8*cm,
                            topMargin=1.6*cm, bottomMargin=1.6*cm,
                            title=f"DOE-AL {ed['numero']} - Clipping PMAL")
    story = [
        Paragraph("DOE-AL - Clipping PMAL", st_titulo),
        Paragraph(f"Edição nº {ed['numero']} — {ed['data_texto']} - "
                  f"{ed.get('qtd_secao', 0)} na seção PMAL, "
                  f"{ed.get('qtd_mencoes', 0)} menções em outras seções", st_sub),
        HRFlowable(width="100%", thickness=1.2, color=colors.HexColor("#b8860b")),
    ]

    if ed["materias"]:
        story.append(Paragraph("SEÇÃO OFICIAL DA PMAL", st_secao))
        for i, mat in enumerate(ed["materias"], 1):
            proto = f" - Protocolo {mat['protocolo']}" if mat["protocolo"] else ""
            story.append(Paragraph(f"{i}. {p(mat['titulo'])}{proto}", st_mat))
            story.append(Paragraph(f"({pag_label(mat)} do diário oficial)", st_pag))
            story.append(Paragraph(p(mat["texto"]), st_corpo))

    if ed["mencoes"]:
        story.append(Paragraph("MENÇÕES À PMAL EM OUTRAS SEÇÕES", st_secao))
        for i, mat in enumerate(ed["mencoes"], 1):
            proto = f" - Protocolo {mat['protocolo']}" if mat["protocolo"] else ""
            story.append(Paragraph(f"{i}. {p(mat['titulo'])}{proto}", st_mat))
            story.append(Paragraph(f"({pag_label(mat)} do diário oficial)", st_pag))
            story.append(Paragraph(p(mat["texto"]), st_corpo))

    if not ed["materias"] and not ed["mencoes"]:
        story.append(Paragraph("Nenhuma matéria da PMAL nesta edição.", st_corpo))

    story.append(Spacer(1, 12))
    story.append(HRFlowable(width="100%", thickness=0.6, color=colors.HexColor("#e3ded4")))
    story.append(Paragraph(
        "Compilação automática de caráter informativo. O documento oficial é a "
        "edição certificada digitalmente publicada pela Imprensa Oficial Graciliano Ramos.", st_sub))
    doc.build(story)


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"last_id": 51739, "processed": []}


def load_editions():
    if DATA_FILE.exists():
        return json.loads(DATA_FILE.read_text(encoding="utf-8"))
    return []


def process_pdf(edition_id, pdf_path):
    meta = parse_metadata(first_page_text(pdf_path))
    materias_pmal, materias_mencoes = extract_all_pmal(pdf_path)
    return {
        "id": edition_id,
        "numero": meta["numero"] or str(edition_id),
        "data": meta["data"],
        "data_texto": meta["data_texto"] or "data não identificada",
        "materias": materias_pmal,
        "mencoes": materias_mencoes,
        "qtd_secao": len(materias_pmal),
        "qtd_mencoes": len(materias_mencoes),
        "processado_em": datetime.now(TZ_MACEIO).isoformat(),
    }


def main():
    state = load_state()
    editions_index = load_editions()
    processed_ids = set(state.get("processed", []))
    EDICOES_DIR.mkdir(parents=True, exist_ok=True)
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)

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

        total = ed["qtd_secao"] + ed["qtd_mencoes"]
        print(f"  -> Edição nº {ed['numero']} ({ed['data_texto']}): "
              f"{ed['qtd_secao']} na seção + {ed['qtd_mencoes']} menções = {total} total")

        (EDICOES_DIR / f'{ed["numero"]}.html').write_text(
            build_edition_page(ed), encoding="utf-8")
        gerar_pdf(ed, EDICOES_DIR / f'{ed["numero"]}.pdf')

        with open(BASE / "novas.txt", "a", encoding="utf-8") as f:
            f.write(f"docs/edicoes/{ed['numero']}.pdf\n")
        with open(BASE / "resumo.txt", "a", encoding="utf-8") as f:
            f.write(f"Edição nº {ed['numero']} — {ed['data_texto']}: "
                    f"{ed['qtd_secao']} na seção PMAL + "
                    f"{ed['qtd_mencoes']} menções.\n")

        summary = {k: ed[k] for k in ("id", "numero", "data", "data_texto",
                                       "qtd_secao", "qtd_mencoes")}
        editions_index = [e for e in editions_index if e["numero"] != ed["numero"]]
        editions_index.append(summary)
        processed_ids.add(candidate)
        state["last_id"] = max(state["last_id"], candidate)
        found_any = True

    state["processed"] = sorted(processed_ids)[-200:]
    STATE_FILE.write_text(json.dumps(state, indent=1), encoding="utf-8")
    DATA_FILE.write_text(json.dumps(editions_index, ensure_ascii=False, indent=1), encoding="utf-8")
    (DOCS / "index.html").write_text(build_index(editions_index), encoding="utf-8")

    if found_any:
        print("Concluído: novas edições compiladas.")
    else:
        print("Nenhuma edição nova encontrada nesta execução.")


if __name__ == "__main__":
    main()
