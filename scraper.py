# -*- coding: utf-8 -*-
"""
Robô DOE-AL → PMAL
Baixa novas edições do Diário Oficial do Estado de Alagoas,
extrai a seção da Polícia Militar de Alagoas (PMAL) com
indicação da página inicial e final de cada matéria.
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

END_SECTION_MARKERS = [
    "ADMINISTRAÇÃO INDIRETA",
    "Eventos Funcionais",
    "Prefeituras do Interior",
    "PARTICULARES",
]

# Cabeçalho de seção: precisa estar praticamente isolado numa linha
# (sem outros textos grudados antes/depois na mesma linha), pra evitar
# capturar o índice, referências dentro de despachos, etc.
PMAL_HEADING_RE = re.compile(
    r"^[\s.]*Pol[ií]cia Militar do Estado de Alagoas\s*\(PMAL\)[\s.]*$",
    re.I | re.M
)

MESES = {
    "janeiro": 1, "fevereiro": 2, "março": 3, "marco": 3, "abril": 4,
    "maio": 5, "junho": 6, "julho": 7, "agosto": 8, "setembro": 9,
    "outubro": 10, "novembro": 11, "dezembro": 12,
}

FIXES = {
    "Oicial": "Oficial", "oicial": "oficial",
    "Certiicad": "Certificad", "certiicad": "certificad",
    "especíic": "específic", "Especíic": "Específic",
    "veriicaç": "verificaç", "Veriicaç": "Verificaç",
    "iscal": "fiscal", "Ediicaç": "Edificaç",
    "proission": "profission", "Proission": "Profission",
    "justiicativ": "justificativ", "ratiicad": "ratificad",
    "Ratiicad": "Ratificad", "retiicaç": "retificaç",
    "inanceir": "financeir",
    "eiciência": "eficiência", "eicácia": "eficácia",
    "gratiicaç": "gratificaç", "Gratiicaç": "Gratificaç",
    "notiicaç": "notificaç", "Notiicaç": "Notificaç",
    "classiicaç": "classificaç", "Classiicaç": "Classificaç",
    "qualiicaç": "qualificaç", "Qualiicaç": "Qualificaç",
    "identiicaç": "identificaç", "Identiicaç": "Identificaç",
    "ica ": "fica ", " im ": " fim ",
}


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    for wrong, right in FIXES.items():
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


def extract_pages(pdf_path: Path) -> list:
    pages = []
    with pdfplumber.open(pdf_path) as pdf:
        for i, page in enumerate(pdf.pages, 1):
            w, h = page.width, page.height
            mid = w / 2
            try:
                left = page.crop((0, 0, mid, h)).extract_text() or ""
                right = page.crop((mid, 0, w, h)).extract_text() or ""
                page_text = left + "\n" + right
            except Exception:
                page_text = page.extract_text() or ""
            pages.append((i, normalize(strip_headers(page_text))))
    return pages


def parse_metadata(text: str) -> dict:
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


def extract_pmal_materias(pages: list) -> list:
    full_text = ""
    page_offsets = []
    for num, txt in pages:
        page_offsets.append((num, len(full_text)))
        full_text += txt + "\n"

    matches = list(PMAL_HEADING_RE.finditer(full_text))
    if not matches:
        return []
    # Se houver mais de uma ocorrência (raro, mas possível),
    # usa a que estiver mais próxima do fim do diário.
    start = matches[-1].end()
    end = len(full_text)
    for marker in END_SECTION_MARKERS:
        pattern = re.compile(r"^\s*" + re.escape(marker) + r"\s*$", re.M)
        m = pattern.search(full_text, start)
        if m and m.start() < end:
            end = m.start()

    section = full_text[start:end]

    def pos_to_page(abs_pos: int) -> int:
        pagina = page_offsets[0][0]
        for p, off in page_offsets:
            if abs_pos >= off:
                pagina = p
            else:
                break
        return pagina

    materias_ends = []
    for m in re.finditer(r"Protocolo\s+\d+", section):
        materias_ends.append(start + m.end())

    materias_out = []
    cursor = start
    for proto_end_abs in materias_ends:
        body = full_text[cursor:proto_end_abs].strip()
        if len(body) > 30:
            pagina_ini = pos_to_page(cursor)
            pagina_fim = pos_to_page(proto_end_abs - 1)
            lines = [l.strip() for l in body.split("\n") if l.strip()]
            titulo = lines[0] if lines else "Matéria"
            if len(titulo) > 120:
                titulo = titulo[:117] + "..."
            proto = None
            pm = re.search(r"Protocolo\s+(\d+)", body)
            if pm:
                proto = pm.group(1)
            materias_out.append({
                "titulo": titulo,
                "protocolo": proto,
                "texto": body,
                "pagina_ini": pagina_ini,
                "pagina_fim": pagina_fim,
            })
        cursor = proto_end_abs
    return materias_out


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
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def pag_label(mat):
    if mat["pagina_ini"] == mat["pagina_fim"]:
        return f'pág. {mat["pagina_ini"]}'
    return f'págs. {mat["pagina_ini"]}–{mat["pagina_fim"]}'


def build_edition_page(ed):
    body = [f'<p class="voltar"><a href="../index.html">&larr; Todas as edições</a> &nbsp;&middot;&nbsp; '
            f'<a href="{ed["numero"]}.pdf">Baixar compilado PMAL (PDF)</a> &nbsp;&middot;&nbsp; '
            f'<a href="{API_PDF.format(ed["id"])}" target="_blank">Edição completa (PDF oficial)</a></p>']
    body.append(f'<h2>Edição nº {ed["numero"]} — {ed["data_texto"]}'
                f'<span class="badge">{len(ed["materias"])} matéria(s) PMAL</span></h2>')

    if not ed["materias"]:
        body.append('<div class="aviso">Nenhuma matéria da PMAL foi localizada nesta edição.</div>')
    else:
        body.append('<input class="busca" id="busca" type="search" '
                    'placeholder="Filtrar por nome, posto, matrícula...">')
        body.append('<h3 class="secao">Seção Polícia Militar do Estado de Alagoas (PMAL)</h3>')
        for mat in ed["materias"]:
            proto = f' &middot; Protocolo {mat["protocolo"]}' if mat["protocolo"] else ""
            body.append(
                f'<details class="mat"><summary>{esc(mat["titulo"])}'
                f'<span class="pag">{pag_label(mat)}</span>'
                f'<span class="meta">{proto}</span></summary>'
                f'<div class="corpo">{esc(mat["texto"])}</div></details>'
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


def build_index(editions):
    body = []
    if not editions:
        body.append('<div class="aviso">Nenhuma edição processada ainda.</div>')
    else:
        body.append('<h3 class="secao">Edições compiladas</h3>')
        for ed in sorted(editions, key=lambda e: (e.get("data") or "", e["numero"]), reverse=True):
            n_mat = ed.get("qtd_materias", 0)
            body.append(
                f'<a class="card-link" href="edicoes/{ed["numero"]}.html"><div class="card">'
                f'<h2>Edição nº {ed["numero"]}</h2>'
                f'<div class="meta">{ed["data_texto"]} &middot; {n_mat} matéria(s) na seção PMAL</div>'
                f'</div></a>'
            )
    return html_page("DOE-AL — Clipping PMAL", "\n".join(body))


def gerar_pdf(ed, dest):
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import cm
    from reportlab.lib import colors
    from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer,
                                    HRFlowable)

    def p(texto):
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
    st_pag = ParagraphStyle("pag", parent=styles["Normal"],
                            fontName="Helvetica-Oblique", fontSize=8,
                            textColor=colors.HexColor("#b8860b"),
                            spaceAfter=4)
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
                  f"{len(ed['materias'])} matéria(s) na seção PMAL", st_sub),
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
            story.append(Paragraph(f"({pag_label(mat)} do diário oficial)",
                                   st_pag))
            story.append(Paragraph(p(mat["texto"]), st_corpo))
    else:
        story.append(Paragraph(
            "Nenhuma matéria localizada na seção da PMAL nesta edição.",
            st_corpo))

    story.append(Spacer(1, 12))
    story.append(HRFlowable(width="100%", thickness=0.6,
                            color=colors.HexColor("#e3ded4")))
    story.append(Paragraph(
        "Compilação automática de caráter informativo. O documento oficial é a "
        "edição certificada digitalmente publicada pela Imprensa Oficial "
        "Graciliano Ramos.", st_sub))
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
    pages = extract_pages(pdf_path)
    meta = parse_metadata(first_page_text(pdf_path))
    materias = extract_pmal_materias(pages)
    hoje = datetime.now(TZ_MACEIO)
    return {
        "id": edition_id,
        "numero": meta["numero"] or str(edition_id),
        "data": meta["data"],
        "data_texto": meta["data_texto"] or "data não identificada",
        "materias": materias,
        "processado_em": hoje.isoformat(),
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

        print(f"  → Edição nº {ed['numero']} ({ed['data_texto']}): "
              f"{len(ed['materias'])} matérias PMAL")

        (EDICOES_DIR / f'{ed["numero"]}.html').write_text(
            build_edition_page(ed), encoding="utf-8")
        gerar_pdf(ed, EDICOES_DIR / f'{ed["numero"]}.pdf')

        with open(BASE / "novas.txt", "a", encoding="utf-8") as f:
            f.write(f"docs/edicoes/{ed['numero']}.pdf\n")
        with open(BASE / "resumo.txt", "a", encoding="utf-8") as f:
            f.write(f"Edição nº {ed['numero']} — {ed['data_texto']}: "
                    f"{len(ed['materias'])} matéria(s) na seção PMAL.\n")

        summary = {k: ed[k] for k in ("id", "numero", "data", "data_texto")}
        summary["qtd_materias"] = len(ed["materias"])
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
