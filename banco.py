import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

BASE = Path(__file__).resolve().parent


def _carregar_env():
    """Carrega variáveis de CAIO/.env sem sobrescrever o ambiente atual."""
    env_file = BASE / ".env"
    if not env_file.is_file():
        return
    for linha in env_file.read_text(encoding="utf-8").splitlines():
        linha = linha.strip()
        if not linha or linha.startswith("#") or "=" not in linha:
            continue
        chave, valor = linha.split("=", 1)
        chave = chave.strip().removeprefix("export ").strip()
        valor = valor.strip()
        if len(valor) >= 2 and valor[0] == valor[-1] and valor[0] in {"'", '"'}:
            valor = valor[1:-1]
        if chave:
            os.environ.setdefault(chave, valor)


_carregar_env()
DB = Path(os.environ.get("CAIO_DB", BASE / "caio.sqlite3"))


@contextmanager
def banco():
    db = sqlite3.connect(DB)
    db.row_factory = sqlite3.Row
    try:
        with db:
            yield db
    finally:
        db.close()


def inicializar_banco():
    with banco() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("""CREATE TABLE IF NOT EXISTS concursos (
            id INTEGER PRIMARY KEY, titulo TEXT, orgao TEXT, uf TEXT, nivel TEXT,
            prazo TEXT, url TEXT, origem TEXT, exemplo INTEGER DEFAULT 0,
            UNIQUE(titulo, orgao, uf, prazo))""")
        colunas_concursos = {c["name"] for c in db.execute("PRAGMA table_info(concursos)")}
        novas_colunas = {
            "situacao": "TEXT NOT NULL DEFAULT 'Publicado'",
            "vagas": "TEXT NOT NULL DEFAULT ''",
            "remuneracao": "TEXT NOT NULL DEFAULT ''",
            "jornada": "TEXT NOT NULL DEFAULT ''",
            "localizacao": "TEXT NOT NULL DEFAULT ''",
            "requisitos": "TEXT NOT NULL DEFAULT ''",
            "edital_pdf_url": "TEXT NOT NULL DEFAULT ''",
            "atualizado_em": "TEXT NOT NULL DEFAULT ''",
        }
        for nome_col, definicao in novas_colunas.items():
            if nome_col not in colunas_concursos:
                db.execute(f"ALTER TABLE concursos ADD COLUMN {nome_col} {definicao}")
        db.execute("""CREATE TABLE IF NOT EXISTS coletas (
            id INTEGER PRIMARY KEY,
            executado_em TEXT NOT NULL,
            origem TEXT NOT NULL,
            status TEXT NOT NULL,
            encontrados INTEGER NOT NULL DEFAULT 0,
            inseridos INTEGER NOT NULL DEFAULT 0,
            atualizados INTEGER NOT NULL DEFAULT 0,
            pdfs_processados INTEGER NOT NULL DEFAULT 0,
            detalhes TEXT NOT NULL DEFAULT '')""")
        db.execute("""CREATE TABLE IF NOT EXISTS notificacoes_enviadas (
            concurso_id INTEGER NOT NULL,
            chat_id INTEGER NOT NULL,
            enviado_em TEXT NOT NULL,
            PRIMARY KEY (concurso_id, chat_id))""")
        db.execute("""CREATE TABLE IF NOT EXISTS perfis (
            id TEXT PRIMARY KEY, nome TEXT, q TEXT, uf TEXT, nivel TEXT)""")
        if "avisos_telegram" not in {c["name"] for c in db.execute("PRAGMA table_info(perfis)")}:
            db.execute("ALTER TABLE perfis ADD COLUMN avisos_telegram INTEGER NOT NULL DEFAULT 0")
        if "telegram_chat_id" not in {c["name"] for c in db.execute("PRAGMA table_info(perfis)")}:
            db.execute("ALTER TABLE perfis ADD COLUMN telegram_chat_id INTEGER")
        db.execute("""CREATE TABLE IF NOT EXISTS vinculos_telegram (
            perfil_id TEXT PRIMARY KEY, codigo_hash TEXT NOT NULL UNIQUE, expira INTEGER NOT NULL)""")
        db.executemany("""INSERT OR IGNORE INTO concursos
            (titulo,orgao,uf,nivel,prazo,url,origem,exemplo)
            VALUES (?,?,?,?,?,'','Demonstração',1)""", [
            ("Analista de Tecnologia da Informação", "Universidade Federal do Cerrado", "MG", "Superior", "2027-02-12"),
            ("Assistente Administrativo", "Prefeitura de Vila Ipê", "MG", "Médio", "2027-01-28"),
            ("Técnico em Informática", "Instituto Federal do Horizonte", "GO", "Técnico", "2027-03-05"),
        ])
