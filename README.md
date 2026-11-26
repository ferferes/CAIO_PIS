# CAIO · Concursos públicos

Sistema web acadêmico em Python para coleta, padronização, centralização e notificação personalizada de concursos públicos brasileiros. O catálogo inicial começa com três concursos fictícios de demonstração. A consulta pública de PDF serve para leitura e não adiciona registros ao catálogo.

## Rodar no VS Code

Abra a pasta `CAIO`, que contém `app.py`, e use o terminal integrado.

**macOS / Linux**

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m pip install -r requirements-bot.txt
python -m uvicorn app:app --reload
```

**Windows · PowerShell**

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m pip install -r requirements-bot.txt
python -m uvicorn app:app --reload
```

Abra [http://127.0.0.1:8000](http://127.0.0.1:8000). Use **Explorar** para consultar o catálogo, **Receber avisos** para salvar preferências ou **Ler edital em PDF** para experimentar com `exemplos/edital.pdf`. O leitor aceita PDF digital de até 8 MB e consulta o texto das primeiras 20 páginas. PDFs escaneados requerem OCR externo.

## Organização dos arquivos

| Arquivo | Responsabilidade |
|---|---|
| `app.py` | Rotas públicas, busca, cadastro, agendamento da coleta e leitura de PDF |
| `banco.py` | Conexão SQLite, tabelas, controle de notificações e atualização de bases |
| `opcoes.py` | UFs e níveis de escolaridade usados pelo site e pela coleta |
| `extracao.py` | Extração de texto e tabelas de HTML e PDF (`pdfplumber` + Regex) |
| `coleta.py` | Pipeline ETL, deduplicação cruzada entre portais e disparo de alertas |
| `fontes/pci.py` | Web scraping do portal **PCI Concursos** |
| `fontes/ache.py` | Web scraping e PDF scraping de editais do **Ache Concursos** |
| `fontes/cnb.py` | Web scraping e PDF scraping de editais do **Concursos no Brasil** |
| `fontes/api.py` e `fontes/http.py` | Leitura de API JSON e requisições HTTP |
| `telegram_vinculo.py` | Geração de link e associação do chat ao perfil |
| `bot_telegram.py` | Handlers `/start` e `/parar` para vinculação do chat |
| `templates/`, `static/` | Layout, estilo e imagens |
| `exemplos/` | HTML e PDF fictícios para demonstração |

## Conectar o Telegram e ativar alertas

1. Configure `CAIO_BOT_USERNAME`, `CAIO_NOTIFICAR_TELEGRAM` e `TEL_TOKEN` no terminal do **site** antes de iniciar o Uvicorn. Use o nome público do bot criado no BotFather, sem o `@`. No PowerShell, por exemplo:
   ```powershell
   $env:CAIO_BOT_USERNAME = "CAIOSUPbot"
   $env:CAIO_NOTIFICAR_TELEGRAM = "1"
   $env:TEL_TOKEN = "SEU_TOKEN_DO_BOTFATHER"
   ```
2. Em outro terminal (com o ambiente virtual ativo), configure `TEL_TOKEN` e inicie o bot:
   ```powershell
   $env:TEL_TOKEN = "SEU_TOKEN_DO_BOTFATHER"
   python bot_telegram.py
   ```
3. No site, em **Receber avisos**, preencha seus filtros (cargo/órgão, estado e escolaridade), marque a opção de avisos e clique em **Salvar e conectar ao Telegram**. O CAIO grava o cadastro e abre o bot. Toque em **Iniciar** no chat e volte ao site para conferir o status. O link expira em 10 minutos, funciona uma vez e pode ser reemitido.
4. Para desvincular, desmarque a opção no site e salve ou execute `/parar` no bot.

O site, a coleta e o bot usam o mesmo `caio.sqlite3` por padrão. Se configurar `CAIO_DB`, informe o mesmo caminho absoluto nos processos. Sempre que novos concursos forem publicados, o sistema consulta os perfis com `avisos_telegram=1` e `telegram_chat_id` preenchido, cruza os filtros e envia uma mensagem formatada com órgão, vagas, remuneração, prazo, página e link do edital, registrando o envio em `notificacoes_enviadas` para evitar duplicatas.

## Coleta de dados (Web Scraping, PDF Scraping e API)

### Coleta automática e unificada

Ao iniciar o servidor web (`uvicorn app:app`), o CAIO executa em segundo plano uma coleta periódica automática (padrão a cada 360 minutos, configurável por `CAIO_COLETA_INTERVALO_MIN`, ou desativável com `CAIO_COLETA_AUTO=0`) e também disponibiliza o botão **Atualizar catálogo agora** na página inicial e o endpoint `GET /api/concursos`.

No terminal, é possível executar cada coletor ou a coleta unificada:

```bash
python coleta.py --todas --publicar
python coleta.py --pci --limite 20 --publicar
python coleta.py --ache --limite 20 --publicar
python coleta.py --cnb --limite 15 --publicar
python coleta.py --periodico --intervalo 360
```

Nos coletores **Ache Concursos** (`fontes/ache.py`) e **Concursos no Brasil** (`fontes/cnb.py`), além do web scraping das páginas de resumo, o sistema localiza os links dos editais oficiais em `.pdf`, baixa os arquivos e executa o **PDF scraping** com `pdfplumber` (tabelas e texto) para extrair vagas, remuneração (incluindo valores abreviados como *"R\$ 5,5 mil"*), jornada, localização, escolaridade e requisitos. No **PCI Concursos** (`fontes/pci.py`), o crawler consulta páginas permitidas pelo `robots.txt` para trazer concursos nacionais e estaduais. Durante a importação, o `coleta.py` realiza deduplicação cruzada entre portais (por URL, link do PDF ou nome normalizado do órgão + UF + prazo) para evitar duplicatas no catálogo.

### Conferência local e PDF scraping

Na pasta `CAIO`, rode os exemplos em modo de conferência (sem gravar no banco):

```bash
python coleta.py exemplos/concurso.html
python coleta.py exemplos/edital.pdf
python coleta.py "caminho/para/edital.pdf" --tabelas
```

A opção `--tabelas` mostra as tabelas detectadas nas primeiras 20 páginas e indica de qual página veio cada registro. Para importar dados reais revisados em JSON, utilize o formato abaixo e execute `python coleta.py concursos.json --publicar`:

```json
{
  "titulo": "Analista de Dados",
  "orgao": "Órgão responsável",
  "uf": "MG",
  "nivel": "Superior",
  "prazo": "2027-02-20",
  "url": "https://example.org/edital"
}
```

### API

O CAIO também permite importar registros de APIs públicas que retornem JSON:

```bash
python coleta.py --api "https://example.org/api/concursos" --chave-api data.items --publicar
```

Use `--chave-api` se a lista vier dentro de uma chave ou caminho, como `data.items`. Para uma lista na raiz, omita a opção. A resposta precisa fornecer os seis campos obrigatórios do catálogo (`titulo`, `orgao`, `uf`, `nivel`, `prazo`, `url`). Em `fontes/api.py`, está indicado onde adaptar mapeamento de campos, autenticação e paginação conforme a fonte escolhida.