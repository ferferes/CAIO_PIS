"""Leitura HTTP para comandos e coleta automatizada."""
from urllib.request import Request, urlopen

AGENTE = "CAIO-Coleta/1.0"
LIMITE = 5 * 1024 * 1024
LIMITE_PDF = 10 * 1024 * 1024


def baixar(url, accept, limite=LIMITE, timeout=25):
    pedido = Request(
        url,
        headers={
            "User-Agent": AGENTE,
            "Accept": accept,
            "Accept-Language": "pt-BR,pt;q=0.9",
        },
    )
    with urlopen(pedido, timeout=timeout) as resposta:
        dados = resposta.read(limite + 1)
    if len(dados) > limite:
        raise ValueError(f"A fonte retornou mais de {limite // (1024 * 1024)} MB. Use filtros ou paginação.")
    return dados
