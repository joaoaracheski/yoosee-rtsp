"""Testes do cliente RTSP sem camera: pacotes montados a mao e um socket falso."""

import functools
from types import SimpleNamespace

import pytest

from yoosee_rtsp import rtsp

SDP_YOOSEE = (
    "v=0\r\n"
    "m=video 0 RTP/AVP 96\r\n"
    "a=rtpmap:96 H265/90000\r\n"
    "a=fmtp:96 sprop-parameter-sets=Z0IAH5WoFAFuQA==,aM48gA==\r\n"
    "a=control:track1\r\n"
    "m=audio 0 RTP/AVP 8\r\n"
    "a=control:track2\r\n"
)


def socket_falso(*pedacos):
    """Imita um socket cujo recv entrega os pedacos em ordem e depois b"" (conexao fechada)."""

    fila = list(pedacos)
    return SimpleNamespace(recv=functools.partial(tirar_da_fila, fila))


def tirar_da_fila(fila, _tamanho):
    """recv do socket_falso: o proximo pedaco, ou b"" quando acabou."""

    return fila.pop(0) if fila else b""


def rtp(carga, seq=0, csrc=0):
    """Monta um pacote RTP minimo (versao 2) com a carga dada."""

    return (
        bytes([0x80 | csrc, 96])
        + seq.to_bytes(2, "big")
        + b"\0" * 8
        + b"\0" * 4 * csrc
        + carga
    )


def test_controle_da_trilha_ignora_sessao_e_acha_video_e_audio():
    """O a=control de cada m= e encontrado; sem trilha devolve None."""

    assert rtsp.controle_da_trilha(SDP_YOOSEE, "video") == "track1"
    assert rtsp.controle_da_trilha(SDP_YOOSEE, "audio") == "track2"
    assert rtsp.controle_da_trilha("m=video 0 RTP/AVP 96\r\n", "audio") is None


def test_url_da_trilha_relativa_e_absoluta():
    """Controle relativo e pendurado na URL; absoluto e usado como veio."""

    url = rtsp.montar_url("10.0.0.5", "onvif1")
    assert rtsp.url_da_trilha(url, "track1") == "rtsp://10.0.0.5:554/onvif1/track1"
    assert rtsp.url_da_trilha(url, "rtsp://x/t") == "rtsp://x/t"


def test_exigir_sucesso():
    """So 200 passa; outro status levanta ConnectionError com a linha da resposta."""

    rtsp.exigir_sucesso("RTSP/1.0 200 OK\r\nCSeq: 1", "PLAY")
    with pytest.raises(ConnectionError, match="401"):
        rtsp.exigir_sucesso("RTSP/1.0 401 Unauthorized", "DESCRIBE")


def test_ler_sessao_tira_timeout():
    """O id da sessao vem sem o ;timeout=."""

    assert rtsp.ler_sessao("RTSP/1.0 200 OK\r\nSession: 1234;timeout=60") == "1234"


def test_ler_resposta_junta_pedacos_e_respeita_content_length():
    """Cabecalho e corpo quebrados em varios recv voltam inteiros."""

    sock = socket_falso(b"RTSP/1.0 200 OK\r\nContent-", b"Length: 5\r\n\r\nab", b"cde")
    cabecalho, corpo = rtsp.ler_resposta(sock)
    assert cabecalho.endswith("Content-Length: 5")
    assert corpo == b"abcde"


def test_ler_resposta_com_conexao_fechada_no_meio_do_corpo_nao_trava():
    """Camera que fecha no meio do corpo levanta ConnectionError em vez de girar para sempre."""

    sock = socket_falso(b"RTSP/1.0 200 OK\r\nContent-Length: 50\r\n\r\nab")
    with pytest.raises(ConnectionError):
        rtsp.ler_resposta(sock)


def test_resposta_do_play_no_meio_do_video():
    """O 200 OK que chega depois de RTP e achado e o RTP de antes e depois vira a sobra."""

    antes, depois = b"$\x00\x00\x02ab", b"$\x00\x00\x01c"
    resposta = b"RTSP/1.0 200 OK\r\nCSeq: 5\r\nSession: 1\r\n\r\n"
    sock = socket_falso(antes + resposta[:10], resposta[10:] + depois)
    cabecalho, sobra = rtsp.ler_resposta_play(sock)
    assert cabecalho.startswith("RTSP/1.0 200 OK")
    assert sobra == antes + depois


def test_ler_interleaved_separa_canais_e_ressincroniza():
    """Lixo antes do '$' e pulado; cada quadro sai com seu canal."""

    dados = b"RTSP/1.0 200 OK\r\n\r\n" + b"$\x00\x00\x03abc" + b"$\x02\x00\x02xy"
    sock = socket_falso(dados[:10], dados[10:])
    leitor = rtsp.ler_interleaved(sock, b"")
    assert next(leitor) == (0, b"abc")
    assert next(leitor) == (2, b"xy")


def test_carga_rtp_pula_csrc_e_extensao():
    """CSRCs e o bloco de extensao nao entram na carga."""

    assert rtsp.carga_rtp(rtp(b"carga", csrc=2)) == b"carga"
    extensao = b"\xbe\xde\x00\x01" + b"\0" * 4
    pacote = bytes([0x90, 96]) + b"\0" * 10 + extensao + b"carga"
    assert rtsp.carga_rtp(pacote) == b"carga"


def test_nal_unico_ganha_prefixo():
    """NAL simples (ex.: VPS tipo 32) sai com 00 00 00 01 na frente."""

    vps = bytes([32 << 1, 1]) + b"dados"
    assert rtsp.depacotar_h265(vps, {"partes": [], "seq": None}) == [
        rtsp.INICIO_NAL + vps
    ]


def test_agregacao_ap_vira_varios_nals():
    """Um AP (tipo 48) com dois NALs devolve os dois."""

    nal_a, nal_b = bytes([33 << 1, 1]) + b"sps", bytes([34 << 1, 1]) + b"pps"
    carga = bytes([48 << 1, 1])
    for nal in (nal_a, nal_b):
        carga += len(nal).to_bytes(2, "big") + nal
    nals = rtsp.depacotar_h265(carga, {"partes": [], "seq": None})
    assert nals == [rtsp.INICIO_NAL + nal_a, rtsp.INICIO_NAL + nal_b]


def fragmentos(tipo, corpo, tamanho=4):
    """Quebra um NAL em cargas FU (tipo 49) como a camera manda."""

    partes = [corpo[i : i + tamanho] for i in range(0, len(corpo), tamanho)]
    cargas = []
    for indice, parte in enumerate(partes):
        cabecalho_fu = tipo
        if indice == 0:
            cabecalho_fu |= 0x80
        if indice == len(partes) - 1:
            cabecalho_fu |= 0x40
        cargas.append(bytes([49 << 1, 1, cabecalho_fu]) + parte)
    return cargas


def test_fragmentacao_fu_remonta_o_nal_original():
    """Os fragmentos FU voltam a ser um NAL com o cabecalho e o tipo originais."""

    buffer_fu = {"partes": [], "seq": None}
    saida = []
    for seq, carga in enumerate(fragmentos(19, b"fatia-idr-grande")):
        saida += rtsp.depacotar_h265(carga, buffer_fu, seq)
    assert saida == [rtsp.INICIO_NAL + bytes([19 << 1, 1]) + b"fatia-idr-grande"]
    assert not rtsp.abre_gop(saida[0])


def test_fragmento_perdido_descarta_o_nal():
    """Pulo no numero de sequencia no meio de um FU nao gera NAL corrompido."""

    cargas = fragmentos(19, b"fatia-idr-grande")
    buffer_fu = {"partes": [], "seq": None}
    saida = []
    for seq, carga in zip((0, 1, 3, 4), cargas):  # o seq 2 se perdeu
        saida += rtsp.depacotar_h265(carga, buffer_fu, seq)
    assert saida == []


def test_numero_de_sequencia_da_volta():
    """Depois do 65535 vem o 0: nao e perda."""

    buffer_fu = {"partes": [], "seq": None}
    saida = []
    for seq, carga in zip((65534, 65535, 0, 1), fragmentos(1, b"0123456789abcdef")):
        saida += rtsp.depacotar_h265(carga, buffer_fu, seq)
    assert len(saida) == 1


def test_abre_gop_reconhece_vps():
    """So o VPS (tipo 32) abre GOP."""

    assert rtsp.abre_gop(rtsp.INICIO_NAL + bytes([32 << 1, 1]))
    assert not rtsp.abre_gop(rtsp.INICIO_NAL + bytes([33 << 1, 1]))


def test_carga_curta_nao_quebra():
    """Carga de 0 ou 1 byte e FU sem cabecalho completo sao ignorados sem excecao."""

    buffer_fu = {"partes": [], "seq": None}
    assert rtsp.depacotar_h265(b"", buffer_fu) == []
    assert rtsp.depacotar_h265(bytes([49 << 1, 1]), buffer_fu) == []
