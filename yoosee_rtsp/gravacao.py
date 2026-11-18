"""Gravacao continua em segmentos .mp4 pelo ffmpeg, copiando o H.265 sem recodificar.

O ffmpeg fatia por tempo e escreve uma pasta por dia; a troca do ffmpeg quando a camera
reconecta fica no alimentador.
"""

import functools
from datetime import datetime, timedelta
from pathlib import Path

from yoosee_rtsp import alimentador, rtsp


def gravar(
    ip,
    pasta,
    parar,
    nome="camera",
    duracao_segmento_s=600,
    caminho=rtsp.CAMINHO_PRINCIPAL,
    ao_sumir=None,
):
    """Grava a camera em `pasta/AAAA-MM-DD/<nome>_AAAAMMDD_HHMMSS.mp4` ate `parar` ser sinalizado.

    Bloqueia: rode numa thread por camera. `ao_sumir` vai para rtsp.gerar_nals (ex.:
    descoberta.ao_sumir_pelo_mac). Efeito colateral: cria pastas e arquivos .mp4 e um
    `<nome>_ffmpeg.log` com os erros do ffmpeg.
    """

    pasta = Path(pasta)
    garantir_pastas_do_dia(pasta)
    alimentador.alimentar(
        ip,
        parar,
        montar_comando(pasta, nome, duracao_segmento_s),
        pasta / f"{nome}_ffmpeg.log",
        nome=nome,
        caminho=caminho,
        ao_sumir=ao_sumir,
        # Gravando por dias seguidos, a pasta de amanha precisa existir antes da meia-noite
        preparar=functools.partial(garantir_pastas_do_dia, pasta),
    )


def montar_comando(pasta, nome, duracao_segmento_s):
    """Devolve a linha de comando do ffmpeg para gravar segmentos. Chamada por gravar."""

    padrao_saida = pasta / "%Y-%m-%d" / f"{nome}_%Y%m%d_%H%M%S.mp4"
    return alimentador.montar_comando(
        [
            "-f",
            "segment",
            "-segment_time",
            str(duracao_segmento_s),
            "-segment_format",
            "mp4",
            # mp4 fragmentado: o segmento atual ja toca antes de fechar (e sobrevive a queda de luz)
            "-segment_format_options",
            "movflags=+frag_keyframe+empty_moov+default_base_moof",
            "-reset_timestamps",
            "1",
            "-strftime",
            "1",
            str(padrao_saida),
        ]
    )


def garantir_pastas_do_dia(pasta):
    """Cria a pasta de hoje e a de amanha: o ffmpeg nao cria pasta ao virar o dia."""

    hoje = datetime.now()
    for dia in (hoje, hoje + timedelta(days=1)):
        (pasta / f"{dia:%Y-%m-%d}").mkdir(parents=True, exist_ok=True)
