import asyncio
import os
import secrets
import unicodedata
from contextlib import asynccontextmanager
from datetime import date, datetime
from html import escape
from pathlib import Path
from string import Template
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from banco import BASE, banco, inicializar_banco
from coleta import coleta_em_andamento, executar_coleta_completa, ultima_coleta
from extracao import extrair, sugerir, sugerir_de_pdf
from opcoes import NIVEIS, UFS
from telegram_vinculo import criar_link, nome_do_bot

BASE_HTML = Template((BASE / "templates/base.html").read_text(encoding="utf-8"))
MAX_ARQUIVO = 8 * 1024 * 1024
_TZ_SP = ZoneInfo("America/Sao_Paulo")


def _intervalo_coleta_minutos():
    try:
        return max(5, int(os.getenv("CAIO_COLETA_INTERVALO_MIN", "360")))
    except ValueError:
        return 360


def _precisa_coleta_inicial(intervalo_min):
    with banco() as db:
        tem_reais = db.execute("SELECT 1 FROM concursos WHERE exemplo = 0 LIMIT 1").fetchone()
    ultima = ultima_coleta()
    if not tem_reais or not ultima:
        return True
    try:
        dt_ultima = datetime.fromisoformat(ultima["executado_em"])
        decorrido = (datetime.now(_TZ_SP) - dt_ultima).total_seconds()
        return decorrido >= intervalo_min * 60
    except ValueError:
        return True


async def _loop_coleta_periodica():
    intervalo_min = _intervalo_coleta_minutos()
    if await run_in_threadpool(_precisa_coleta_inicial, intervalo_min):
        try:
            await run_in_threadpool(
                executar_coleta_completa,
                limite_pci=20, limite_ache=20, limite_cnb=15,
                baixar_pdf=True, publicar=True,
            )
        except Exception:
            pass
    while True:
        await asyncio.sleep(intervalo_min * 60)
        try:
            await run_in_threadpool(
                executar_coleta_completa,
                limite_pci=20, limite_ache=20, limite_cnb=15,
                baixar_pdf=True, publicar=True,
            )
        except Exception:
            pass


@asynccontextmanager
async def lifespan(app):
    inicializar_banco()
    tarefa_coleta = None
    if os.getenv("CAIO_COLETA_AUTO", "1") != "0":
        tarefa_coleta = asyncio.create_task(_loop_coleta_periodica())
    try:
        yield
    finally:
        if tarefa_coleta is not None:
            tarefa_coleta.cancel()
            try:
                await tarefa_coleta
            except asyncio.CancelledError:
                pass


app = FastAPI(title="CAIO", docs_url=None, redoc_url=None, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")


def hoje():
    return datetime.now(_TZ_SP).date().isoformat()


def e(value):
    return escape(str(value or ""), quote=True)


def normalizar(value):
    return "".join(c for c in unicodedata.normalize("NFD", value.casefold())
                   if not unicodedata.combining(c))


def pagina(title, content, active="", hero="", status=200):
    menu = [("/", "Explorar"), ("/perfil", "Receber avisos"), ("/ler-edital", "Ler edital em PDF")]
    nav = "".join(
        f'<a href="{link}"' + (' aria-current="page"' if active == link else "") + f'>{label}</a>'
        for link, label in menu
    )
    return HTMLResponse(BASE_HTML.substitute(
        title=e(title), content=content, nav=nav, hero=hero), status_code=status)


def mensagem(texto, erro=False):
    return f'<p class="notice {"error" if erro else ""}" role="{"alert" if erro else "status"}">{e(texto)}</p>'


def campo(label, name, value="", tipo="text", required=False, limit=150):
    return (f'<label>{label}<input type="{tipo}" name="{name}" value="{e(value)}" '
            f'maxlength="{limit}" {"required" if required else ""}></label>')


def selecao(label, name, options, value="", required=False, placeholder=None):
    first = placeholder or ("Selecione" if required else "Todos")
    items = f'<option value="">{first}</option>'
    items += "".join(f'<option{" selected" if item == value else ""}>{e(item)}</option>' for item in options)
    return f'<label>{label}<select name="{name}" {"required" if required else ""}>{items}</select></label>'


def filtros(q="", uf="", nivel=""):
    return (campo("Cargo ou órgão", "q", q, tipo="search") +
            selecao("Estado", "uf", UFS, uf) +
            selecao("Escolaridade", "nivel", NIVEIS, nivel))


def obter_perfil(request):
    with banco() as db:
        return db.execute("SELECT * FROM perfis WHERE id=?", (request.cookies.get("caio_perfil"),)).fetchone()


def _consultar_concursos(q="", uf="", nivel=""):
    with banco() as db:
        tem_reais = db.execute("SELECT 1 FROM concursos WHERE exemplo = 0 LIMIT 1").fetchone() is not None
        filtro_exemplo = "AND exemplo = 0" if tem_reais else ""
        rows = db.execute(
            f"SELECT * FROM concursos WHERE situacao = 'Publicado' {filtro_exemplo} ORDER BY exemplo, prazo < ?, prazo, id",
            (hoje(),),
        ).fetchall()
    terms = normalizar(q[:150]).split()
    return [
        row for row in rows
        if (not uf or row["uf"] == uf)
        and (not nivel or row["nivel"] == nivel)
        and all(term in normalizar(f"{row['titulo']} {row['orgao']} {row['requisitos']} {row['localizacao']}") for term in terms)
    ]


def resumo(row):
    prazo = date.fromisoformat(row["prazo"]).strftime("%d/%m/%Y")
    status = "Exemplo fictício" if row["exemplo"] else (
        "Prazo vigente" if row["prazo"] >= hoje() else "Prazo encerrado")
    detalhes_extra = [p for p in (row["vagas"], row["remuneracao"], row["localizacao"]) if p]
    subtitulo = " · ".join(detalhes_extra[:2])
    linha_extra = f'<p class="help">{e(subtitulo)}</p>' if subtitulo else ""
    selo_pdf = " · PDF" if row["edital_pdf_url"] else ""
    return f'''<article class="card">
        <div class="meta"><span class="chip">{status}</span><span>{e(row["uf"])} · {e(row["nivel"])}{selo_pdf}</span></div>
        <h3><a href="/concurso/{row["id"]}">{e(row["titulo"])}</a></h3>
        <p>{e(row["orgao"])}</p>{linha_extra}<div class="card-bottom">
        <span>Prazo <strong>{prazo}</strong></span><a href="/concurso/{row["id"]}" aria-label="Ver detalhes de {e(row["titulo"])}">Ver detalhes →</a>
        </div></article>'''


def _bloco_coleta(coleta_aviso=""):
    rodando = coleta_em_andamento()
    ultima = ultima_coleta()
    aviso = ""
    if coleta_aviso == "iniciada" or rodando:
        aviso = mensagem("Coleta Web + PDF em andamento em segundo plano. Atualize a página em instantes para ver novos editais.")
    elif coleta_aviso == "andamento":
        aviso = mensagem("Já existe uma coleta em andamento no momento.")

    if ultima:
        try:
            quando = datetime.fromisoformat(ultima["executado_em"]).strftime("%d/%m/%Y %H:%M")
        except ValueError:
            quando = ultima["executado_em"]
        texto_status = (
            f"Última coleta periódica ({ultima['origem']}): {quando} · "
            f"{ultima['encontrados']} encontrado(s), {ultima['pdfs_processados']} PDF(s) analisado(s)."
        )
    else:
        texto_status = "Coleta automática periódica ativada (UFU Web+PDF e PCI Concursos)."

    botao = "Coleta em andamento..." if rodando else "Atualizar catálogo agora"
    desativado = "disabled" if rodando else ""
    return f'''{aviso}<div class="panel publish"><div class="section-head">
      <p class="help">{e(texto_status)}</p>
      <form action="/coletar" method="post"><button class="btn secondary" {desativado}>{botao}</button></form>
      </div></div>'''


@app.get("/", response_class=HTMLResponse)
def inicio(request: Request, q: str = "", uf: str = "", nivel: str = "", perfil: bool = False, coleta: str = ""):
    saved = obter_perfil(request)
    if perfil and saved:
        q, uf, nivel = saved["q"], saved["uf"], saved["nivel"]
    rows = _consultar_concursos(q, uf, nivel)
    hero = '''<section class="hero"><div class="wrap hero-inner">
      <div><h1>Encontre seu próximo<br>concurso público<span>.</span></h1>
      <p>Coleta periódica de portais e editais em PDF. Pesquise por cargo, estado e escolaridade ou receba avisos no Telegram.</p>
      <a class="btn primary" href="#busca">Buscar concursos</a></div>
      <img class="hero-art" src="/static/caio-edital.webp" alt="" width="800" height="800">
      </div></section>'''
    prefer = '<a href="/?perfil=true#resultados">Usar minhas preferências</a>' if saved else '<a href="/perfil">Cadastrar para receber avisos</a>'
    cards = "".join(resumo(row) for row in rows)
    if not cards:
        cards = '<p class="panel">Nenhum concurso corresponde aos filtros. <a href="/#busca">Limpar filtros</a></p>'
    count = f'{len(rows)} ' + ("resultado" if len(rows) == 1 else "resultados")
    return pagina("Explorar concursos", f'''<section class="panel filters" id="busca">
       <div class="section-head"><h2>Buscar concursos</h2>{prefer}</div>
       <form action="/#resultados" method="get" class="search">{filtros(q, uf, nivel)}
       <button class="btn primary">Pesquisar</button></form></section>
       {_bloco_coleta(coleta)}
       <section id="resultados" aria-labelledby="h-resultados"><div class="section-head results">
       <h2 id="h-resultados">Concursos no catálogo</h2><span role="status">{count}</span></div>
       <div class="cards">{cards}</div></section>''', "/", hero)


@app.post("/coletar")
async def coletar_agora():
    if coleta_em_andamento():
        return RedirectResponse("/?coleta=andamento#resultados", status_code=303)
    asyncio.create_task(run_in_threadpool(
        executar_coleta_completa,
        limite_pci=20, limite_ache=20, limite_cnb=15,
        baixar_pdf=True, publicar=True,
    ))
    return RedirectResponse("/?coleta=iniciada#resultados", status_code=303)


@app.get("/api/concursos", response_class=JSONResponse)
def api_concursos(q: str = "", uf: str = "", nivel: str = ""):
    rows = _consultar_concursos(q, uf, nivel)
    return {"total": len(rows), "itens": [dict(row) for row in rows]}


@app.get("/concurso/{identificador}", response_class=HTMLResponse)
def detalhes(identificador: int):
    with banco() as db:
        row = db.execute("SELECT * FROM concursos WHERE id=? AND situacao = 'Publicado'", (identificador,)).fetchone()
    if row is None:
        return pagina("Concurso indisponível", '<h1>Concurso indisponível</h1><p><a href="/">Voltar para a busca</a></p>', status=404)
    titulo = e(row["titulo"])
    prazo = date.fromisoformat(row["prazo"]).strftime("%d/%m/%Y")
    campos_dl = [
        ("Estado", row["uf"]),
        ("Localização", row["localizacao"]),
        ("Escolaridade", row["nivel"]),
        ("Vagas", row["vagas"]),
        ("Remuneração", row["remuneracao"]),
        ("Jornada", row["jornada"]),
        ("Requisitos / Cargos", row["requisitos"]),
        ("Prazo informado", prazo),
        ("Fonte", row["origem"]),
    ]
    dl_html = "".join(f"<dt>{label}</dt><dd>{e(valor)}</dd>" for label, valor in campos_dl if valor)
    if row["exemplo"]:
        actions = mensagem("Este concurso é fictício e serve para testar o catálogo.")
    else:
        botoes = [f'<a class="btn primary" href="{e(row["url"])}" target="_blank" rel="noopener">Abrir fonte oficial</a>']
        if row["edital_pdf_url"]:
            botoes.append(f'<a class="btn secondary" href="{e(row["edital_pdf_url"])}" target="_blank" rel="noopener">Abrir edital em PDF</a>')
        actions = f'<div class="actions">{"".join(botoes)}</div>'
    return pagina(row["titulo"], f'''<div class="narrow"><a href="/">← Voltar para a busca</a>
       <h1>{titulo}</h1><p class="lead">{e(row["orgao"])}</p><div class="panel">
       <dl>{dl_html}</dl>
       {actions}<p class="publish"><a href="/ler-edital">Extrair informações de um PDF</a></p></div></div>''', "/")


def perfil_view(data, feedback="", status=200):
    nome = campo("Nome", "nome", data.get("nome"), required=True)
    options = filtros(data.get("q"), data.get("uf"), data.get("nivel"))
    marcado = "checked" if data.get("avisos_telegram") else ""
    bot_configurado = bool(nome_do_bot())
    conectado = bool(data.get("telegram_chat_id"))
    botao = "Salvar meu cadastro"
    ajuda = "Você pode desmarcar essa opção para desvincular o chat."
    if bot_configurado and not conectado:
        botao = ('<span class="salvar-cadastro">Salvar meu cadastro</span>'
                 '<span class="salvar-conectar">Salvar e conectar ao Telegram</span>')
        ajuda = ("Com a opção marcada, salvar abre o bot. Toque em Iniciar no Telegram "
                 "e depois volte para conferir o status.")
    if conectado:
        situacao = "Conta do Telegram conectada."
    elif not bot_configurado:
        situacao = "A conexão depende da configuração do bot."
    elif marcado:
        situacao = "Falta conectar sua conta ao bot."
    else:
        situacao = "Avisos pelo Telegram desativados."
    return pagina("Receber avisos", f'''<div class="narrow"><h1>Receber avisos</h1>
      <p class="lead">Escolha quais concursos quer acompanhar pelo Telegram.</p>{feedback}
      <form class="panel stack form-perfil" action="/perfil" method="post">{nome}
      <fieldset class="stack"><legend>Concursos de interesse</legend>
      <p class="help">Os filtros são opcionais. Deixe em branco ou escolha “Todos” para acompanhar mais concursos.</p>
      {options}</fieldset>
      <fieldset class="stack"><legend>Avisos pelo Telegram</legend>
      <label class="check"><input type="checkbox" name="avisos_telegram" value="1" {marcado}>
      Quero receber avisos de novos concursos que correspondam aos meus filtros.</label>
      <p class="help">{ajuda}</p>
      <p class="help"><strong>Status</strong> · {situacao}</p></fieldset>
      <div class="actions"><button class="btn primary">{botao}</button>
      <a href="/?perfil=true#resultados">Ver concursos do meu interesse</a></div></form></div>''', "/perfil", status=status)


@app.get("/perfil", response_class=HTMLResponse)
def perfil_get(request: Request, salvo: bool = False):
    data = obter_perfil(request)
    return perfil_view(dict(data) if data else {}, mensagem("Cadastro de avisos salvo.") if salvo else "")


@app.post("/perfil")
async def perfil_post(request: Request):
    async with request.form(max_fields=8, max_files=0) as form:
        data = {key: str(form.get(key, "")).strip() for key in ("nome", "q", "uf", "nivel")}
        data["avisos_telegram"] = int(form.get("avisos_telegram") == "1")
    if not data["nome"] or len(data["nome"]) > 150 or len(data["q"]) > 150 or (
            data["uf"] not in ["", *UFS] or data["nivel"] not in ["", *NIVEIS]):
        return perfil_view(data, mensagem("Confira o nome e as opções do perfil.", True), 422)
    saved = obter_perfil(request)
    token = saved["id"] if saved else secrets.token_urlsafe(32)
    with banco() as db:
        db.execute("""INSERT INTO perfis (id,nome,q,uf,nivel,avisos_telegram)
                   VALUES (?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                   nome=excluded.nome,q=excluded.q,uf=excluded.uf,nivel=excluded.nivel,
                   avisos_telegram=excluded.avisos_telegram""",
                   (token, data["nome"], data["q"], data["uf"], data["nivel"], data["avisos_telegram"]))
        if not data["avisos_telegram"]:
            db.execute("UPDATE perfis SET telegram_chat_id=NULL WHERE id=?", (token,))
            db.execute("DELETE FROM vinculos_telegram WHERE perfil_id=?", (token,))
    link = criar_link(token) if data["avisos_telegram"] else None
    response = RedirectResponse(link or "/perfil?salvo=true", status_code=303,
                                headers={"Cache-Control": "no-store"})
    response.set_cookie("caio_perfil", token, httponly=True, samesite="lax", max_age=31536000)
    return response


def leitura_view(data=None, feedback="", preview="", status=200):
    resumo_html = ""
    if preview:
        nomes = {
            "titulo": "Concurso",
            "orgao": "Órgão",
            "uf": "Estado",
            "localizacao": "Localização",
            "nivel": "Escolaridade",
            "vagas": "Vagas",
            "remuneracao": "Remuneração",
            "jornada": "Jornada",
            "requisitos": "Requisitos / Cargos",
            "prazo": "Prazo informado",
        }
        itens = "".join(f"<dt>{label}</dt><dd>{e(data[key])}</dd>"
                        for key, label in nomes.items() if data.get(key))
        campos = f"<dl>{itens}</dl>" if itens else "<p>Confira os dados no trecho extraído.</p>"
        resumo_html = f'''<section class="panel stack publish"><h2>Informações extraídas</h2>
        {campos}<details open><summary>Trecho do edital</summary><pre tabindex="0">{e(preview[:3500])}</pre></details>
        <p class="help">Confira as informações no documento original.</p></section>'''
    return pagina("Ler edital em PDF", f'''<div class="narrow"><h1>Ler edital em PDF</h1>
      <p class="lead">Selecione um PDF para consultar as informações extraídas. O arquivo é usado apenas nesta consulta.</p>
      {feedback}<form action="/ler-edital" method="post" enctype="multipart/form-data" class="panel stack">
      <label>Edital em PDF<input type="file" name="arquivo" accept=".pdf,application/pdf" aria-describedby="arquivo-ajuda" required></label>
      <p id="arquivo-ajuda" class="help">Até 8 MB. A leitura usa o texto e as tabelas das primeiras páginas de um PDF digital.</p>
      <div><button class="btn primary">Extrair informações</button></div></form>{resumo_html}
      </div>''', "/ler-edital", status=status)


@app.get("/ler-edital", response_class=HTMLResponse)
def ler_edital():
    return leitura_view()


async def ler_arquivo(request, tipos):
    tamanho = 0
    stream_original = request.stream
    async def stream_limitado():
        nonlocal tamanho
        async for chunk in stream_original():
            tamanho += len(chunk)
            if tamanho > MAX_ARQUIVO + 65536:
                raise HTTPException(413, "Escolha um arquivo de até 8 MB.")
            yield chunk

    request.stream = stream_limitado
    async with request.form(max_files=1, max_fields=0) as form:
        arquivo = form.get("arquivo")
        extension = Path(getattr(arquivo, "filename", "") or "").suffix.lower()
        if extension not in tipos:
            raise HTTPException(422, "Selecione um arquivo " + ("PDF ou HTML." if len(tipos) > 1 else "PDF."))
        conteudo = await arquivo.read(MAX_ARQUIVO + 1)
    if len(conteudo) > MAX_ARQUIVO:
        raise HTTPException(413, "Escolha um arquivo de até 8 MB.")
    tipo = tipos[extension]
    try:
        if tipo == "pdf":
            dados, texto = await run_in_threadpool(sugerir_de_pdf, conteudo)
        else:
            texto = await run_in_threadpool(extrair, conteudo, tipo)
            dados = sugerir(texto)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if not texto.strip():
        raise HTTPException(422, "Nenhum texto foi encontrado. Para PDF escaneado, aplique OCR antes de enviar.")
    return dados, texto, tipo


@app.post("/ler-edital", response_class=HTMLResponse)
async def ler_edital_post(request: Request):
    try:
        dados, texto, _ = await ler_arquivo(request, {".pdf": "pdf"})
    except HTTPException as exc:
        return leitura_view(feedback=mensagem(exc.detail, True), status=exc.status_code)
    return leitura_view(dados, mensagem("Extração concluída."), texto)
