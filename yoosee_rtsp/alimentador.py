"""Alimenta um ffmpeg com os NALs da camera e troca o ffmpeg sozinho quando precisa.

Base da gravacao (ffmpeg -> .mp4) e do servir (ffmpeg -> MediaMTX): os dois copiam o H.265
sem recodificar e sofrem os mesmos problemas quando a camera reconecta.
"""

import logging
import subprocess
import time

import imageio_ffmpeg

from yoosee_rtsp import rtsp

LOG = logging.getLogger(__name__)

# Stream parado mais que isso = a camera reconectou; os parametros do video podem ter mudado
# e o ffmpeg antigo morre com "PPS id out of range" (ja deixou uma camera 8 dias sem gravar)
PAUSA_NOVO_FFMPEG_S = 5.0

# ffmpeg que morre de novo e de novo nao pode virar uma enxurrada de processos
INTERVALO_NOVO_FFMPEG_S = 5.0

# De quanto em quanto tempo roda o `preparar` (ex.: criar a pasta de amanha)
INTERVALO_PREPARAR_S = 600.0

# Entrada comum: H.265 cru pela stdin, com timestamps tirados do relogio (o fluxo nao tem)
ENTRADA_HEVC = [
    "-hide_banner",
    "-loglevel",
    "error",
    "-use_wallclock_as_timestamps",
    "1",
    "-fflags",
    "+genpts",
    "-f",
    "hevc",
    "-i",
    "pipe:0",
    "-c",
    "copy",
]


def montar_comando(saida):
    """Devolve a linha de comando completa: ffmpeg + ENTRADA_HEVC + os argumentos de `saida`."""

    return [imageio_ffmpeg.get_ffmpeg_exe(), *ENTRADA_HEVC, *saida]


def alimentar(
    ip,
    parar,
    comando,
    arquivo_log,
    nome="camera",
    caminho=rtsp.CAMINHO_PRINCIPAL,
    ao_sumir=None,
    preparar=None,
):
    """Le a camera e escreve os NALs no ffmpeg de `comando` ate `parar` ser sinalizado.

    Bloqueia: rode numa thread por stream. `arquivo_log` recebe o stderr do ffmpeg.
    `preparar()` roda antes de cada ffmpeg novo e a cada INTERVALO_PREPARAR_S.
    `ao_sumir` vai para rtsp.gerar_nals. Chamada por gravacao.gravar e servidor.retransmitir.
    """

    estado = {
        "processo": None,
        "subiu_em": 0.0,
        "ultimo_nal": time.monotonic(),
        "preparado_em": 0.0,
    }
    ffmpeg = {
        "comando": comando,
        "log": arquivo_log,
        "nome": nome,
        "preparar": preparar,
    }

    try:
        for nal in rtsp.gerar_nals(ip, parar, caminho, ao_sumir=ao_sumir):
            agora = time.monotonic()
            if (
                estado["processo"] is not None
                and agora - estado["ultimo_nal"] > PAUSA_NOVO_FFMPEG_S
            ):
                LOG.info("%s: stream voltou, ffmpeg novo para a conexao nova", nome)
                encerrar(estado)
            estado["ultimo_nal"] = agora

            if (
                preparar is not None
                and agora - estado["preparado_em"] > INTERVALO_PREPARAR_S
            ):
                preparar()
                estado["preparado_em"] = agora

            processo = ffmpeg_pronto(estado, ffmpeg, nal)
            if processo is None:
                continue
            try:
                processo.stdin.write(nal)
            except OSError:
                LOG.warning("%s: ffmpeg caiu (codigo %s)", nome, processo.poll())
                encerrar(estado)
    finally:
        encerrar(estado)


def ffmpeg_pronto(estado, ffmpeg, nal):
    """Devolve o ffmpeg vivo, ou sobe um novo se for hora; None = descartar este NAL.

    O ffmpeg novo so comeca num VPS (inicio de GOP), senao ele recebe fatias sem os
    parametros do video. Chamada por alimentar.
    """

    processo = estado["processo"]
    if processo is not None and processo.poll() is None:
        return processo
    if processo is not None:
        LOG.warning(
            "%s: ffmpeg saiu (codigo %s), sobe outro",
            ffmpeg["nome"],
            processo.returncode,
        )
        encerrar(estado)

    agora = time.monotonic()
    if not rtsp.abre_gop(nal) or agora - estado["subiu_em"] < INTERVALO_NOVO_FFMPEG_S:
        return None

    if ffmpeg["preparar"] is not None:
        ffmpeg["preparar"]()
    estado["processo"] = iniciar(ffmpeg["comando"], ffmpeg["log"])
    estado["subiu_em"] = agora
    return estado["processo"]


def iniciar(comando, arquivo_log):
    """Sobe o ffmpeg com stdin aberta e stderr acrescentado em `arquivo_log`."""

    with open(arquivo_log, "ab") as log_erro:
        log_erro.write(
            time.strftime("\n--- ffmpeg novo %Y-%m-%d %H:%M:%S ---\n").encode()
        )
        log_erro.flush()
        # O ffmpeg herda o arquivo aberto; fechar aqui nao corta o log dele
        return subprocess.Popen(
            comando,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=log_erro,
            bufsize=0,
        )


def encerrar(estado):
    """Fecha a stdin do ffmpeg (ele finaliza a saida) e espera ele sair. Chamada por alimentar."""

    processo = estado["processo"]
    estado["processo"] = None
    if processo is None:
        return
    try:
        processo.stdin.close()
    except OSError:
        pass
    try:
        processo.wait(timeout=10)
    except subprocess.TimeoutExpired:
        processo.kill()
