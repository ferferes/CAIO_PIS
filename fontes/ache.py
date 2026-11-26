"""
Coleta de concursos públicos do portal Ache Concursos (acheconcursos.com.br).

Estratégia:
  1. Web Scraping: lista de /concursos-abertos — tabela com órgão, nível, salário,
     datas de inscrição, vagas e link para a página de detalhe.
  2. Web Scraping (detalhe): página por concurso — extrai UF via slug da URL,
     datas estruturadas, vagas e link direto do PDF do edital hospedado no próprio site.
  3. PDF Scraping: baixa o edital em PDF e usa pdfplumber para enriquecer
     vagas, remuneração, jornada, localização e requisitos.

robots.txt: permite User-agent: * em todas as rotas relevantes.
"""
import re
from time import sleep
from urllib.error import HTTPError
from urllib.parse import urljoin
from urllib.robotparser import RobotFileParser

from lxml import etree, html

from extracao import analisar_edital_pdf
from fontes.http import AGENTE, LIMITE_PDF, baixar
from opcoes import UFS

BASE = "https://www.acheconcursos.com.br"
LISTA_URL = f"{BASE}/concursos-abertos"

# Slug da URL → sigla UF
_SLUG_UF = {
    "acre": "AC", "alagoas": "AL", "amapa": "AP", "amazonas": "AM",
    "bahia": "BA", "ceara": "CE", "distrito-federal": "DF",
    "espirito-santo": "ES", "goias": "GO", "maranhao": "MA",
    "mato-grosso-do-sul": "MS", "mato-grosso": "MT",
    "minas-gerais": "MG", "para": "PA", "paraiba": "PB",
    "parana": "PR", "pernambuco": "PE", "piaui": "PI",
    "rio-de-janeiro": "RJ", "rio-grande-do-norte": "RN",
    "rio-grande-do-sul": "RS", "rondonia": "RO", "roraima": "RR",
    "santa-catarina": "SC", "sao-paulo": "SP", "sergipe": "SE",
    "tocantins": "TO", "brasil": "BR",
}

_RE_ID = re.compile(r"-(\d+)$")
_RE_DATA = re.compile(r"\b(\d{2}/\d{2}/\d{4})\b")
_RE_VAGAS = re.compile(r"(\d[\d.]*)\s*vagas?", re.I)


def _limpar(texto):
    return " ".join(str(texto or "").split())


def _documento(conteudo):
    try:
        return html.fromstring(conteudo.decode("utf-8", "replace"))
    except (etree.ParserError, ValueError) as exc:
        raise ValueError("Ache Concursos não retornou HTML legível.") from exc


def _uf_de_slug(url):
    """Extrai a UF a partir do slug /concursos-<estado>/."""
    m = re.search(r"/concursos-([a-z-]+)/", url)
    if m:
        slug = m.group(1)
        return _SLUG_UF.get(slug, "BR")
    return "BR"


def _parse_listagem(doc, limite=50):
    """Lê a tabela principal de /concursos-abertos."""
    registros = []
    vistos = set()
    for tr in doc.xpath("//table//tr"):
        tds = tr.xpath(".//td")
        if len(tds) < 2:
            continue
        a = tr.xpath(".//a[@href]")
        if not a:
            continue
        href = a[0].get("href", "")
        if not _RE_ID.search(href):
            continue
        url = urljoin(BASE, href) if href.startswith("/") else href
        if url in vistos:
            continue
        vistos.add(url)

        # Texto completo da linha: "Órgão | Nível: X | R$ Y"
        linha = _limpar(tr.text_content())
        titulo = _limpar(a[0].text_content())

        # Nível: "Nível: Superior" / "Nível: Médio e Superior"
        m_nivel = re.search(r"[Nn]ível[:.]?\s*([^\|]+?)(?:\||R\$|$)", linha)
        nivel_raw = _limpar(m_nivel.group(1)) if m_nivel else ""

        # Salário: "R$ 35.874,14"
        m_sal = re.search(r"R\$\s*([\d.,]+)", linha)
        remuneracao = f"até R$ {_limpar(m_sal.group(1))}" if m_sal else ""

        uf = _uf_de_slug(url)

        registros.append({
            "_url": url,
            "_titulo": titulo,
            "_nivel_raw": nivel_raw,
            "_remuneracao": remuneracao,
            "_uf": uf,
        })
        if len(registros) >= limite:
            break
    return registros


def _parse_detalhe(conteudo, rascunho):
    """
    Enriquece o rascunho com dados extraídos da página de detalhe.
    Retorna o rascunho atualizado com todos os campos do catálogo.
    """
    doc = _documento(conteudo)

    h1 = doc.xpath("//h1")
    titulo = _limpar(h1[0].text_content()) if h1 else rascunho["_titulo"]

    # Bloco de info: procura texto que contenha "Encerra inscrições" e extrai as datas
    encerra = ""
    abre = ""
    vagas_txt = ""
    sal_info = ""

    for bloco in doc.xpath("//*[contains(@class,'info') or contains(@class,'dados') or contains(@class,'detail')]"):
        txt = _limpar(bloco.text_content())
        # Verifica se é o bloco certo (contém "Encerra inscrições")
        if "encerra inscri" not in txt.lower() and "abertura inscri" not in txt.lower():
            continue
        # Extrai datas na sequência do bloco
        # Formato: "Abertura inscrições DD/MM/YYYY Encerra inscrições DD/MM/YYYY ..."
        m_abre = re.search(r"(?:abertura inscri[çc][oõ]es?)\s+(\d{2}/\d{2}/\d{4})", txt, re.I)
        m_enc = re.search(r"(?:encerra inscri[çc][oõ]es?)\s+(\d{2}/\d{2}/\d{4})", txt, re.I)
        if m_abre:
            abre = m_abre.group(1)
        if m_enc:
            encerra = m_enc.group(1)
        # Vagas
        m_vagas2 = re.search(r"total\s+de\s+vagas?\s+(\d[\d.]*|a\s+divulgar|cadastro\s+reserva)", txt, re.I)
        if m_vagas2:
            vagas_txt = _limpar(m_vagas2.group(1))
        # Salário
        m_sal2 = re.search(r"sal[aá]rios?\s+at[eé]\s+(R\$\s*[\d.,]+)", txt, re.I)
        if m_sal2:
            sal_info = _limpar(m_sal2.group(1))
        break  # Bloco correto encontrado

    prazo_match = _RE_DATA.search(encerra) or _RE_DATA.search(abre)
    prazo = prazo_match.group(1) if prazo_match else ""

    vagas_num = vagas_txt if vagas_txt and vagas_txt.lower() not in ("a divulgar",) else ""

    remuneracao = rascunho["_remuneracao"]
    if sal_info:
        remuneracao = f"até {sal_info}" if "até" not in sal_info.lower() else sal_info

    # Link do PDF do edital hospedado no Ache Concursos
    pdf_url = ""
    for a in doc.xpath("//a[@href]"):
        href = a.get("href", "")
        if "/imagens/anexo/" in href and href.endswith(".pdf"):
            pdf_url = urljoin(BASE, href) if href.startswith("/") else href
            break

    # Texto do artigo para nível (fallback)
    texto_art = ""
    for art in doc.xpath('//div[contains(@class,"texto")] | //div[contains(@class,"materia")] | //article'):
        texto_art = _limpar(art.text_content())[:3000]
        if texto_art:
            break

    from extracao import normalizar_nivel
    nivel = normalizar_nivel(rascunho["_nivel_raw"]) or normalizar_nivel(texto_art) or "Vários"

    return {
        "titulo": titulo[:220],
        "orgao": rascunho["_titulo"][:180],
        "uf": rascunho["_uf"],
        "nivel": nivel,
        "prazo": prazo,
        "url": rascunho["_url"],
        "vagas": vagas_num[:60],
        "remuneracao": remuneracao[:60],
        "jornada": "",
        "localizacao": rascunho["_uf"],
        "requisitos": "",
        "edital_pdf_url": pdf_url,
        "_pdf_url": pdf_url,
        "_texto_art": texto_art,
    }


def _enriquecer_pdf(registro, fetch_fn):
    """Baixa o edital PDF e executa PDF scraping com pdfplumber."""
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


def coletar_ache(limite=30, enriquecer_pdf=10):
    """
    Coleta editais abertos do Ache Concursos com web scraping e PDF scraping.

    Args:
        limite: quantos concursos coletar da listagem (máx 300+).
        enriquecer_pdf: quantos dos primeiros registros terão o PDF baixado e analisado.
    """
    regras = RobotFileParser(BASE + "/robots.txt")
    try:
        regras.parse(baixar(regras.url, "text/plain").decode("utf-8", errors="replace").splitlines())
    except HTTPError as exc:
        if exc.code != 404:
            raise
        regras.parse([])
    atraso = max(1, regras.crawl_delay(AGENTE) or 0, regras.crawl_delay("*") or 0)

    def pagina(url, accept="text/html", limite=None):
        if not regras.can_fetch(AGENTE, url):
            raise ValueError("Consulta vedada pelo robots.txt: " + url)
        sleep(atraso)
        if limite is None:
            return baixar(url, accept)
        return baixar(url, accept, limite=limite)

    conteudo_lista = pagina(LISTA_URL)
    doc_lista = _documento(conteudo_lista)
    rascunhos = _parse_listagem(doc_lista, limite=limite)
    if not rascunhos:
        raise ValueError("Nenhum concurso reconhecido na listagem do Ache Concursos.")

    registros, pendencias = [], []
    for i, rascunho in enumerate(rascunhos):
        url = rascunho["_url"]
        try:
            conteudo_det = pagina(url)
            registro = _parse_detalhe(conteudo_det, rascunho)
            if not registro.get("prazo"):
                pendencias.append(f"{url}: prazo de inscrições não reconhecido")
                continue
            if i < enriquecer_pdf:
                _enriquecer_pdf(registro, pagina)
            # Remove campos internos de controle
            for k in list(registro):
                if k.startswith("_"):
                    del registro[k]
            registros.append(registro)
        except (OSError, ValueError) as exc:
            pendencias.append(f"{url}: {exc}")

    return registros, pendencias
