"""
Coleta de concursos do portal Concursos no Brasil (concursosnobrasil.com).

Estratégia:
1. Web Scraping: tabela em /concursos/ com órgão, vagas e link para detalhe.
2. Web Scraping (detalhe): extrai H1 (título), data de inscrição, link oficial
     do organizador (access.selecao.net.br, quadrix.org.br, cesgranrio.org.br etc.)
     e remuneração.
3. PDF Scraping: se o link externo apontar diretamente para um PDF, baixa e analisa.

robots.txt: apenas /wp-admin/ bloqueado — listagem e posts liberados.
"""
import re
from time import sleep
from urllib.error import HTTPError
from urllib.parse import urljoin
from urllib.robotparser import RobotFileParser

from lxml import etree, html

from extracao import normalizar_nivel, analisar_edital_pdf
from fontes.http import AGENTE, LIMITE_PDF, baixar

BASE = "https://concursosnobrasil.com"
LISTA_URL = f"{BASE}/concursos/"

_RE_DATA = re.compile(r"\b(\d{2}/\d{2}/\d{4})\b")
_RE_DATA_CURTA = re.compile(r"\b(\d{1,2}\s+de\s+\w+\s+de\s+\d{4})\b", re.I)
_RE_VAGAS = re.compile(r"([\d.,]+)\s*vagas?", re.I)
# Captura tanto "R$ 5.482,21" quanto abreviações jornalísticas como "R$ 5,5 mil" ou "R$ 8 mil"
_RE_SAL = re.compile(r"R\$\s*(\d{1,3}(?:\.\d{3})*(?:,\d{1,2})?|\d+(?:,\d{1,2})?)\s*(mil)?", re.I)

# UF no slug da URL (/concursos/<uf>/ ou /concurso-<orgao>/)
_UF_SLUG = {
    "ac", "al", "ap", "am", "ba", "ce", "df", "es", "go",
    "ma", "mt", "ms", "mg", "pa", "pb", "pr", "pe", "pi",
    "rj", "rn", "rs", "ro", "rr", "sc", "sp", "se", "to",
}


def _limpar(texto):
    return " ".join(str(texto or "").split())


def _documento(conteudo):
    try:
        return html.fromstring(conteudo.decode("utf-8", "replace"))
    except (etree.ParserError, ValueError) as exc:
        raise ValueError("Concursos no Brasil não retornou HTML legível.") from exc


def _uf_de_url(url):
    # /concursos/<uf>/... ou /concursos/<uf>-<cidade>/...
    m = re.search(r"/concursos/([a-z]{2})(?:/|-)", url)
    if m and m.group(1) in _UF_SLUG:
        return m.group(1).upper()
    return "BR"


def _extrair_maior_remuneracao(texto):
    """Identifica o maior salário citado no texto (evitando confundir com taxas de inscrição)."""
    melhor_valor = 0.0
    melhor_texto = ""
    for m in _RE_SAL.finditer(texto):
        num_str = m.group(1).replace(".", "").replace(",", ".")
        eh_mil = bool(m.group(2))
        try:
            valor = float(num_str) * (1000.0 if eh_mil else 1.0)
        except ValueError:
            continue
        # Ignora valores muito baixos típicos de taxa de inscrição (ex.: R$ 50,00) quando houver salário
        if valor > melhor_valor:
            melhor_valor = valor
            sufixo = " mil" if eh_mil else ""
            melhor_texto = f"até R$ {_limpar(m.group(1))}{sufixo}"
    return melhor_texto if melhor_valor >= 300.0 else ""


def _parse_listagem(doc, limite=50):
    rascunhos = []
    vistos = set()
    for tr in doc.xpath("//table//tr"):
        a_nodes = tr.xpath(".//td[1]//a[@href]")
        if not a_nodes:
            continue
        a = a_nodes[0]
        href = a.get("href", "")
        url = urljoin(BASE, href) if href.startswith("/") else href
        if url in vistos or "concursosnobrasil" not in url:
            continue
        vistos.add(url)
        orgao = _limpar(a.text_content())
        vagas_cells = tr.xpath(".//td[2]")
        vagas_txt = _limpar(vagas_cells[0].text_content()) if vagas_cells else ""
        # Descarta texto sem dígito (ex: ", vagas", "Várias vagas" sem número)
        if not re.search(r"\d", vagas_txt):
            vagas_txt = ""
        uf = _uf_de_url(url)
        rascunhos.append({"_url": url, "_orgao": orgao, "_vagas": vagas_txt, "_uf": uf})
        if len(rascunhos) >= limite:
            break
    return rascunhos


def _parse_detalhe(conteudo, rascunho):
    doc = _documento(conteudo)

    h1 = doc.xpath("//h1")
    titulo = _limpar(h1[0].text_content()) if h1 else rascunho["_orgao"]

    # Texto do artigo
    art_nodes = doc.xpath("//article")
    texto_art = _limpar(art_nodes[0].text_content())[:4000] if art_nodes else ""

    # Links externos (site oficial/organizador)
    ext_links = []
    for a in doc.xpath("//article//a[@href] | //p//a[@href]"):
        href = a.get("href", "")
        txt = _limpar(a.text_content()).lower()
        if href.startswith("http") and "concursosnobrasil" not in href and "whatsapp" not in href:
            if any(k in txt for k in ("site", "página", "edital", "inscri", "clique", "acesse")):
                ext_links.append(href)
    ext_url = ext_links[0] if ext_links else ""

    # PDF direto no link externo?
    edital_pdf_url = ""
    if ext_url and ext_url.lower().endswith(".pdf"):
        edital_pdf_url = ext_url

    # Data de prazo no texto
    prazo = ""
    for m in _RE_DATA.finditer(texto_art):
        prazo = m.group(1)
        break

    # Vagas
    vagas = rascunho["_vagas"]
    m_vagas = _RE_VAGAS.search(texto_art)
    if m_vagas and (not vagas or vagas in ("Várias", "Vários", "")):
        vagas = f"{_limpar(m_vagas.group(1))} vagas"

    # Remuneração (busca tanto no título quanto no corpo do artigo e pega o maior valor)
    remuneracao = _extrair_maior_remuneracao(f"{titulo} {texto_art}")

    # Nível
    nivel = normalizar_nivel(texto_art) or "Vários"

    return {
        "titulo": titulo[:220],
        "orgao": rascunho["_orgao"][:180],
        "uf": rascunho["_uf"],
        "nivel": nivel,
        "prazo": prazo,
        "url": ext_url or rascunho["_url"],
        "vagas": vagas[:60],
        "remuneracao": remuneracao[:60],
        "jornada": "",
        "localizacao": rascunho["_uf"],
        "requisitos": "",
        "edital_pdf_url": edital_pdf_url,
        "_pdf_url": edital_pdf_url,
        "_post_url": rascunho["_url"],
    }


def _enriquecer_pdf(registro, fetch_fn):
    pdf_url = registro.get("_pdf_url", "")
    if not pdf_url:
        return
    try:
        pdf_bytes = fetch_fn(pdf_url, "application/pdf", limite=LIMITE_PDF)
        analise = analisar_edital_pdf(pdf_bytes)
        for campo in ("vagas", "remuneracao", "jornada", "localizacao", "requisitos"):
            if not registro.get(campo) and analise.get(campo):
                registro[campo] = analise[campo]
        if registro.get("nivel") == "Vários" and analise.get("nivel") and analise["nivel"] != "Vários":
            registro["nivel"] = analise["nivel"]
        registro["pdf_processado"] = True
    except (OSError, ValueError):
        pass


def coletar_cnb(limite=30, enriquecer_pdf=5):
    """Coleta editais do Concursos no Brasil com web + PDF scraping."""
    regras = RobotFileParser(BASE + "/robots.txt")
    try:
        regras.parse(baixar(regras.url, "text/plain").decode("utf-8", errors="replace").splitlines())
    except HTTPError as exc:
        if exc.code != 404:
            raise
        regras.parse([])
    atraso = max(1, regras.crawl_delay(AGENTE) or 0, regras.crawl_delay("*") or 0)

    def pagina(url, accept="text/html", limite_bytes=None):
        if not regras.can_fetch(AGENTE, url):
            raise ValueError("Consulta vedada pelo robots.txt: " + url)
        sleep(atraso)
        if limite_bytes is None:
            return baixar(url, accept)
        return baixar(url, accept, limite=limite_bytes)

    conteudo = pagina(LISTA_URL)
    doc = _documento(conteudo)
    rascunhos = _parse_listagem(doc, limite=limite)
    if not rascunhos:
        raise ValueError("Nenhum concurso reconhecido na listagem do Concursos no Brasil.")

    registros, pendencias = [], []
    for i, rascunho in enumerate(rascunhos):
        url = rascunho["_url"]
        try:
            registro = _parse_detalhe(pagina(url), rascunho)
            if not registro.get("prazo"):
                pendencias.append(f"{url}: prazo de inscrições não encontrado")
                continue
            if i < enriquecer_pdf:
                _enriquecer_pdf(registro, lambda u, a, **kw: pagina(u, a, limite=kw.get("limite")))
            for k in list(registro):
                if k.startswith("_"):
                    del registro[k]
            registros.append(registro)
        except (OSError, ValueError) as exc:
            pendencias.append(f"{url}: {exc}")

    return registros, pendencias