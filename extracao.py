"""Extrai texto, tabelas e campos estruturados de HTML e editais em PDF."""
import io
import re
import unicodedata
from html.parser import HTMLParser

import pdfplumber

CAMPOS = {
    "titulo": r"(?:concurso|título|edital)",
    "orgao": r"(?:órgão|organizador)",
    "uf": r"(?:UF|estado)",
    "nivel": r"(?:escolaridade|nível)",
    "prazo": r"(?:inscrições até|fim das inscrições|prazo)",
    "url": r"(?:fonte oficial|link oficial)",
}

_RE_VALOR = re.compile(r"R\$\s*\d{1,3}(?:\.\d{3})*,\d{2}")


class PaginaHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.ignored = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "template"}:
            self.ignored += 1
        if tag in {"h1", "h2", "p", "div", "li", "tr", "br"}:
            self.parts.append("\n")
        if tag in {"td", "th"}:
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag in {"script", "style", "template"}:
            self.ignored = max(0, self.ignored - 1)
        if tag in {"h1", "h2", "p", "div", "li", "tr", "title"}:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.ignored:
            self.parts.append(data)


def _limpar(texto):
    return " ".join(str(texto or "").split())


def _sem_acentos(texto):
    return "".join(
        c for c in unicodedata.normalize("NFD", str(texto or "").casefold())
        if not unicodedata.combining(c)
    )


def normalizar_nivel(texto):
    """Mapeia descrições livres de escolaridade para os níveis do catálogo."""
    bruto = _sem_acentos(texto)
    if not bruto:
        return ""
    encontrados = set()
    if any(k in bruto for k in ("superior", "graduacao", "bacharel", "licenciatura", "pos-graduacao", "mestrado", "doutorado", 'nivel "e"', "nivel e")):
        encontrados.add("Superior")
    if any(k in bruto for k in ("tecnico", "tecnologo")):
        encontrados.add("Técnico")
    if any(k in bruto for k in ("medio", 'nivel "d"', "nivel d")):
        encontrados.add("Médio")
    if any(k in bruto for k in ("fundamental", "alfabetizado")):
        encontrados.add("Fundamental")
    if len(encontrados) > 1 or "varios" in bruto:
        return "Vários"
    return next(iter(encontrados), "")


def extrair(conteudo, tipo):
    if tipo == "html":
        parser = PaginaHTML()
        try:
            html = conteudo.decode("utf-8-sig")
        except UnicodeDecodeError:
            html = conteudo.decode("cp1252", errors="replace")
        parser.feed(html)
        return "".join(parser.parts).strip()

    if not conteudo.startswith(b"%PDF-"):
        raise ValueError("Selecione um arquivo PDF válido.")
    try:
        with pdfplumber.open(io.BytesIO(conteudo)) as pdf:
            return "\n".join(page.extract_text() or "" for page in pdf.pages[:20]).strip()
    except Exception as exc:
        raise ValueError("Não foi possível ler o PDF. Verifique se ele abre e está desbloqueado.") from exc


def extrair_pdf_completo(conteudo, max_paginas_texto=15, max_paginas_tabelas=6):
    """Extrai texto e tabelas de um PDF de edital."""
    if not conteudo.startswith(b"%PDF-"):
        raise ValueError("O arquivo baixado não é um PDF válido.")
    try:
        with pdfplumber.open(io.BytesIO(conteudo)) as pdf:
            textos = [page.extract_text() or "" for page in pdf.pages[:max_paginas_texto]]
            tabelas = []
            for pagina in pdf.pages[:max_paginas_tabelas]:
                for linhas in pagina.extract_tables() or []:
                    if linhas:
                        tabelas.append({"pagina": pagina.page_number, "linhas": linhas})
        return {"texto": "\n".join(textos).strip(), "tabelas": tabelas}
    except Exception as exc:
        raise ValueError("Não foi possível ler o PDF do edital.") from exc


def _valor_para_float(valor):
    num = valor.replace("R$", "").replace(".", "").replace(",", ".").strip()
    try:
        return float(num)
    except ValueError:
        return 0.0


def _resumir_remuneracao(valores):
    unicos = []
    vistos = set()
    for v in valores:
        norm = _limpar(v).replace("R$ ", "R$").replace("R$", "R$ ")
        if norm not in vistos and _valor_para_float(norm) > 0:
            vistos.add(norm)
            unicos.append(norm)
    if not unicos:
        return ""
    unicos.sort(key=_valor_para_float)
    return unicos[0] if len(unicos) == 1 else f"{unicos[0]} a {unicos[-1]}"


def _mapear_colunas(cabecalho):
    mapa = {}
    for idx, celula in enumerate(cabecalho):
        col = _sem_acentos(celula)
        if not col:
            continue
        if any(k in col for k in ("cargo", "area", "subarea", "funcao", "especialidade")):
            mapa.setdefault("cargo", idx)
        elif "vaga" in col:
            mapa.setdefault("vagas", idx)
        elif any(k in col for k in ("remunera", "salario", "vencimento", "provento")):
            mapa.setdefault("remuneracao", idx)
        elif any(k in col for k in ("carga horaria", "jornada", "regime")):
            mapa.setdefault("jornada", idx)
        elif any(k in col for k in ("cidade", "municipio", "campus", "campi", "local", "lotacao")):
            mapa.setdefault("localizacao", idx)
        elif any(k in col for k in ("qualificacao", "requisito", "escolaridade", "formacao")):
            mapa.setdefault("requisitos", idx)
    return mapa


def analisar_edital_pdf(conteudo):
    """Extrai metadados estruturados (vagas, remuneração, jornada, local, requisitos) de um PDF."""
    extraido = extrair_pdf_completo(conteudo)
    texto = extraido["texto"]
    tabelas = extraido["tabelas"]

    cargos, vagas_nums, remuneracoes, jornadas, cidades, requisitos, niveis = [], [], [], [], [], [], []
    tem_total_vagas = None

    for tabela in tabelas:
        linhas = [[_limpar(c) for c in row] for row in tabela["linhas"] if any(_limpar(c) for c in row)]
        if len(linhas) < 2:
            continue
        mapa = _mapear_colunas(linhas[0])
        if not mapa or ("cargo" not in mapa and "vagas" not in mapa and "remuneracao" not in mapa):
            continue

        # Detecta nível indicado no cabeçalho (ex.: CARGOS NÍVEL "D" ou "E")
        cab_texto = " ".join(linhas[0])
        nivel_cab = normalizar_nivel(cab_texto)
        if nivel_cab:
            niveis.append(nivel_cab)

        for row in linhas[1:]:
            row_texto = " ".join(row)
            if "cargos nivel" in _sem_acentos(row_texto):
                nv = normalizar_nivel(row_texto)
                if nv:
                    niveis.append(nv)
                mapa = _mapear_colunas(row) or mapa
                continue
            if "total de vagas" in _sem_acentos(row_texto):
                m = re.search(r"\b(\d+)\b", row_texto)
                if m:
                    tem_total_vagas = int(m[1])
                continue
            if "cargo" in mapa and mapa["cargo"] < len(row):
                cargo = row[mapa["cargo"]]
                if cargo and cargo not in cargos:
                    cargos.append(cargo)
            if "vagas" in mapa and mapa["vagas"] < len(row):
                m = re.search(r"\b(\d+)\b", row[mapa["vagas"]])
                if m:
                    vagas_nums.append(int(m[1]))
            if "remuneracao" in mapa and mapa["remuneracao"] < len(row):
                remuneracoes.extend(_RE_VALOR.findall(row[mapa["remuneracao"]]))
            if "jornada" in mapa and mapa["jornada"] < len(row):
                j = row[mapa["jornada"]]
                if j:
                    if j.isdigit():
                        j = f"{j} horas semanais"
                    if j not in jornadas:
                        jornadas.append(j)
            if "localizacao" in mapa and mapa["localizacao"] < len(row):
                loc = row[mapa["localizacao"]]
                if loc and "quaisquer" not in _sem_acentos(loc) and loc not in cidades:
                    cidades.append(loc)
            if "requisitos" in mapa and mapa["requisitos"] < len(row):
                req = row[mapa["requisitos"]]
                if req:
                    if req not in requisitos:
                        requisitos.append(req)
                    nv = normalizar_nivel(req)
                    if nv:
                        niveis.append(nv)

    # Fallbacks por Regex no texto extraído do PDF
    vagas_texto = ""
    total_vagas = tem_total_vagas if tem_total_vagas is not None else (sum(vagas_nums) if vagas_nums else 0)
    if total_vagas > 0:
        vagas_texto = f"{total_vagas} " + ("vaga" if total_vagas == 1 else "vagas")
    else:
        m_vagas = re.search(r"(?:total\s+de\s+vagas|n[úu]mero\s+de\s+vagas|vagas)\s*[:–-]?\s*(\d{1,4})\b", texto, re.I)
        if m_vagas:
            n = int(m_vagas[1])
            vagas_texto = f"{n} " + ("vaga" if n == 1 else "vagas")

    if not remuneracoes:
        for m in re.finditer(r"(?:remunera[çc][ãa]o|vencimento(?:\s+b[áa]sico)?|sal[áa]rio|retribui[çc][ãa]o)[^\n]{0,90}?(R\$\s*\d{1,3}(?:\.\d{3})*,\d{2})", texto, re.I):
            remuneracoes.append(m[1])
    remuneracao = _resumir_remuneracao(remuneracoes)

    if not jornadas:
        m_jornada = re.search(r"(\b\d{2}\s*(?:\([a-z]+\)\s*)?horas?\s*semanais\b|\b\d{2}h\s*semanais\b|dedica[çc][ãa]o\s+exclusiva)", texto, re.I)
        if m_jornada:
            jornadas.append(_limpar(m_jornada[1]))
    jornada = "; ".join(jornadas[:2])[:120]

    if not cidades:
        for m_cid in re.finditer(r"(?:cidade\s+de\s+|munic[íi]pio\s+de\s+|campus\s+[^\n,]{0,30}?\s+em\s+)([A-ZÁÉÍÓÚÂÊÔÃÕ][A-Za-záéíóúâêôãõç\s]{2,35}(?:\([A-Z]{2}\)|-[A-Z]{2}))", texto):
            c = _limpar(m_cid[1])
            if c not in cidades:
                cidades.append(c)
    localizacao = ", ".join(cidades[:4])[:160]

    req_resumo = "; ".join(requisitos[:2]) if requisitos else ""
    if cargos and not req_resumo:
        req_resumo = "Cargos/Áreas: " + ", ".join(cargos[:5])
    elif cargos and req_resumo:
        req_resumo = f"Cargos: {', '.join(cargos[:3])} — {req_resumo}"
    req_resumo = req_resumo[:300]

    niveis_unicos = set(niveis)
    if "Vários" in niveis_unicos or len(niveis_unicos) > 1:
        nivel = "Vários"
    elif len(niveis_unicos) == 1:
        nivel = next(iter(niveis_unicos))
    else:
        nivel = normalizar_nivel(texto[:5000])

    return {
        "texto": texto,
        "cargos": cargos,
        "vagas": vagas_texto,
        "remuneracao": remuneracao,
        "jornada": jornada,
        "localizacao": localizacao,
        "requisitos": req_resumo,
        "nivel": nivel,
    }


def sugerir(texto):
    dados = {}
    for campo, rotulo in CAMPOS.items():
        match = re.search(rf"^\s*{rotulo}\s*[:–-]\s*([^\n]+)", texto, re.I | re.M)
        if match:
            dados[campo] = _limpar(match[1])[:250]
    prazo = re.search(r"\b(\d{2})[/.-](\d{2})[/.-](\d{4})\b", dados.get("prazo", ""))
    dados["prazo"] = f"{prazo[3]}-{prazo[2]}-{prazo[1]}" if prazo else ""
    dados["uf"] = dados.get("uf", "").upper()
    dados["nivel"] = dados.get("nivel", "").lower().capitalize()
    return dados


def sugerir_de_pdf(conteudo):
    """Combina extração por rótulos e análise de tabelas/regex para PDFs enviados no site."""
    analise = analisar_edital_pdf(conteudo)
    dados = sugerir(analise["texto"])
    for chave in ("vagas", "remuneracao", "jornada", "localizacao", "requisitos"):
        if analise.get(chave):
            dados[chave] = analise[chave]
    if not dados.get("nivel") and analise.get("nivel"):
        dados["nivel"] = analise["nivel"]
    return dados, analise["texto"]


def inspecionar_tabelas(arquivo):
    """Mostra linhas e páginas para adaptar a técnica do notebook da aula."""
    resultados = []
    with pdfplumber.open(arquivo) as pdf:
        for pagina in pdf.pages[:20]:
            for linhas in pagina.extract_tables():
                resultados.append({"pagina": pagina.page_number, "linhas": linhas})
    return resultados
