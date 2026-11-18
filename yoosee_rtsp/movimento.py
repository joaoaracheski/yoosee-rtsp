"""Deteccao de movimento por diferenca de quadros, sem OpenCV e sem nuvem.

Cada quadro vira cinza 160x90 e e comparado ao anterior. Movimento sustentado abre um evento;
silencio por alguns segundos fecha. Um episodio = um aviso (anti-spam embutido).
"""

import time

import numpy as np

# Quadro reduzido: pequeno o bastante para varias cameras num notebook, grande para ver pessoa
LARGURA = 160
ALTURA = 90

# Diferenca (0-255) para um pixel contar como "mudou": abaixo disso e ruido do sensor
LIMIAR_PIXEL = 25

# Fracao minima de pixels mudados na sensibilidade 5 (padrao)
FRACAO_MIN = 0.004

# Acima disso e troca IR/cor, luz acendendo ou defeito do decodificador, nao movimento
FRACAO_MAX = 0.5

# Salto no brilho medio que indica troca de modo noturno: nao conta como movimento
SALTO_BRILHO = 12

# 4 amostras por segundo bastam para pessoa andando e poupam CPU
INTERVALO_S = 0.25

# Primeiras amostras ignoradas: o decodificador ainda entrega quadros instaveis
AQUECIMENTO = 12

# Pontos (+1 com movimento, -1 sem) para abrir: um quadro isolado nao vira alerta
PONTOS_PARA_ABRIR = 3

# Sem movimento por esse tempo fecha o evento
SILENCIO_S = 12.0


def novo_detector(area=(0.0, 0.0, 1.0, 1.0), sensibilidade=5):
    """Devolve o estado de um detector (um por camera).

    `area` = (esquerda, topo, direita, base) em fracao 0..1: so olha ali (ignora a rua, a
    arvore). `sensibilidade` vai de 1 (pouco sensivel) a 10 (muito).
    """

    return {
        "area": tuple(area),
        "minimo": fracao_minima(sensibilidade),
        "anterior": None,
        "ultima": 0.0,
        "amostras": 0,
        "pontos": 0,
        "aberto": False,
        "ultimo_movimento": 0.0,
        "pico": 0.0,
    }


def fracao_minima(sensibilidade):
    """Fracao minima de pixels mudados para a sensibilidade 1..10 (+1 nivel = 1,41x mais sensivel)."""

    return FRACAO_MIN * 2 ** ((5 - sensibilidade) / 2)


def analisar(detector, quadro, agora=None):
    """Processa um quadro (av.VideoFrame) e devolve "abriu", "fechou" ou None.

    Pode receber todos os quadros: so analisa um a cada INTERVALO_S. `agora` (monotonic)
    existe para os testes. Altera o `detector`.
    """

    cinza = quadro.reformat(width=LARGURA, height=ALTURA, format="gray").to_ndarray()
    return analisar_cinza(detector, cinza, agora)


def analisar_cinza(detector, cinza, agora=None):
    """Mesmo que analisar, recebendo o quadro ja em cinza ALTURA x LARGURA (numpy uint8)."""

    agora = time.monotonic() if agora is None else agora
    if agora - detector["ultima"] < INTERVALO_S:
        return None
    detector["ultima"] = agora

    atual = recortar(cinza, detector["area"])
    anterior, detector["anterior"] = detector["anterior"], atual
    detector["amostras"] += 1
    if anterior is None or detector["amostras"] <= AQUECIMENTO:
        return None

    fracao = fracao_mudada(anterior, atual)
    if detector["minimo"] <= fracao <= FRACAO_MAX:
        detector["pontos"] = min(detector["pontos"] + 1, PONTOS_PARA_ABRIR + 4)
        detector["ultimo_movimento"] = agora
        detector["pico"] = max(detector["pico"], fracao)
        if not detector["aberto"] and detector["pontos"] >= PONTOS_PARA_ABRIR:
            detector["aberto"] = True
            return "abriu"
        return None

    detector["pontos"] = max(detector["pontos"] - 1, 0)
    if detector["aberto"] and agora - detector["ultimo_movimento"] >= SILENCIO_S:
        detector["aberto"] = False
        detector["pico"] = 0.0
        return "fechou"
    return None


def recortar(cinza, area):
    """Recorta o quadro cinza na area (esquerda, topo, direita, base) em fracao 0..1."""

    esquerda, topo, direita, base = area
    altura, largura = cinza.shape
    coluna_a = round(esquerda * largura)
    coluna_b = max(round(direita * largura), coluna_a + 1)
    linha_a = round(topo * altura)
    linha_b = max(round(base * altura), linha_a + 1)
    return cinza[linha_a:linha_b, coluna_a:coluna_b]


def fracao_mudada(anterior, atual):
    """Fracao de pixels que mudaram entre dois quadros cinza; 0.0 em salto de brilho global."""

    if abs(float(atual.mean()) - float(anterior.mean())) > SALTO_BRILHO:
        return 0.0
    diferenca = np.abs(atual.astype(np.int16) - anterior.astype(np.int16))
    return float((diferenca > LIMIAR_PIXEL).mean())
