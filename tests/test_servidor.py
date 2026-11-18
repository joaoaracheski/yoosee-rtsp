"""Testes do servir e dos comandos do ffmpeg, sem camera e sem MediaMTX."""

import pytest

from yoosee_rtsp import gravacao, servidor


def test_ler_camera_com_e_sem_nome():
    """'nome=ip' vira o dict; so o IP ganha o nome camera<indice>."""

    assert servidor.ler_camera("garagem=192.168.0.20", 1) == {
        "nome": "garagem",
        "ip": "192.168.0.20",
    }
    assert servidor.ler_camera("192.168.0.21", 2) == {
        "nome": "camera2",
        "ip": "192.168.0.21",
    }


def test_ler_camera_recusa_nome_que_quebra_a_url():
    """Nome com espaco ou barra e recusado antes de virar caminho no MediaMTX."""

    with pytest.raises(ValueError):
        servidor.ler_camera("sala de estar=192.168.0.20", 1)
    with pytest.raises(ValueError):
        servidor.ler_camera("a/b=192.168.0.20", 1)


def test_comando_do_servir_copia_e_publica_por_tcp():
    """O servir copia o video (sem recodificar) e publica em RTSP sobre TCP."""

    comando = servidor.montar_comando("rtsp://127.0.0.1:8554/garagem")
    assert comando[comando.index("-c") + 1] == "copy"
    assert comando[-5:-3] == ["-f", "rtsp"]
    assert comando[-1] == "rtsp://127.0.0.1:8554/garagem"
    assert comando[comando.index("-rtsp_transport") + 1] == "tcp"


def test_comando_da_gravacao_usa_pasta_por_dia(tmp_path):
    """A gravacao fatia em segmentos mp4 numa pasta por dia."""

    comando = gravacao.montar_comando(tmp_path, "garagem", 600)
    assert comando[comando.index("-segment_time") + 1] == "600"
    assert comando[-1].endswith("garagem_%Y%m%d_%H%M%S.mp4")
    assert "%Y-%m-%d" in comando[-1]
