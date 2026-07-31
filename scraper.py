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

PMAL_HEADING_RE = re.compile(
    r"Pol[ií]cia Militar do Estado de Alagoas\s*\(PMAL\)", re.I
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
    """Devolve lista [(numero_pagina, texto_pagina)] respeitando as 2 colunas."""
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
    """Percorre página a página, recorta só a seção da PMAL e devolve
    matérias com página inicial e final."""
    full_text = ""
    page_offsets = []  # [(pagina, pos_inicio_no_texto)]
    for num, txt in pages:
        page_offsets.append((num, len(full_text)))
        full_text += txt + "\n"

    # Localiza início e fim da seção PMAL no texto concatenado
    matches = list(PMAL_HEADING_RE.finditer(full_text))
    if not matches:
        return []
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

    # Divide a seção em matérias pelo delimitador "Protocolo N"
    materias = []
    for m in re.finditer(r"Protocolo\s+\d+", section):
        proto_end_abs = start + m.end()
        materias.append(proto_end_abs)

    materias_out = []
    cursor = start
    for proto_end_abs in materias:
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


def html_page(title: str, body: str,
