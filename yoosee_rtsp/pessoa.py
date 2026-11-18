"""Filtro de pessoa com YOLOX-nano (ONNX, CPU), para nao avisar por gato, sombra ou farol.

So roda quando ja ha movimento. Sem o onnxruntime ou sem o modelo, o filtro fica desligado e
todo movimento conta como alerta.
"""

import logging
from pathlib import Path
from threading import Lock

import numpy as np

LOG = logging.getLogger(__name__)

# Modelo oficial do YOLOX (Apache 2.0); nao vai no repositorio, ver README
MODELO_PADRAO = Path(__file__).resolve().parent.parent / "modelos" / "yolox_nano.onnx"

# Entrada quadrada do YOLOX-nano
LADO = 416

# Cinza das bordas do letterbox, o mesmo do treino do YOLOX
PREENCHIMENTO = 114.0

# Confianca minima (objeto x classe pessoa): abaixo disso sobra falso positivo de noite
LIMIAR = 0.4

# Grades do YOLOX (52x52, 26x26, 13x13 = 3549 saidas)
PASSOS = (8, 16, 32)

# Sessao do onnxruntime (None = filtro desligado); a trava porque varias cameras dividem uma
SESSAO = {"sessao": None}
TRAVA = Lock()


def carregar(caminho=MODELO_PADRAO):
    """Carrega o modelo e devolve se o filtro ficou ativo. Chamar uma vez na subida."""

    try:
        import onnxruntime

        SESSAO["sessao"] = onnxruntime.InferenceSession(
            str(caminho), providers=["CPUExecutionProvider"]
        )
    except (
        Exception
    ) as erro:  # noqa: BLE001 - sem modelo ou sem lib o filtro so nao existe
        SESSAO["sessao"] = None
        LOG.info("filtro de pessoa desligado (%s)", type(erro).__name__)
    return SESSAO["sessao"] is not None


def ativo():
    """Diz se o modelo esta carregado."""

    return SESSAO["sessao"] is not None


def montar_grade():
    """Devolve (gx, gy, passo) de cada uma das 3549 saidas do YOLOX. Calculada uma vez."""

    colunas, linhas, passos = [], [], []
    for passo in PASSOS:
        lado = LADO // passo
        gy, gx = np.meshgrid(np.arange(lado), np.arange(lado), indexing="ij")
        colunas.append(gx.ravel())
        linhas.append(gy.ravel())
        passos.append(np.full(lado * lado, passo))
    return np.concatenate(colunas), np.concatenate(linhas), np.concatenate(passos)


GRADE_X, GRADE_Y, GRADE_PASSO = montar_grade()


def preparar_entrada(rgb):
    """Letterbox LADO x LADO em BGR float32, formato NCHW. Devolve (entrada, escala)."""

    altura, largura = rgb.shape[:2]
    escala = LADO / max(largura, altura)
    nova_largura = max(1, round(largura * escala))
    nova_altura = max(1, round(altura * escala))
    # Vizinho mais proximo em numpy puro: sem OpenCV so para redimensionar
    linhas = (np.arange(nova_altura) / escala).astype(int).clip(0, altura - 1)
    colunas = (np.arange(nova_largura) / escala).astype(int).clip(0, largura - 1)
    reduzido = rgb[linhas][:, colunas]

    tela = np.full((LADO, LADO, 3), PREENCHIMENTO, dtype=np.float32)
    tela[:nova_altura, :nova_largura] = reduzido[:, :, ::-1]
    return tela.transpose(2, 0, 1)[None], escala


def achar_pessoas(rgb):
    """Devolve as pessoas na imagem RGB (numpy HxWx3) acima do LIMIAR.

    Cada uma e (esquerda, topo, direita, base, confianca) em fracao 0..1 da imagem. Sem
    modelo carregado devolve [].
    """

    sessao = SESSAO["sessao"]
    if sessao is None:
        return []

    entrada, escala = preparar_entrada(rgb)
    with TRAVA:
        saida = sessao.run(None, {sessao.get_inputs()[0].name: entrada})[0][0]
    return decodificar_saida(saida, escala, rgb.shape[1], rgb.shape[0])


def decodificar_saida(saida, escala, largura, altura):
    """Transforma a saida em grade do YOLOX em caixas de pessoa. Chamada por achar_pessoas.

    O modelo exportado nao decodifica: centro = (saida + celula) * passo, lado = exp * passo.
    """

    confianca = saida[:, 4] * saida[:, 5]  # objeto x classe 0 (pessoa no COCO)
    caixas = []
    for i in np.nonzero(confianca >= LIMIAR)[0]:
        centro_x = (saida[i, 0] + GRADE_X[i]) * GRADE_PASSO[i]
        centro_y = (saida[i, 1] + GRADE_Y[i]) * GRADE_PASSO[i]
        meia_largura = np.exp(saida[i, 2]) * GRADE_PASSO[i] / 2
        meia_altura = np.exp(saida[i, 3]) * GRADE_PASSO[i] / 2
        caixas.append(
            (
                float((centro_x - meia_largura) / escala / largura),
                float((centro_y - meia_altura) / escala / altura),
                float((centro_x + meia_largura) / escala / largura),
                float((centro_y + meia_altura) / escala / altura),
                float(confianca[i]),
            )
        )
    return caixas


def pes_na_area(caixa, area):
    """Diz se os pes (meio da base da caixa) estao dentro da area.

    Pes e nao o corpo: pela perspectiva, quem passa na rua fica com a cabeca "dentro" da area
    da garagem, mas os pes continuam na rua.
    """

    esquerda, topo, direita, base = area
    pe_x, pe_y = (caixa[0] + caixa[2]) / 2, caixa[3]
    return esquerda <= pe_x <= direita and topo <= pe_y <= base


def tem_pessoa(quadro, area=None):
    """Diz se o quadro (av.VideoFrame) tem pessoa (com `area`: com os pes dentro dela).

    Sem modelo carregado devolve True: o filtro desligado nao pode esconder alerta.
    """

    if not ativo():
        return True
    # Reduz pelo swscale (bilinear) antes: fica melhor que o vizinho mais proximo do numpy
    escala = LADO / max(quadro.width, quadro.height)
    reduzido = quadro.reformat(
        width=max(1, round(quadro.width * escala)),
        height=max(1, round(quadro.height * escala)),
        format="rgb24",
    )
    caixas = achar_pessoas(reduzido.to_ndarray())
    return any(area is None or pes_na_area(caixa, area) for caixa in caixas)
