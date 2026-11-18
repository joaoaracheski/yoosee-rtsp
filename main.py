"""Linha de comando: achar cameras Yoosee, tirar foto, gravar, vigiar movimento e republicar."""

import argparse
import functools
import logging
import signal
import sys
import time
from datetime import datetime
from pathlib import Path
from threading import Event

from yoosee_rtsp import descoberta, gravacao, movimento, pessoa, rtsp, servidor, video

# Pasta do projeto: os caminhos padrao sao relativos a ela, de onde quer que o comando rode
PASTA_PROJETO = Path(__file__).parent

# Intervalo entre consultas ao detector de pessoa enquanto o movimento dura
INTERVALO_PESSOA_S = 1.0


def main():
    """Le os argumentos e chama o comando escolhido. Sai com codigo 1 se algo falhar."""

    argumentos = montar_parser().parse_args()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )

    parar = Event()
    # Ctrl+C encerra com calma: o ffmpeg fecha o mp4 em vez de deixar o arquivo cortado
    signal.signal(signal.SIGINT, functools.partial(pedir_parada, parar))

    argumentos.funcao(argumentos, parar)


def montar_parser():
    """Monta o argparse com os subcomandos. Chamada por main."""

    parser = argparse.ArgumentParser(
        prog="yoosee-rtsp", description="Cameras Yoosee sem o app do fabricante."
    )
    comandos = parser.add_subparsers(required=True, metavar="comando")

    achar = comandos.add_parser("descobrir", help="lista as cameras Yoosee da rede")
    achar.add_argument(
        "--rede", help='prefixo /24, ex.: "192.168.0" (padrao: a deste PC)'
    )
    achar.set_defaults(funcao=comando_descobrir)

    foto = comandos.add_parser("foto", help="salva um JPEG da camera")
    foto.add_argument("ip")
    foto.add_argument("-o", "--saida", default="foto.jpg")
    foto.add_argument("--leve", action="store_true", help="usa o substream 640x360")
    foto.set_defaults(funcao=comando_foto)

    gravar = comandos.add_parser("gravar", help="grava em segmentos .mp4 ate Ctrl+C")
    gravar.add_argument("ip")
    gravar.add_argument("--pasta", default=str(PASTA_PROJETO / "gravacoes"))
    gravar.add_argument("--nome", default="camera")
    gravar.add_argument(
        "--segmento", type=int, default=600, help="segundos por arquivo"
    )
    gravar.set_defaults(funcao=comando_gravar)

    vigiar = comandos.add_parser("movimento", help="avisa movimento e salva a foto")
    vigiar.add_argument("ip")
    vigiar.add_argument("--pasta", default=str(PASTA_PROJETO / "alertas"))
    vigiar.add_argument(
        "--area",
        type=ler_area,
        default=(0.0, 0.0, 1.0, 1.0),
        help="esquerda,topo,direita,base em 0..1 (ex.: 0,0.4,1,1)",
    )
    vigiar.add_argument("--sensibilidade", type=int, default=5, choices=range(1, 11))
    vigiar.add_argument(
        "--pessoa", action="store_true", help="so avisa se o YOLOX achar uma pessoa"
    )
    vigiar.set_defaults(funcao=comando_movimento)

    servir = comandos.add_parser(
        "servir", help="republica as cameras como RTSP padrao (MediaMTX)"
    )
    servir.add_argument(
        "cameras", nargs="+", metavar="nome=ip", help="ex.: garagem=192.168.0.20"
    )
    servir.add_argument(
        "--destino",
        default=servidor.DESTINO_PADRAO,
        help=f"endereco do MediaMTX (padrao: {servidor.DESTINO_PADRAO})",
    )
    servir.add_argument(
        "--sub", action="store_true", help="publica tambem o substream (<nome>_sub)"
    )
    servir.add_argument("--logs", default=str(PASTA_PROJETO / "logs"))
    servir.set_defaults(funcao=comando_servir)

    return parser


def ler_area(texto):
    """Converte "0,0.4,1,1" na tupla da area; erro de argparse se nao forem 4 numeros 0..1."""

    try:
        area = tuple(float(parte) for parte in texto.split(","))
    except ValueError:
        area = ()
    if len(area) != 4 or not all(0.0 <= valor <= 1.0 for valor in area):
        raise argparse.ArgumentTypeError(
            "use 4 numeros entre 0 e 1: esquerda,topo,direita,base"
        )
    return area


def pedir_parada(parar, *_):
    """Handler do Ctrl+C: sinaliza o Event que todos os lacos respeitam."""

    print("\nEncerrando...")
    parar.set()


def comando_descobrir(argumentos, parar):
    """Varre a rede e imprime IP, MAC e servidor de cada camera achada."""

    rede = argumentos.rede or descoberta.rede_local()
    print(f"Procurando cameras em {rede}.0/24 ...")
    cameras = descoberta.descobrir(rede)
    if not cameras:
        print("Nenhuma camera Yoosee encontrada.")
        return
    for camera in cameras:
        print(
            f"{camera['ip']:<16} {camera['mac'] or '(MAC desconhecido)':<18} {camera['servidor']}"
        )


def comando_foto(argumentos, parar):
    """Salva um JPEG da camera no caminho pedido."""

    caminho = rtsp.CAMINHO_SECUNDARIO if argumentos.leve else rtsp.CAMINHO_PRINCIPAL
    jpeg = video.tirar_foto(argumentos.ip, caminho)
    if jpeg is None:
        sys.exit(f"A camera {argumentos.ip} nao mandou imagem.")
    Path(argumentos.saida).write_bytes(jpeg)
    print(f"Foto salva em {argumentos.saida}")


def comando_gravar(argumentos, parar):
    """Grava ate o Ctrl+C. Se a camera trocar de IP, procura ela pelo MAC."""

    macs = descoberta.anotar_macs([argumentos.ip])
    ao_sumir = functools.partial(descoberta.ao_sumir_pela_tabela, macs)

    print(f"Gravando {argumentos.ip} em {argumentos.pasta} (Ctrl+C para parar)")
    gravacao.gravar(
        argumentos.ip,
        argumentos.pasta,
        parar,
        nome=argumentos.nome,
        duracao_segmento_s=argumentos.segmento,
        ao_sumir=ao_sumir,
    )


def comando_servir(argumentos, parar):
    """Publica as cameras no MediaMTX ate o Ctrl+C e mostra os enderecos para usar."""

    try:
        cameras = [
            servidor.ler_camera(texto, indice)
            for indice, texto in enumerate(argumentos.cameras, start=1)
        ]
    except ValueError as erro:
        sys.exit(str(erro))

    macs = descoberta.anotar_macs([camera["ip"] for camera in cameras])
    ao_sumir = functools.partial(descoberta.ao_sumir_pela_tabela, macs)
    pasta_logs = Path(argumentos.logs)
    pasta_logs.mkdir(parents=True, exist_ok=True)

    host = argumentos.destino.split("://", 1)[-1].split(":", 1)[0]
    if host in ("127.0.0.1", "localhost"):
        host = "<ip-deste-pc>"
    print("Enderecos para VLC / Home Assistant / Frigate:")
    for camera in cameras:
        print(f"  rtsp://{host}:8554/{camera['nome']}")
        if argumentos.sub:
            print(f"  rtsp://{host}:8554/{camera['nome']}{servidor.SUFIXO_SUB}")
    print("Ctrl+C para parar")

    servidor.servir(
        cameras,
        parar,
        destino=argumentos.destino,
        com_sub=argumentos.sub,
        pasta_logs=pasta_logs,
        ao_sumir=ao_sumir,
    )


def comando_movimento(argumentos, parar):
    """Vigia o substream e salva um JPEG por evento (com --pessoa, so quando ha pessoa)."""

    if argumentos.pessoa and not pessoa.carregar():
        sys.exit(f"Modelo nao encontrado em {pessoa.MODELO_PADRAO} (ver README).")

    pasta = Path(argumentos.pasta)
    pasta.mkdir(parents=True, exist_ok=True)
    detector = movimento.novo_detector(argumentos.area, argumentos.sensibilidade)
    evento = {"aberto": False, "avisado": False, "ultima_pessoa": 0.0}
    print(f"Vigiando {argumentos.ip} (Ctrl+C para parar)")

    for quadro in video.gerar_quadros(argumentos.ip, parar):
        mudanca = movimento.analisar(detector, quadro)
        if mudanca == "abriu":
            evento.update(aberto=True, avisado=False)
        elif mudanca == "fechou":
            evento["aberto"] = False
            print(f"{datetime.now():%H:%M:%S} movimento terminou")

        if evento["aberto"] and not evento["avisado"]:
            avisar_se_for_hora(evento, quadro, argumentos, pasta)


def avisar_se_for_hora(evento, quadro, argumentos, pasta):
    """Salva a foto do evento uma vez; com --pessoa, so quando a pessoa aparece com os pes na area.

    Chamada por comando_movimento a cada quadro com evento aberto.
    """

    if argumentos.pessoa:
        # O YOLOX custa ~25 ms na CPU: uma consulta por segundo basta para pegar quem passa
        agora = time.monotonic()
        if agora - evento["ultima_pessoa"] < INTERVALO_PESSOA_S:
            return
        evento["ultima_pessoa"] = agora
        if not pessoa.tem_pessoa(quadro, argumentos.area):
            return
    evento["avisado"] = True
    arquivo = pasta / f"{datetime.now():%Y%m%d_%H%M%S}.jpg"
    arquivo.write_bytes(video.para_jpeg(quadro))
    tipo = "pessoa" if argumentos.pessoa else "movimento"
    print(f"{datetime.now():%H:%M:%S} {tipo}! foto em {arquivo}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
