"""Cliente RTSP feito a mao para as cameras Yoosee, que fogem do padrao.

O SDP diz H265 mas o sprop-parameter-sets traz bytes de H.264, e a resposta do SETUP vem
com um cabecalho Transport malformado: ffmpeg e VLC recusam. Aqui o handshake e manual e o
RTP chega "interleaved" na propria conexao TCP, remontado em NALs H.265 Annex-B.
"""

import logging
import socket
import time

LOG = logging.getLogger(__name__)

# Prefixo Annex-B: o ffmpeg e o PyAV reconhecem o inicio de cada NAL por ele
INICIO_NAL = b"\x00\x00\x00\x01"

# A camera responde rapido na rede local; mais que isso e camera fora do ar
TEMPO_LIMITE_S = 6.0

# Porta RTSP fixa nessas cameras (nao da para mudar no app do fabricante)
PORTA_RTSP = 554

# Stream principal (1080p, bom para gravar) e substream (640x360, leve para ao vivo)
CAMINHO_PRINCIPAL = "onvif1"
CAMINHO_SECUNDARIO = "onvif2"

# Tipos de NAL do HEVC (RFC 7798) que precisam de tratamento especial
NAL_AP = 48
NAL_FU = 49
NAL_VPS = 32

# Canais do RTP interleaved pedidos no SETUP (0 = video, 2 = audio)
CANAL_VIDEO = 0
CANAL_AUDIO = 2

# O video ja corre antes da resposta do PLAY; passou disso sem resposta, desiste
LIMITE_ESPERA_PLAY = 4 * 1024 * 1024

# Sem resposta por mais que isso: avisa quem chamou (a camera pode ter mudado de IP)
SUMIDA_S = 60.0


def montar_url(ip, caminho):
    """Devolve a URL RTSP da camera para o caminho dado (onvif1 ou onvif2)."""

    return f"rtsp://{ip}:{PORTA_RTSP}/{caminho}"


def conectar(ip, caminho=CAMINHO_PRINCIPAL, com_audio=False):
    """Faz OPTIONS, DESCRIBE, SETUP e PLAY e devolve a sessao pronta para receber o video.

    Devolve um dict {"sock", "sobra"}: `sobra` sao os bytes de RTP que ja vieram colados na
    resposta do PLAY. Com `com_audio`, pede tambem o audio (G.711 A-law) no canal 2.
    Levanta ConnectionError se a camera recusar. Chamada por gerar_nals.
    """

    url = montar_url(ip, caminho)
    sock = socket.create_connection((ip, PORTA_RTSP), timeout=TEMPO_LIMITE_S)
    sock.settimeout(TEMPO_LIMITE_S)

    try:
        requisitar(sock, "OPTIONS", url, 1)
        cabecalho, corpo = requisitar(
            sock, "DESCRIBE", url, 2, "Accept: application/sdp\r\n"
        )
        exigir_sucesso(cabecalho, "DESCRIBE")
        sdp = corpo.decode("latin1")

        controle_video = controle_da_trilha(sdp, "video") or "track1"
        cabecalho = requisitar(
            sock,
            "SETUP",
            url_da_trilha(url, controle_video),
            3,
            "Transport: RTP/AVP/TCP;unicast;interleaved=0-1\r\n",
        )[0]
        # So o status importa: o Transport desta resposta e o cabecalho malformado
        exigir_sucesso(cabecalho, "SETUP")
        sessao = ler_sessao(cabecalho)

        controle_audio = controle_da_trilha(sdp, "audio") if com_audio else None
        if controle_audio:
            requisitar(
                sock,
                "SETUP",
                url_da_trilha(url, controle_audio),
                4,
                "Transport: RTP/AVP/TCP;unicast;interleaved=2-3\r\n"
                f"Session: {sessao}\r\n",
            )

        sock.sendall(
            (
                f"PLAY {url} RTSP/1.0\r\nCSeq: 5\r\nUser-Agent: yoosee-rtsp\r\n"
                f"Session: {sessao}\r\nRange: npt=0.000-\r\n\r\n"
            ).encode()
        )
        cabecalho, sobra = ler_resposta_play(sock)
        exigir_sucesso(cabecalho, "PLAY")
    except BaseException:
        sock.close()
        raise

    return {"sock": sock, "sobra": sobra}


def requisitar(sock, metodo, url, cseq, extra=""):
    """Envia uma requisicao RTSP e devolve (cabecalho, corpo) da resposta. Chamada por conectar."""

    pedido = (
        f"{metodo} {url} RTSP/1.0\r\n"
        f"CSeq: {cseq}\r\n"
        "User-Agent: yoosee-rtsp\r\n"
        f"{extra}\r\n"
    )
    sock.sendall(pedido.encode())
    return ler_resposta(sock)


def ler_resposta(sock):
    """Le uma resposta RTSP inteira (cabecalho e corpo pelo Content-Length).

    Devolve (cabecalho em texto, corpo em bytes). Bytes que chegarem alem do corpo (o RTP
    colado no PLAY) tambem ficam no corpo. Chamada por requisitar.
    """

    dados = b""
    while b"\r\n\r\n" not in dados:
        dados += receber(sock)

    cabecalho_bytes, _, resto = dados.partition(b"\r\n\r\n")
    cabecalho = cabecalho_bytes.decode("latin1")

    tamanho = 0
    for linha in cabecalho.split("\r\n"):
        if linha.lower().startswith("content-length"):
            tamanho = int(linha.split(":", 1)[1])

    while len(resto) < tamanho:
        resto += receber(sock)

    return cabecalho, resto


def ler_resposta_play(sock):
    """Le a resposta do PLAY, que nessas cameras chega NO MEIO do video.

    A camera comeca a mandar RTP logo depois do SETUP, entao os primeiros bytes depois do
    PLAY sao video e o "RTSP/1.0 200 OK" aparece la no meio. Devolve (cabecalho, sobra), com
    a sobra = o RTP de antes e de depois da resposta, para nada do video se perder.
    """

    dados = b""
    while True:
        inicio = dados.find(b"RTSP/1.0 ")
        fim = dados.find(b"\r\n\r\n", inicio) if inicio >= 0 else -1
        if fim >= 0:
            break
        if len(dados) > LIMITE_ESPERA_PLAY:
            raise ConnectionError("a camera nao respondeu ao PLAY")
        dados += receber(sock)

    cabecalho = dados[inicio:fim].decode("latin1")
    return cabecalho, dados[:inicio] + dados[fim + 4 :]


def receber(sock):
    """Le o que tiver no socket; levanta ConnectionError se a camera fechou a conexao.

    Sem essa checagem, um recv vazio no meio do corpo deixava o laco girando para sempre.
    """

    parte = sock.recv(65536)
    if not parte:
        raise ConnectionError("a camera fechou a conexao")
    return parte


def exigir_sucesso(cabecalho, metodo):
    """Levanta ConnectionError se a primeira linha da resposta nao for 200. Chamada por conectar."""

    primeira = cabecalho.split("\r\n", 1)[0]
    partes = primeira.split(" ", 2)
    if len(partes) < 2 or partes[1] != "200":
        raise ConnectionError(f"{metodo} falhou: {primeira}")


def ler_sessao(cabecalho):
    """Devolve o id do cabecalho Session (sem o ;timeout=). Chamada por conectar."""

    for linha in cabecalho.split("\r\n"):
        if linha.lower().startswith("session:"):
            return linha.split(":", 1)[1].strip().split(";")[0]
    return ""


def controle_da_trilha(sdp, midia):
    """Devolve o `a=control` da secao m=<midia> do SDP, ou None se a trilha nao existir.

    O sprop-parameter-sets e ignorado de proposito: nessas cameras ele vem errado (H.264) e
    os parametros certos (VPS/SPS/PPS) chegam dentro do proprio fluxo.
    """

    secao = None
    for linha in sdp.split("\n"):
        linha = linha.strip()
        if linha.startswith("m="):
            secao = linha[2:].split(" ", 1)[0]
        elif secao == midia and linha.startswith("a=control:"):
            controle = linha.split(":", 1)[1]
            if controle not in ("", "*"):
                return controle
    return None


def url_da_trilha(url, controle):
    """Monta a URL do SETUP: o controle pode ser absoluto ou relativo a URL base."""

    if controle.startswith("rtsp://"):
        return controle
    return f"{url}/{controle}"


def gerar_nals(
    ip,
    parar,
    caminho=CAMINHO_PRINCIPAL,
    ao_audio=None,
    ao_sumir=None,
    atraso_reconexao_s=2.0,
):
    """Gera os NALs H.265 (Annex-B) da camera, reconectando sozinho quando cai.

    `parar` e um threading.Event que encerra o gerador. `ao_audio(bytes)` recebe o audio
    A-law de cada pacote (so o stream principal tem audio). `ao_sumir(ip)` e chamado quando a
    camera fica mais de SUMIDA_S sem responder e pode devolver um IP novo para tentar.
    """

    buffer_fu = {"partes": [], "seq": None}
    falhando_desde = None
    caiu_em = None

    while not parar.is_set():
        sock = None
        try:
            sessao = conectar(ip, caminho, com_audio=ao_audio is not None)
            sock = sessao["sock"]
            falhando_desde = None
            if caiu_em is not None:
                LOG.warning(
                    "%s/%s: stream voltou depois de %.0f s",
                    ip,
                    caminho,
                    time.monotonic() - caiu_em,
                )
                caiu_em = None

            for canal, pacote in ler_interleaved(sock, sessao["sobra"]):
                if parar.is_set():
                    break
                if len(pacote) < 12:
                    continue
                if canal == CANAL_AUDIO and ao_audio is not None:
                    ao_audio(carga_rtp(pacote))
                elif canal == CANAL_VIDEO:
                    seq = int.from_bytes(pacote[2:4], "big")
                    yield from depacotar_h265(carga_rtp(pacote), buffer_fu, seq)

        except Exception as erro:  # noqa: BLE001 - qualquer falha vira reconexao
            if parar.is_set():
                break
            agora = time.monotonic()
            # So a transicao vai para o log: camera fora nao enche o log a cada tentativa
            if caiu_em is None:
                caiu_em = agora
                LOG.warning(
                    "%s/%s: stream caiu (%s); reconectando",
                    ip,
                    caminho,
                    type(erro).__name__,
                )
            falhando_desde = falhando_desde or agora
            if ao_sumir is not None and agora - falhando_desde > SUMIDA_S:
                ip = ao_sumir(ip) or ip
                falhando_desde = agora
            parar.wait(atraso_reconexao_s)
        finally:
            if sock is not None:
                fechar(sock)


def fechar(sock):
    """Fecha o socket ignorando erro (a conexao pode ja ter caido). Chamada por gerar_nals."""

    try:
        sock.close()
    except OSError:
        pass


def ler_interleaved(sock, sobra):
    """Gera (canal, pacote_rtp) lendo o enquadramento '$' <canal> <tamanho 16 bits> <dados>.

    Chamada por gerar_nals. Se aparecer texto RTSP no meio (resposta atrasada), pula ate o
    proximo '$'.
    """

    buffer = bytearray(sobra)
    while True:
        while len(buffer) < 4:
            buffer += receber(sock)

        if buffer[0] != 0x24:  # '$'
            proximo = buffer.find(b"$")
            del buffer[: proximo if proximo > 0 else len(buffer)]
            continue

        canal = buffer[1]
        tamanho = (buffer[2] << 8) | buffer[3]
        while len(buffer) < 4 + tamanho:
            buffer += receber(sock)

        pacote = bytes(buffer[4 : 4 + tamanho])
        del buffer[: 4 + tamanho]
        yield canal, pacote


def carga_rtp(pacote):
    """Tira o cabecalho RTP (12 bytes, CSRCs e extensao opcional) e devolve a carga util."""

    tem_extensao = pacote[0] & 0x10
    qtd_csrc = pacote[0] & 0x0F
    deslocamento = 12 + qtd_csrc * 4
    if tem_extensao and len(pacote) >= deslocamento + 4:
        tamanho_ext = (pacote[deslocamento + 2] << 8) | pacote[deslocamento + 3]
        deslocamento += 4 + tamanho_ext * 4
    return pacote[deslocamento:]


def depacotar_h265(carga, buffer_fu, seq=None):
    """Transforma a carga RTP H.265 (RFC 7798) em uma lista de NALs Annex-B.

    `buffer_fu` e um dict {"partes", "seq"} que guarda os fragmentos de um FU entre pacotes.
    Com `seq` (numero de sequencia RTP), um fragmento perdido descarta o NAL inteiro em vez
    de entregar um NAL corrompido ao decodificador.
    """

    nals = []
    if len(carga) < 2:
        return nals

    tipo = (carga[0] >> 1) & 0x3F

    if tipo == NAL_FU:
        if len(carga) < 3:
            return nals
        cabecalho_fu = carga[2]
        inicio, fim = cabecalho_fu & 0x80, cabecalho_fu & 0x40
        esperado = None if buffer_fu["seq"] is None else (buffer_fu["seq"] + 1) & 0xFFFF

        if inicio:
            # Reconstroi o cabecalho NAL original trocando o tipo 49 pelo tipo do fragmento
            byte0 = (carga[0] & 0x81) | ((cabecalho_fu & 0x3F) << 1)
            buffer_fu["partes"] = [bytes([byte0, carga[1]])]
        elif not buffer_fu["partes"] or (seq is not None and seq != esperado):
            buffer_fu["partes"] = []  # fragmento perdido: descarta o NAL todo
            buffer_fu["seq"] = seq
            return nals

        buffer_fu["partes"].append(carga[3:])
        buffer_fu["seq"] = seq
        if fim:
            nals.append(INICIO_NAL + b"".join(buffer_fu["partes"]))
            buffer_fu["partes"] = []

    elif tipo == NAL_AP:
        posicao = 2
        while posicao + 2 <= len(carga):
            tamanho = (carga[posicao] << 8) | carga[posicao + 1]
            posicao += 2
            nals.append(INICIO_NAL + carga[posicao : posicao + tamanho])
            posicao += tamanho

    else:
        # NAL unico: inclui VPS (32), SPS (33), PPS (34) e as fatias de imagem
        nals.append(INICIO_NAL + carga)

    return nals


def abre_gop(nal):
    """Diz se o NAL Annex-B e um VPS, que abre o GOP com os parametros do video."""

    return len(nal) > 4 and (nal[4] >> 1) & 0x3F == NAL_VPS
