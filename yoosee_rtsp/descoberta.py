"""Acha as cameras Yoosee na rede local e reencontra uma camera pelo MAC quando o IP muda.

Sem reserva no DHCP, o roteador troca os IPs quando reinicia e a gravacao para. A busca varre
a rede /24 na porta 554, confirma pelo DESCRIBE que e uma Yoosee e le o MAC na tabela ARP.
"""

import re
import socket
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from yoosee_rtsp import rtsp

# Varredura rapida: na rede local quem atende responde em poucos milissegundos
TEMPO_LIMITE_PORTA_S = 0.8

# 254 conexoes em paralelo derrubam alguns roteadores domesticos; 64 e seguro e rapido
MAX_PARALELO = 64

# Linha do `arp -a` (Windows/macOS): IP seguido do MAC com - ou :
PADRAO_ARP = re.compile(
    r"(\d+\.\d+\.\d+\.\d+)\D+?([0-9a-fA-F]{1,2}(?:[-:][0-9a-fA-F]{1,2}){5})"
)

# Sem isso o Windows pisca uma janela de console a cada `arp -a`
SEM_JANELA = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def descobrir(rede=None):
    """Devolve a lista de cameras achadas: dicts {"ip", "mac", "servidor", "codec"}.

    `rede` e o prefixo /24 (ex.: "192.168.0"); sem ele, usa a rede deste computador.
    """

    rede = rede or rede_local()
    candidatos = hosts_com_rtsp(rede)
    arp = tabela_arp()

    with ThreadPoolExecutor(max_workers=MAX_PARALELO) as executor:
        fichas = list(executor.map(identificar, candidatos))

    cameras = []
    for ip, ficha in zip(candidatos, fichas):
        if ficha is not None:
            cameras.append({"ip": ip, "mac": arp.get(ip, ""), **ficha})
    return cameras


def rede_local():
    """Devolve o prefixo /24 da rede deste computador (ex.: "192.168.0").

    Usa um socket UDP "conectado" a um IP externo so para o sistema escolher a placa de rede;
    nenhum pacote e enviado.
    """

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.connect(("10.255.255.255", 1))
        ip = sock.getsockname()[0]
    return ip.rsplit(".", 1)[0]


def hosts_com_rtsp(rede):
    """Devolve os IPs da rede /24 com a porta 554 aberta. A tentativa tambem enche a tabela ARP."""

    ips = [f"{rede}.{final}" for final in range(1, 255)]
    with ThreadPoolExecutor(max_workers=MAX_PARALELO) as executor:
        abertas = list(executor.map(porta_aberta, ips))
    return [ip for ip, aberta in zip(ips, abertas) if aberta]


def porta_aberta(ip):
    """Diz se o IP aceita conexao na porta RTSP. Chamada por hosts_com_rtsp."""

    try:
        with socket.create_connection(
            (ip, rtsp.PORTA_RTSP), timeout=TEMPO_LIMITE_PORTA_S
        ):
            return True
    except OSError:
        return False


def identificar(ip):
    """Faz um DESCRIBE no onvif1 e devolve {"servidor", "codec"} se parecer uma Yoosee.

    A marca da Yoosee e o SDP com H265 no rtpmap; outras cameras RTSP na rede (que nao
    precisam deste cliente) ficam de fora. Devolve None se nao for.
    """

    try:
        with socket.create_connection(
            (ip, rtsp.PORTA_RTSP), timeout=rtsp.TEMPO_LIMITE_S
        ) as sock:
            sock.settimeout(rtsp.TEMPO_LIMITE_S)
            cabecalho, corpo = rtsp.requisitar(
                sock,
                "DESCRIBE",
                rtsp.montar_url(ip, rtsp.CAMINHO_PRINCIPAL),
                1,
                "Accept: application/sdp\r\n",
            )
            rtsp.exigir_sucesso(cabecalho, "DESCRIBE")
    except (OSError, ValueError):  # ConnectionError e filha de OSError
        return None

    if "H265" not in corpo.decode("latin1").upper():
        return None

    servidor = ""
    for linha in cabecalho.split("\r\n"):
        if linha.lower().startswith("server:"):
            servidor = linha.split(":", 1)[1].strip()
    return {"servidor": servidor, "codec": "H265"}


def tabela_arp():
    """Devolve {ip: mac} dos vizinhos que o sistema conhece agora (MAC minusculo, com ':')."""

    caminho_linux = Path("/proc/net/arp")
    if sys.platform.startswith("linux") and caminho_linux.exists():
        return ler_proc_arp(caminho_linux.read_text())

    try:
        saida = subprocess.run(
            ["arp", "-a"],
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=SEM_JANELA,
            errors="replace",
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    return {ip: normalizar_mac(mac) for ip, mac in PADRAO_ARP.findall(saida)}


def ler_proc_arp(texto):
    """Le o /proc/net/arp do Linux (IP na 1a coluna, MAC na 4a). Chamada por tabela_arp."""

    tabela = {}
    for linha in texto.splitlines()[1:]:
        colunas = linha.split()
        if len(colunas) >= 4 and colunas[3] != "00:00:00:00:00:00":
            tabela[colunas[0]] = normalizar_mac(colunas[3])
    return tabela


def normalizar_mac(mac):
    """Deixa o MAC em minusculo com ':' e dois digitos por byte (o macOS omite o zero)."""

    partes = re.split(r"[-:]", mac.strip().lower())
    return ":".join(parte.zfill(2) for parte in partes)


def relocalizar(mac, rede):
    """Procura na rede quem atende RTSP com esse MAC e devolve o IP novo (ou None).

    Feita para o `ao_sumir` do rtsp.gerar_nals: use functools.partial com o MAC da camera.
    """

    mac = normalizar_mac(mac)
    candidatos = hosts_com_rtsp(rede)
    arp = tabela_arp()  # depois da varredura: as tentativas encheram a tabela
    return next((ip for ip in candidatos if arp.get(ip) == mac), None)


def anotar_macs(ips):
    """Devolve {ip: mac} das cameras que respondem agora (para achar de novo se o IP mudar).

    A conexao de teste e o que poe cada camera na tabela ARP do sistema.
    """

    for ip in ips:
        porta_aberta(ip)
    arp = tabela_arp()
    return {ip: arp[ip] for ip in ips if ip in arp}


def ao_sumir_pela_tabela(macs, ip_antigo):
    """Callback para rtsp.gerar_nals com varias cameras: acha o MAC pelo IP que sumiu.

    `macs` e o dict de anotar_macs; o IP novo e anotado nele, para a camera ser achada de
    novo se mudar outra vez.
    """

    mac = macs.get(ip_antigo)
    if not mac:
        return None
    novo = relocalizar(mac, ip_antigo.rsplit(".", 1)[0])
    if novo:
        macs[novo] = mac
    return novo


def ao_sumir_pelo_mac(mac, ip_antigo):
    """Callback pronto para rtsp.gerar_nals: procura o MAC na mesma /24 do IP antigo."""

    return relocalizar(mac, ip_antigo.rsplit(".", 1)[0])
