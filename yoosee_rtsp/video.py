"""Decodifica os NALs H.265 em quadros (PyAV) e tira fotos JPEG.

Usado no ao vivo e na deteccao de movimento: o substream (onvif2, 640x360) e leve o bastante
para decodificar varias cameras num notebook.
"""

import io
from threading import Event, Timer

import av
from PIL import Image

from yoosee_rtsp import rtsp

# O decodificador reclama de cada NAL antes do primeiro VPS; isso e normal e so suja o terminal
av.logging.set_level(av.logging.PANIC)

# Desiste da foto se a camera nao entregar um quadro inteiro nesse tempo
TEMPO_LIMITE_FOTO_S = 15.0


def novo_decodificador():
    """Devolve um decodificador HEVC do PyAV; um por camera (guarda estado entre NALs)."""

    return av.CodecContext.create("hevc", "r")


def decodificar(decodificador, nal):
    """Devolve a lista de quadros (av.VideoFrame) que esse NAL completou; [] se nenhum.

    NAL corrompido ou antes do primeiro VPS e ignorado: o proximo GOP corrige sozinho.
    """

    quadros = []
    try:
        pacotes = decodificador.parse(nal)
    except av.FFmpegError:
        return quadros
    for pacote in pacotes:
        try:
            quadros.extend(decodificador.decode(pacote))
        except av.FFmpegError:
            continue
    return quadros


def gerar_quadros(ip, parar, caminho=rtsp.CAMINHO_SECUNDARIO):
    """Gera os quadros decodificados da camera ate `parar`, reconectando quando cai."""

    decodificador = novo_decodificador()
    for nal in rtsp.gerar_nals(ip, parar, caminho):
        yield from decodificar(decodificador, nal)


def para_jpeg(quadro, largura=None, qualidade=80):
    """Devolve o quadro em bytes JPEG, reduzido para `largura` se for dada."""

    if largura and quadro.width != largura:
        altura = max(1, round(quadro.height * largura / quadro.width))
        quadro = quadro.reformat(width=largura, height=altura)
    imagem = Image.fromarray(quadro.to_ndarray(format="rgb24"))
    buffer = io.BytesIO()
    imagem.save(buffer, format="JPEG", quality=qualidade)
    return buffer.getvalue()


def tirar_foto(ip, caminho=rtsp.CAMINHO_PRINCIPAL):
    """Conecta, espera o primeiro quadro inteiro e devolve em JPEG (ou None se nao vier).

    O primeiro quadro so sai depois do primeiro GOP, entao pode levar alguns segundos.
    """

    parar = Event()
    # O gerador reconecta para sempre; o Timer e o que garante a desistencia
    relogio = Timer(TEMPO_LIMITE_FOTO_S, parar.set)
    relogio.start()
    try:
        for quadro in gerar_quadros(ip, parar, caminho):
            return para_jpeg(quadro)
        return None
    finally:
        relogio.cancel()
        parar.set()
