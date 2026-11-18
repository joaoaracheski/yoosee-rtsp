"""Republica as cameras como RTSP padrao num MediaMTX, para VLC, Home Assistant e Frigate.

A Yoosee so fala com este cliente; o ffmpeg pega os NALs ja consertados e publica no
MediaMTX, que entrega um RTSP normal (e WebRTC/HLS) para quantos clientes quiserem.
"""

import logging
from pathlib import Path
from threading import Thread

from yoosee_rtsp import alimentador, rtsp

LOG = logging.getLogger(__name__)

# Endereco padrao do MediaMTX rodando no mesmo computador
DESTINO_PADRAO = "rtsp://127.0.0.1:8554"

# Sufixo do caminho do substream no MediaMTX (garagem -> garagem_sub)
SUFIXO_SUB = "_sub"


def servir(
    cameras, parar, destino=DESTINO_PADRAO, com_sub=False, pasta_logs=".", ao_sumir=None
):
    """Publica cada camera em `<destino>/<nome>` (e `<nome>_sub` com `com_sub`) ate `parar`.

    `cameras` e uma lista de dicts {"nome", "ip"}. Sobe uma thread por stream e bloqueia ate
    todas terminarem. `ao_sumir(ip)` e repassado a cada stream. Efeito colateral: grava
    `<nome>_servir.log` com os erros do ffmpeg em `pasta_logs`.
    """

    threads = []
    for camera in cameras:
        streams = [(camera["nome"], rtsp.CAMINHO_PRINCIPAL)]
        if com_sub:
            streams.append((camera["nome"] + SUFIXO_SUB, rtsp.CAMINHO_SECUNDARIO))
        for nome, caminho in streams:
            thread = Thread(
                target=retransmitir,
                args=(camera["ip"], f"{destino.rstrip('/')}/{nome}", parar),
                kwargs={
                    "nome": nome,
                    "caminho": caminho,
                    "pasta_logs": pasta_logs,
                    "ao_sumir": ao_sumir,
                },
                name=f"servir-{nome}",
                daemon=True,
            )
            thread.start()
            threads.append(thread)
            LOG.info("%s (%s/%s) -> %s/%s", nome, camera["ip"], caminho, destino, nome)

    # join com tempo limite: no Windows um join sem limite nao deixa o Ctrl+C chegar
    for thread in threads:
        while thread.is_alive():
            thread.join(timeout=1.0)


def retransmitir(
    ip,
    url_destino,
    parar,
    nome="camera",
    caminho=rtsp.CAMINHO_PRINCIPAL,
    pasta_logs=".",
    ao_sumir=None,
):
    """Publica um stream da camera em `url_destino` ate `parar`. Chamada por servir.

    Se o MediaMTX cair, o ffmpeg morre e o alimentador sobe outro no proximo GOP.
    """

    try:
        alimentador.alimentar(
            ip,
            parar,
            montar_comando(url_destino),
            Path(pasta_logs) / f"{nome}_servir.log",
            nome=nome,
            caminho=caminho,
            ao_sumir=ao_sumir,
        )
    except Exception:  # noqa: BLE001 - uma camera com problema nao derruba as outras
        LOG.exception("%s: retransmissao parou", nome)


def montar_comando(url_destino):
    """Devolve o ffmpeg que publica o H.265 copiado no MediaMTX por RTSP sobre TCP."""

    return alimentador.montar_comando(
        ["-f", "rtsp", "-rtsp_transport", "tcp", url_destino]
    )


def ler_camera(texto, indice):
    """Converte "garagem=192.168.0.20" (ou so o IP) em {"nome", "ip"}.

    Sem nome, usa "camera<indice>". Levanta ValueError se o nome tiver caractere que nao
    serve em URL. Chamada pela linha de comando.
    """

    nome, separador, ip = texto.partition("=")
    if not separador:
        nome, ip = f"camera{indice}", texto
    if not nome or not all(c.isalnum() or c in "-_" for c in nome):
        raise ValueError(f"nome invalido: {nome!r} (use letras, numeros, - ou _)")
    return {"nome": nome, "ip": ip.strip()}
