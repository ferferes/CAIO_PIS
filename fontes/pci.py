"""Coleta pública do portal PCI Concursos respeitando o robots.txt."""
import re
from time import sleep
from urllib.error import HTTPError
from urllib.robotparser import RobotFileParser

from lxml import etree, html

from extracao import normalizar_nivel
from fontes.http import AGENTE, baixar
from opcoes import UFS

BASE = "https://www.pciconcursos.com.br"
LISTA_URL = f"{BASE}/concursos/"

_RE_VAGAS = re.compile(r"(\d[\d.]*\s*vagas?|cadastro\s+de\s+reserva)", re.I)
_RE_REMUNERACAO = re.compile(r"(?:at[ée]\s+)?R\$\s*\d{1,3}(?:\.\d{3})*,\d{2}", re.I)
_RE_DATA = re.compile(r"\b(\d{2}/\d{2}/\d{4})\b")


def _limpar(texto):
    return " ".join(str(texto or "").split())


def _parse_cards(conteudo, limite=30, uf_filtro=None):
    try:
        doc = html.fromstring(conteudo)
    except (etree.ParserError, ValueError) as exc:
        raise ValueError("O portal PCI Concursos não retornou HTML legível.") from exc

    registros, pendencias = [], []
    vistos = set()

    for bloco in doc.xpath('//div[contains(concat(" ",normalize-space(@class)," ")," da ")]'):
        ancoras = bloco.xpath('.//div[contains(concat(" ",normalize-space(@class)," ")," ca ")]/a[@href]')
        if not ancoras:
            continue
        a = ancoras[0]
        url = _limpar(a.get("href") or bloco.get("data-url") or "")
        orgao = _limpar(a.text_content())
        titulo_attr = _limpar(a.get("title") or "")
        if not url or not orgao:
            continue

        cc = bloco.xpath('.//div[contains(concat(" ",normalize-space(@class)," ")," cc ")]')
        uf_raw = _limpar(cc[0].text_content()).upper() if cc else ""
        uf = uf_raw if uf_raw in UFS else "BR"
        if uf_filtro and uf != uf_filtro.upper():
            continue

        ce = bloco.xpath('.//div[contains(concat(" ",normalize-space(@class)," ")," ce ")]')
        prazo_match = _RE_DATA.search(_limpar(ce[0].text_content())) if ce else None
        if not prazo_match:
            continue
        prazo = prazo_match[1]

        cd_nodes = bloco.xpath('.//div[contains(concat(" ",normalize-space(@class)," ")," cd ")]')
        cd_el = cd_nodes[0] if cd_nodes else None
        cd_topo = _limpar(cd_el.text) if cd_el is not None else ""
        spans = cd_el.xpath(".//span") if cd_el is not None else []
        cargo_txt = _limpar(spans[0].text) if spans else ""
        nivel_txt = _limpar(spans[1].text_content()) if len(spans) > 1 else ""

        vagas_match = _RE_VAGAS.search(cd_topo)
        vagas = _limpar(vagas_match[1]) if vagas_match else ""
        rem_match = _RE_REMUNERACAO.search(cd_topo)
        remuneracao = _limpar(rem_match[0]) if rem_match else ""
        nivel = normalizar_nivel(nivel_txt) or "Vários"

        if titulo_attr:
            titulo = titulo_attr
        elif cargo_txt and cargo_txt.casefold() != "vários cargos":
            titulo = f"{cargo_txt} — {orgao}"
        else:
            titulo = f"Concurso Público — {orgao}"
        titulo = titulo[:220]

        chave_dedup = (titulo.casefold(), orgao.casefold(), uf, prazo)
        if chave_dedup in vistos:
            continue
        vistos.add(chave_dedup)

        requisitos = ""
        if cargo_txt and cargo_txt.casefold() != "vários cargos":
            requisitos = f"Cargo principal: {cargo_txt}"
            if nivel_txt:
                requisitos += f" ({nivel_txt})"
        elif nivel_txt:
            requisitos = f"Escolaridade informada: {nivel_txt}"

        registros.append({
            "titulo": titulo,
            "orgao": orgao[:180],
            "uf": uf,
            "nivel": nivel,
            "prazo": prazo,
            "url": url,
            "vagas": vagas,
            "remuneracao": remuneracao,
            "localizacao": "Nacional" if uf == "BR" else uf,
            "requisitos": requisitos[:300],
        })
        if len(registros) >= limite:
            break

    return registros, pendencias


def _enriquecer_noticia(registro, fetch_fn):
    """Lê a página HTML da notícia (permitida no robots.txt) para complementar requisitos."""
    try:
        raw = fetch_fn(registro["url"], "text/html")
        doc = html.fromstring(raw)
    except (OSError, ValueError, etree.ParserError):
        return
    corpo = doc.xpath('//div[@itemprop="articleBody"]')
    if not corpo:
        return
    texto = _limpar(corpo[0].text_content())
    m_jornada = re.search(r"(\b\d{2}\s*(?:a\s*\d{2}\s*)?horas?\s*semanais\b|jornada\s+de\s+\d{2}\s*h)", texto, re.I)
    if m_jornada and not registro.get("jornada"):
        registro["jornada"] = _limpar(m_jornada[1])[:100]
    m_cargos = re.search(
        r"oportunidades\s+s[ãa]o\s+para\s+os?\s+cargos?\s+de\s*:\s*(.{15,260}?)(?=(?<!\d)\.(?:\s|$)|\s+(?:A\s+remunera|As\s+inscri|Os\s+interessados))",
        texto,
        re.I,
    )
    if m_cargos:
        resumo = _limpar(m_cargos[1]).rstrip(" .;,:")
        if resumo:
            registro["requisitos"] = resumo[:300]


def coletar_pci(limite=30, uf_filtro=None, enriquecer_detalhes=5):
    """Coleta editais públicos do PCI Concursos respeitando robots.txt."""
    regras = RobotFileParser(BASE + "/robots.txt")
    try:
        regras.parse(baixar(regras.url, "text/plain").decode("utf-8", errors="replace").splitlines())
    except HTTPError as exc:
        if exc.code != 404:
            raise
        regras.parse([])
    atraso = max(1, regras.crawl_delay(AGENTE) or 0, regras.crawl_delay("*") or 0)

    def pagina(url, accept="text/html"):
        if not regras.can_fetch(AGENTE, url):
            raise ValueError("Consulta vedada pelo robots.txt: " + url)
        sleep(atraso)
        return baixar(url, accept)

    conteudo = pagina(LISTA_URL)
    registros, pendencias = _parse_cards(conteudo, limite=limite, uf_filtro=uf_filtro)
    if not registros:
        raise ValueError("Nenhum concurso reconhecido na listagem do PCI Concursos.")

    for registro in registros[:max(0, enriquecer_detalhes)]:
        _enriquecer_noticia(registro, pagina)

    return registros, pendencias
