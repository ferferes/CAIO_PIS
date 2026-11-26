"""Pipeline ETL de coleta (Web Scraping, PDF Scraping, API e arquivos) para o catálogo."""
import argparse
import json
import os
import re
import sys
import threading
import time
import unicodedata
from collections.abc import Mapping
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from banco import banco, inicializar_banco
from extracao import extrair, normalizar_nivel, sugerir, sugerir_de_pdf
from opcoes import NIVEIS, UFS

_COLETA_LOCK = threading.Lock()
_TZ_SP = ZoneInfo("America/Sao_Paulo")


class EditalEncerradoError(ValueError):
    """Indica que o prazo de inscrição do edital já passou."""


def _agora_iso():
    return datetime.now(_TZ_SP).replace(microsecond=0).isoformat()


def _hoje_sp():
    return datetime.now(_TZ_SP).date()


def _validar_http_url(link):
    url = urlsplit(link)
    return not (
        url.scheme not in {"https", "http"}
        or not url.hostname
        or url.username is not None
        or any(c.isspace() or c == chr(92) for c in link)
    )


def _norm_texto(valor):
    return "".join(
        c for c in unicodedata.normalize("NFD", str(valor or "").casefold())
        if not unicodedata.combining(c)
    )


def _chave_orgao_normalizada(orgao):
    """Normaliza o nome do órgão para deduplicar editais iguais vindos de portais diferentes."""
    txt = _norm_texto(orgao)
    txt = re.sub(r"\b(?:concurso|publico|processo|seletivo|edital|n|no|de|da|do|dos|das|e)\b", " ", txt)
    txt = re.sub(r"[^a-z0-9\s]", " ", txt)
    return " ".join(txt.split())


def normalizar_edital(registro, origem, *, ignorar_encerrados=True):
    if not isinstance(registro, Mapping):
        raise ValueError("Cada concurso deve ser um objeto com os campos do catálogo.")
    item = {}
    for chave in ("titulo", "orgao", "uf", "nivel", "prazo", "url"):
        valor = registro.get(chave)
        item[chave] = "" if valor is None else str(valor).strip()
    faltantes = [chave for chave, valor in item.items() if not valor]
    if faltantes:
        raise ValueError("Preencha: " + ", ".join(faltantes))
    item["uf"] = item["uf"].upper()
    nivel_norm = item["nivel"].capitalize()
    if nivel_norm not in NIVEIS:
        nivel_norm = normalizar_nivel(item["nivel"]) or nivel_norm
    item["nivel"] = nivel_norm
    if item["uf"] not in UFS or item["nivel"] not in NIVEIS:
        raise ValueError("Use uma UF e uma escolaridade reconhecidas pelo catálogo.")
    prazo = item["prazo"]
    try:
        dia = datetime.strptime(prazo, "%d/%m/%Y").date() if "/" in prazo else date.fromisoformat(prazo)
    except ValueError as exc:
        raise ValueError("Informe o prazo em AAAA-MM-DD ou DD/MM/AAAA.") from exc

    if ignorar_encerrados and dia < _hoje_sp():
        raise EditalEncerradoError(f"Inscrições encerradas em {dia.isoformat()}.")

    item["prazo"] = dia.isoformat()
    if not _validar_http_url(item["url"]):
        raise ValueError("Informe uma URL oficial em HTTP ou HTTPS.")
    item["origem"] = origem
    for extra in ("vagas", "remuneracao", "jornada", "localizacao", "requisitos"):
        val = registro.get(extra)
        item[extra] = "" if val is None else " ".join(str(val).split())[:300]
    pdf_url = str(registro.get("edital_pdf_url") or "").strip()
    item["edital_pdf_url"] = pdf_url if pdf_url and _validar_http_url(pdf_url) else ""
    item["atualizado_em"] = _agora_iso()
    return item


def ler_fonte(arquivo):
    extensao = arquivo.suffix.lower()
    if extensao == ".json":
        dados = json.loads(arquivo.read_text(encoding="utf-8-sig"))
        if not isinstance(dados, (list, dict)):
            raise ValueError("O JSON deve conter um objeto ou uma lista de objetos.")
        return dados if isinstance(dados, list) else [dados], "JSON"
    tipos = {".pdf": "pdf", ".html": "html", ".htm": "html"}
    if extensao not in tipos:
        raise ValueError("Escolha um arquivo PDF, HTML ou JSON.")
    tipo = tipos[extensao]
    conteudo = arquivo.read_bytes()
    if tipo == "pdf":
        dados, _ = sugerir_de_pdf(conteudo)
        return [dados], "PDF"
    texto = extrair(conteudo, tipo)
    return [sugerir(texto)], "HTML"


def _buscar_existente(db, item):
    """Localiza concurso já cadastrado por URL, link do PDF ou órgão normalizado + UF + prazo."""
    existente = db.execute(
        """SELECT id, titulo, orgao, prazo, nivel, vagas, remuneracao, jornada, localizacao, requisitos, edital_pdf_url
           FROM concursos WHERE exemplo = 0 AND (
               (url != '' AND url = ?)
               OR (edital_pdf_url != '' AND edital_pdf_url = ?)
               OR (titulo = ? AND orgao = ? AND uf = ? AND prazo = ?)
           ) LIMIT 1""",
        (
            item["url"],
            item["edital_pdf_url"],
            item["titulo"],
            item["orgao"],
            item["uf"],
            item["prazo"],
        ),
    ).fetchone()
    if existente is not None:
        return existente

    orgao_alvo = _chave_orgao_normalizada(item["orgao"])
    if len(orgao_alvo) >= 5:
        candidatos = db.execute(
            """SELECT id, titulo, orgao, prazo, nivel, vagas, remuneracao, jornada, localizacao, requisitos, edital_pdf_url
               FROM concursos WHERE exemplo = 0 AND uf = ? AND prazo = ?""",
            (item["uf"], item["prazo"]),
        ).fetchall()
        for cand in candidatos:
            if _chave_orgao_normalizada(cand["orgao"]) == orgao_alvo:
                return cand
    return None


def _persistir_item(db, item):
    """Insere ou atualiza concurso existente por chave natural, PDF ou URL, evitando duplicatas."""
    existente = _buscar_existente(db, item)

    if existente is None:
        cursor = db.execute(
            """INSERT INTO concursos
               (titulo, orgao, uf, nivel, prazo, url, origem, situacao,
                vagas, remuneracao, jornada, localizacao, requisitos, edital_pdf_url, atualizado_em)
               VALUES
               (:titulo, :orgao, :uf, :nivel, :prazo, :url, :origem, 'Publicado',
                :vagas, :remuneracao, :jornada, :localizacao, :requisitos, :edital_pdf_url, :atualizado_em)""",
            item,
        )
        return "inserido", cursor.lastrowid

    novos_valores = {
        "titulo": item["titulo"] or existente["titulo"],
        "prazo": item["prazo"] or existente["prazo"],
        "nivel": item["nivel"] if existente["nivel"] == "Vários" and item["nivel"] != "Vários" else existente["nivel"],
        "vagas": item["vagas"] or existente["vagas"],
        "remuneracao": item["remuneracao"] or existente["remuneracao"],
        "jornada": item["jornada"] or existente["jornada"],
        "localizacao": item["localizacao"] or existente["localizacao"],
        "requisitos": item["requisitos"] or existente["requisitos"],
        "edital_pdf_url": item["edital_pdf_url"] or existente["edital_pdf_url"],
    }
    mudou = any(novos_valores[k] != existente[k] for k in novos_valores)
    if mudou:
        db.execute(
            """UPDATE concursos SET
               titulo = :titulo, prazo = :prazo, nivel = :nivel,
               vagas = :vagas, remuneracao = :remuneracao, jornada = :jornada,
               localizacao = :localizacao, requisitos = :requisitos,
               edital_pdf_url = :edital_pdf_url, atualizado_em = :atualizado_em
               WHERE id = :id""",
            {**novos_valores, "atualizado_em": item["atualizado_em"], "id": existente["id"]},
        )
        return "atualizado", existente["id"]
    return "igual", existente["id"]


def importar_lote(registros, origem, *, publicar=False, pular_invalidos=False):
    """Valida o lote, descarta editais encerrados, persiste com deduplicação e retorna estatísticas."""
    itens = []
    for indice, registro in enumerate(registros, start=1):
        try:
            itens.append(normalizar_edital(registro, origem, ignorar_encerrados=True))
        except EditalEncerradoError:
            # Ignora silenciosamente concursos cujo prazo de inscrição já fechou
            continue
        except ValueError as exc:
            if pular_invalidos:
                continue
            raise ValueError(f"Registro {indice}: {exc}") from exc

    inseridos = 0
    atualizados = 0
    novos_ids = []
    if publicar and itens:
        inicializar_banco()
        with banco() as db:
            for item in itens:
                acao, row_id = _persistir_item(db, item)
                if acao == "inserido":
                    inseridos += 1
                    novos_ids.append(row_id)
                elif acao == "atualizado":
                    atualizados += 1
    return itens, inseridos, atualizados, novos_ids


def importar_catalogo(registros, origem, *, publicar=False):
    """Compatível com chamadas legadas: valida o lote e retorna (itens, inseridos)."""
    itens, inseridos, _, _ = importar_lote(registros, origem, publicar=publicar)
    return itens, inseridos


def _formatar_mensagem_telegram(conc):
    """Monta mensagem rica com dados extraídos do web scraping e do PDF scraping."""
    linhas = [
        "📢 Novo concurso aberto compatível com seu perfil:",
        f"• {conc['titulo']}",
        f"• Órgão: {conc['orgao']} ({conc['uf']} · {conc['nivel']})",
    ]
    if conc["vagas"]:
        linhas.append(f"• Vagas: {conc['vagas']}")
    if conc["remuneracao"]:
        linhas.append(f"• Remuneração: {conc['remuneracao']}")
    if conc["localizacao"]:
        linhas.append(f"• Local: {conc['localizacao']}")
    if conc["jornada"]:
        linhas.append(f"• Jornada: {conc['jornada']}")
    linhas.append(f"• Inscrições até: {conc['prazo']}")
    if conc["requisitos"]:
        linhas.append(f"• Detalhes/Requisitos: {conc['requisitos']}")
    linhas.append(f"• Página do concurso: {conc['url']}")
    if conc["edital_pdf_url"]:
        linhas.append(f"• Edital (PDF): {conc['edital_pdf_url']}")
    return "\n".join(linhas)


def notificar_novos_concursos(novos_ids):
    """Envia avisos via Telegram para perfis compatíveis quando CAIO_NOTIFICAR_TELEGRAM=1."""
    if not novos_ids or os.getenv("CAIO_NOTIFICAR_TELEGRAM") != "1":
        return 0
    token = os.getenv("TEL_TOKEN", "").strip()
    if not token:
        return 0
    try:
        import telebot
    except ImportError:
        return 0

    bot = telebot.TeleBot(token)
    enviados = 0
    agora = _agora_iso()
    hoje_iso = _hoje_sp().isoformat()
    with banco() as db:
        perfis = db.execute(
            "SELECT id, nome, q, uf, nivel, telegram_chat_id FROM perfis WHERE avisos_telegram = 1 AND telegram_chat_id IS NOT NULL"
        ).fetchall()
        placeholders = ",".join("?" for _ in novos_ids)
        concursos = db.execute(
            f"SELECT * FROM concursos WHERE id IN ({placeholders}) AND situacao = 'Publicado' AND prazo >= ?",
            (*novos_ids, hoje_iso),
        ).fetchall()
        for perfil in perfis:
            chat_id = perfil["telegram_chat_id"]
            termos = _norm_texto(perfil["q"]).split()
            for conc in concursos:
                if perfil["uf"] and conc["uf"] != perfil["uf"]:
                    continue
                if perfil["nivel"] and conc["nivel"] != "Vários" and conc["nivel"] != perfil["nivel"]:
                    continue
                alvo = _norm_texto(
                    f"{conc['titulo']} {conc['orgao']} {conc['requisitos']} {conc['localizacao']}"
                )
                if not all(t in alvo for t in termos):
                    continue
                ja_enviado = db.execute(
                    "SELECT 1 FROM notificacoes_enviadas WHERE concurso_id = ? AND chat_id = ?",
                    (conc["id"], chat_id),
                ).fetchone()
                if ja_enviado:
                    continue
                texto = _formatar_mensagem_telegram(conc)
                try:
                    bot.send_message(chat_id, texto, disable_web_page_preview=True)
                    db.execute(
                        "INSERT OR IGNORE INTO notificacoes_enviadas (concurso_id, chat_id, enviado_em) VALUES (?, ?, ?)",
                        (conc["id"], chat_id, agora),
                    )
                    enviados += 1
                except Exception:
                    continue
    return enviados


def ultima_coleta():
    inicializar_banco()
    with banco() as db:
        row = db.execute("SELECT * FROM coletas ORDER BY id DESC LIMIT 1").fetchone()
        return dict(row) if row else None


def coleta_em_andamento():
    return _COLETA_LOCK.locked()


def executar_coleta_completa(
    *,
    limite_pci=100,
    limite_ache=80,
    limite_cnb=80,
    enriquecer_pci=25,
    enriquecer_ache=20,
    enriquecer_cnb=20,
    baixar_pdf=True,
    publicar=True,
):
    """
    Executa web scraping + PDF scraping amplo nos portais de concursos públicos,
    mantendo apenas editais com inscrições ainda abertas (prazo >= hoje).
    """
    if not _COLETA_LOCK.acquire(blocking=False):
        return {"em_andamento": True, "status": "em_andamento"}

    try:
        from fontes.ache import coletar_ache
        from fontes.cnb import coletar_cnb
        from fontes.pci import coletar_pci

        inicializar_banco()
        fontes_config = [
            ("PCI Concursos", lambda: coletar_pci(limite=limite_pci, enriquecer_detalhes=enriquecer_pci)),
            ("Ache Concursos", lambda: coletar_ache(limite=limite_ache, enriquecer_pdf=enriquecer_ache if baixar_pdf else 0)),
            ("Concursos no Brasil", lambda: coletar_cnb(limite=limite_cnb, enriquecer_pdf=enriquecer_cnb if baixar_pdf else 0)),
        ]

        total_encontrados = 0
        total_inseridos = 0
        total_atualizados = 0
        total_pdfs = 0
        todos_novos_ids = []
        pendencias = []
        itens_coletados = []

        for origem, coletor in fontes_config:
            try:
                registros, pend_fonte = coletor()
                pendencias.extend(pend_fonte)
                total_pdfs += sum(1 for r in registros if r.get("pdf_processado"))
                itens, inseridos, atualizados, novos_ids = importar_lote(
                    registros, origem, publicar=publicar, pular_invalidos=True
                )
                total_encontrados += len(itens)
                total_inseridos += inseridos
                total_atualizados += atualizados
                todos_novos_ids.extend(novos_ids)
                itens_coletados.extend(itens)
            except (OSError, ValueError, TypeError) as exc:
                pendencias.append(f"{origem}: {exc}")

        executado_em = _agora_iso()
        status = "sucesso" if total_encontrados > 0 else "erro"
        detalhes = "; ".join(pendencias[:5])[:500]
        if publicar:
            with banco() as db:
                db.execute(
                    """INSERT INTO coletas
                       (executado_em, origem, status, encontrados, inseridos, atualizados, pdfs_processados, detalhes)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        executado_em,
                        "Web + PDF Amplo (PCI, Ache Concursos, Concursos no Brasil)",
                        status,
                        total_encontrados,
                        total_inseridos,
                        total_atualizados,
                        total_pdfs,
                        detalhes,
                    ),
                )
            notificar_novos_concursos(todos_novos_ids)

        return {
            "em_andamento": False,
            "executado_em": executado_em,
            "status": status,
            "encontrados": total_encontrados,
            "inseridos": total_inseridos,
            "atualizados": total_atualizados,
            "pdfs_processados": total_pdfs,
            "pendencias": pendencias,
            "itens": itens_coletados,
        }
    finally:
        _COLETA_LOCK.release()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    fontes = parser.add_mutually_exclusive_group(required=True)
    fontes.add_argument("arquivo", nargs="?", type=Path, help="Arquivo .pdf, .html ou .json")
    fontes.add_argument("--pci", action="store_true", help="Consulta o portal PCI Concursos")
    fontes.add_argument("--ache", action="store_true", help="Consulta o Ache Concursos (web + PDF scraping dos editais)")
    fontes.add_argument("--cnb", action="store_true", help="Consulta o portal Concursos no Brasil")
    fontes.add_argument("--todas", action="store_true", help="Executa coleta completa ampla (PCI + Ache + Concursos no Brasil)")
    fontes.add_argument("--periodico", action="store_true", help="Executa coleta completa periodicamente a cada 6 horas")
    fontes.add_argument("--api", help="Consulta uma API pública que retorna JSON")
    parser.add_argument("--limite", type=int, default=80, help="Quantidade máxima por fonte (padrão: 80)")
    parser.add_argument("--uf", help="Filtro opcional de UF para --pci (ex.: MG)")
    parser.add_argument("--intervalo", type=int, default=360, help="Intervalo em minutos para --periodico (padrão: 360 = 6h)")
    parser.add_argument("--sem-pdf", action="store_true", help="Não baixa nem analisa os editais em PDF")
    parser.add_argument("--chave-api", help="Campo que contém os registros, por exemplo data.items")
    parser.add_argument("--tabelas", action="store_true", help="Mostra as tabelas detectadas em um PDF")
    parser.add_argument("--url", help="Link oficial para um único registro")
    parser.add_argument("--publicar", action="store_true", help="Grava os registros validados no catálogo")
    args = parser.parse_args()

    if args.tabelas and (not args.arquivo or args.arquivo.suffix.lower() != ".pdf" or args.publicar):
        parser.error("--tabelas exige um PDF local em modo de conferência.")
    if args.intervalo < 5:
        parser.error("--intervalo deve ser de pelo menos 5 minutos.")

    if args.periodico:
        while True:
            resumo = executar_coleta_completa(
                limite_pci=max(args.limite, 100),
                limite_ache=max(args.limite, 80),
                limite_cnb=max(args.limite, 80),
                enriquecer_pci=30,
                enriquecer_ache=25,
                enriquecer_cnb=25,
                baixar_pdf=not args.sem_pdf,
                publicar=True,
            )
            print(
                f"[{resumo.get('executado_em')}] status={resumo.get('status')} "
                f"abertos={resumo.get('encontrados')} inseridos={resumo.get('inseridos')} "
                f"atualizados={resumo.get('atualizados')} pdfs={resumo.get('pdfs_processados')}",
                flush=True,
            )
            for pend in resumo.get("pendencias", []):
                print(f"Aviso: {pend}", file=sys.stderr, flush=True)
            time.sleep(args.intervalo * 60)

    try:
        if args.tabelas:
            from extracao import inspecionar_tabelas
            print(json.dumps(inspecionar_tabelas(args.arquivo), ensure_ascii=False, indent=2))
            return
        if args.todas:
            resumo = executar_coleta_completa(
                limite_pci=max(args.limite, 100),
                limite_ache=max(args.limite, 80),
                limite_cnb=max(args.limite, 80),
                enriquecer_pci=25,
                enriquecer_ache=20,
                enriquecer_cnb=20,
                baixar_pdf=not args.sem_pdf,
                publicar=args.publicar,
            )
            for pend in resumo.get("pendencias", []):
                print(f"Aviso: {pend}", file=sys.stderr)
            itens = resumo["itens"]
            if not itens:
                raise ValueError("Nenhum concurso com inscrições abertas coletado nas fontes configuradas.")
            print(json.dumps(itens, ensure_ascii=False, indent=2))
            if args.publicar:
                print(
                    f"{resumo['inseridos']} inserido(s), {resumo['atualizados']} atualizado(s), "
                    f"{resumo['pdfs_processados']} PDF(s) processado(s)."
                )
            else:
                print("Conferência concluída. Para gravar o lote revisado, use --publicar.")
            return
        if args.pci:
            from fontes.pci import coletar_pci
            registros, pendencias = coletar_pci(limite=args.limite, uf_filtro=args.uf)
            origem = "PCI Concursos"
            for pendencia in pendencias:
                print(f"Aviso: {pendencia}", file=sys.stderr)
        elif args.ache:
            from fontes.ache import coletar_ache
            registros, pendencias = coletar_ache(limite=args.limite, enriquecer_pdf=20 if not args.sem_pdf else 0)
            origem = "Ache Concursos"
            for pendencia in pendencias:
                print(f"Aviso: {pendencia}", file=sys.stderr)
            if not registros:
                raise ValueError("Nenhum edital coletado do Ache Concursos.")
        elif args.cnb:
            from fontes.cnb import coletar_cnb
            registros, pendencias = coletar_cnb(limite=args.limite, enriquecer_pdf=20 if not args.sem_pdf else 0)
            origem = "Concursos no Brasil"
            for pendencia in pendencias:
                print(f"Aviso: {pendencia}", file=sys.stderr)
            if not registros:
                raise ValueError("Nenhum edital coletado do Concursos no Brasil.")
        elif args.api:
            from fontes.api import ler_api
            registros, origem = ler_api(args.api, args.chave_api), "API"
        else:
            registros, origem = ler_fonte(args.arquivo)
        if args.url:
            if len(registros) != 1:
                raise ValueError("--url só se aplica a arquivos com um registro.")
            registros[0] = dict(registros[0], url=args.url)
        itens, inseridos, atualizados, _ = importar_lote(registros, origem, publicar=args.publicar, pular_invalidos=True)
    except (OSError, ValueError, TypeError) as exc:
        parser.exit(2, f"{exc}\n")

    print(json.dumps(itens, ensure_ascii=False, indent=2))
    if args.publicar:
        print(f"{inseridos} inserido(s), {atualizados} atualizado(s), {len(itens) - inseridos - atualizados} sem alteração.")
    else:
        print("Conferência concluída. Para gravar o lote revisado, use --publicar.")


if __name__ == "__main__":
    main()