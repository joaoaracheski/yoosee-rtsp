"""Testes do detector de movimento, do filtro por area e da leitura da tabela ARP."""

import numpy as np

from yoosee_rtsp import descoberta, movimento, pessoa


def quadro(valor=100, retangulo=None):
    """Devolve um quadro cinza 90x160 liso, com um retangulo branco opcional (linhas, colunas)."""

    imagem = np.full((movimento.ALTURA, movimento.LARGURA), valor, dtype=np.uint8)
    if retangulo is not None:
        imagem[retangulo] = 255
    return imagem


def alimentar(detector, quadros, inicio=0.0):
    """Passa os quadros ao detector, um por INTERVALO_S, e devolve as mudancas que nao forem None."""

    eventos = []
    for indice, imagem in enumerate(quadros):
        agora = inicio + (indice + 1) * movimento.INTERVALO_S
        mudanca = movimento.analisar_cinza(detector, imagem, agora)
        if mudanca:
            eventos.append((indice, mudanca))
    return eventos


def andando(passos):
    """Quadros com um bloco de 20x20 que anda 5 colunas por quadro (uma pessoa pequena passando)."""

    return [
        quadro(retangulo=(slice(40, 60), slice(5 * i, 5 * i + 20)))
        for i in range(passos)
    ]


def test_movimento_abre_e_silencio_fecha():
    """Movimento sustentado abre um evento e SILENCIO_S parado fecha."""

    detector = movimento.novo_detector()
    parado = [quadro()] * (movimento.AQUECIMENTO + 2)
    silencio = [quadro()] * int(movimento.SILENCIO_S / movimento.INTERVALO_S + 2)
    eventos = alimentar(detector, parado + andando(20) + silencio)
    assert [tipo for _, tipo in eventos] == ["abriu", "fechou"]


def test_troca_para_infravermelho_nao_e_movimento():
    """Salto de brilho global (camera trocando para o modo noturno) e ignorado."""

    detector = movimento.novo_detector()
    quadros = [quadro(100)] * (movimento.AQUECIMENTO + 2) + [quadro(160)] * 10
    assert alimentar(detector, quadros) == []


def test_movimento_fora_da_area_e_ignorado():
    """Com a area so na metade de baixo, um bloco andando no alto nao abre evento."""

    detector = movimento.novo_detector(area=(0.0, 0.6, 1.0, 1.0))
    no_alto = [
        quadro(retangulo=(slice(5, 25), slice(5 * i, 5 * i + 20))) for i in range(20)
    ]
    assert alimentar(detector, [quadro()] * (movimento.AQUECIMENTO + 2) + no_alto) == []


def test_sensibilidade_dobra_a_cada_dois_niveis():
    """Dois niveis a mais deixam o detector duas vezes mais sensivel."""

    assert movimento.fracao_minima(5) == movimento.FRACAO_MIN
    assert np.isclose(movimento.fracao_minima(7), movimento.FRACAO_MIN / 2)


def test_pes_na_area_usa_a_base_da_caixa():
    """Cabeca dentro da area mas pes fora (pessoa na rua) nao conta."""

    garagem = (0.0, 0.5, 1.0, 1.0)
    assert pessoa.pes_na_area((0.4, 0.3, 0.6, 0.9, 0.8), garagem)
    assert not pessoa.pes_na_area((0.4, 0.1, 0.6, 0.45, 0.8), garagem)


def test_filtro_de_pessoa_desligado_nao_esconde_alerta():
    """Sem modelo carregado, tem_pessoa devolve True."""

    pessoa.SESSAO["sessao"] = None
    assert pessoa.tem_pessoa(object())


def test_preparar_entrada_faz_letterbox():
    """A entrada sai 1x3x416x416 com a imagem no canto e borda cinza."""

    entrada, escala = pessoa.preparar_entrada(np.zeros((360, 640, 3), dtype=np.uint8))
    assert entrada.shape == (1, 3, pessoa.LADO, pessoa.LADO)
    assert np.isclose(escala, pessoa.LADO / 640)
    assert entrada[0, 0, -1, 0] == pessoa.PREENCHIMENTO


def test_arp_windows_e_macos():
    """Le o formato do Windows (com -) e o do macOS (sem zero a esquerda)."""

    windows = "  192.168.0.20          a0-b1-c2-d3-e4-f5     dinamico\n"
    assert descoberta.PADRAO_ARP.findall(windows) == [
        ("192.168.0.20", "a0-b1-c2-d3-e4-f5")
    ]
    assert descoberta.normalizar_mac("a0:b1:c:d3:e4:5") == "a0:b1:0c:d3:e4:05"


def test_arp_linux():
    """Le o /proc/net/arp e ignora entradas incompletas (MAC zerado)."""

    texto = (
        "IP address  HW type  Flags  HW address         Mask  Device\n"
        "192.168.0.7 0x1      0x2    a0:b1:c2:d3:e4:f5  *     wlan0\n"
        "192.168.0.9 0x1      0x0    00:00:00:00:00:00  *     wlan0\n"
    )
    assert descoberta.ler_proc_arp(texto) == {"192.168.0.7": "a0:b1:c2:d3:e4:f5"}
