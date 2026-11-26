"""Adaptação da requisição GET e da leitura de JSON do material de API."""
import json
from urllib.parse import urlsplit

from fontes.http import baixar


def ler_api(url, chave=None):
    if urlsplit(url).scheme not in {"https", "http"}:
        raise ValueError("Informe uma URL de API em HTTP ou HTTPS.")
    dados = json.loads(baixar(url, "application/json"))
    if chave:
        for parte in chave.split("."):
            if not isinstance(dados, dict) or parte not in dados:
                raise ValueError(f"Chave ausente na resposta da API: {chave}")
            dados = dados[parte]
    # Caso os nomes sejam diferentes, mapeie cada item para titulo, orgao,
    # uf, nivel, prazo e url aqui. Se houver paginação, percorra os lotes
    # conforme o contrato da fonte e passe os registros ao catálogo.
    return dados if isinstance(dados, list) else [dados]
