"""Adaptação do handler /start da Aula 10 para o CAIO."""
import os

from banco import inicializar_banco
from telegram_vinculo import conectar_chat, parar_avisos


def registrar_handlers(bot):
    """Se já houver um bot do grupo, chame esta função usando aquela instância."""
    @bot.message_handler(commands=["start"], chat_types=["private"])
    def iniciar(mensagem):
        partes = mensagem.text.split(maxsplit=1)
        if len(partes) == 1:
            resposta = "Salve suas preferências no CAIO e use o botão Conectar ao Telegram."
        elif conectar_chat(partes[1], mensagem.chat.id):
            resposta = "Seu Telegram foi conectado ao CAIO. Use /parar para desativar os avisos."
        else:
            resposta = "Código inválido, expirado ou já usado. Gere um novo link na página do CAIO."
        bot.send_message(mensagem.chat.id, resposta)

    @bot.message_handler(commands=["parar"], chat_types=["private"])
    def parar(mensagem):
        parar_avisos(mensagem.chat.id)
        bot.send_message(mensagem.chat.id, "Avisos desativados e chat desvinculado.")


def main():
    import telebot

    token = os.getenv("TEL_TOKEN", "")
    if not token:
        raise SystemExit("Configure TEL_TOKEN no ambiente do bot.")
    inicializar_banco()
    bot = telebot.TeleBot(token)
    registrar_handlers(bot)
    bot.infinity_polling()


if __name__ == "__main__":
    main()
