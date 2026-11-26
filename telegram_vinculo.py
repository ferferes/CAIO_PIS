"""Vincula o cadastro local ao chat privado iniciado pelo candidato."""
import hashlib
import os
import re
import secrets
import time

from banco import banco


def nome_do_bot():
    nome = os.getenv("CAIO_BOT_USERNAME", "").strip().removeprefix("@")
    return nome if re.fullmatch(r"[A-Za-z0-9_]{5,32}", nome) else ""


def _hash(codigo):
    return hashlib.sha256(codigo.encode()).hexdigest()


def criar_link(perfil_id):
    nome = nome_do_bot()
    if not nome:
        return None
    codigo = secrets.token_urlsafe(24)
    agora = int(time.time())
    with banco() as db:
        db.execute("BEGIN IMMEDIATE")
        perfil = db.execute("""SELECT 1 FROM perfis WHERE id=? AND avisos_telegram=1
                              AND telegram_chat_id IS NULL""", (perfil_id,)).fetchone()
        if perfil is None:
            return None
        db.execute("DELETE FROM vinculos_telegram WHERE expira<=?", (agora,))
        db.execute("""INSERT INTO vinculos_telegram VALUES (?,?,?) ON CONFLICT(perfil_id)
                      DO UPDATE SET codigo_hash=excluded.codigo_hash, expira=excluded.expira""",
                   (perfil_id, _hash(codigo), agora + 600))
    return f"https://t.me/{nome}?start={codigo}"


def conectar_chat(codigo, chat_id):
    if not re.fullmatch(r"[A-Za-z0-9_-]{32}", codigo) or not isinstance(chat_id, int) or chat_id <= 0:
        return False
    with banco() as db:
        db.execute("BEGIN IMMEDIATE")
        vinculo = db.execute("SELECT perfil_id FROM vinculos_telegram WHERE codigo_hash=? AND expira>?",
                            (_hash(codigo), int(time.time()))).fetchone()
        if not vinculo:
            return False
        perfil_id = vinculo["perfil_id"]
        db.execute("DELETE FROM vinculos_telegram WHERE perfil_id=?", (perfil_id,))
        salvo = db.execute("""UPDATE perfis SET telegram_chat_id=? WHERE id=?
                              AND avisos_telegram=1 AND telegram_chat_id IS NULL""", (chat_id, perfil_id))
        return salvo.rowcount == 1


def parar_avisos(chat_id):
    with banco() as db:
        db.execute("""UPDATE perfis SET avisos_telegram=0, telegram_chat_id=NULL
                      WHERE telegram_chat_id=?""", (chat_id,))
