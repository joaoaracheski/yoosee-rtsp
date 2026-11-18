# yoosee-rtsp

Grave, assista e vigie câmeras **Yoosee** em Python, direto na rede de casa: sem o app do fabricante, sem nuvem e sem mensalidade. E republique as câmeras como RTSP padrão para usar no VLC, Home Assistant ou Frigate.

> **English:** Yoosee IP cameras ship a broken RTSP server that ffmpeg and VLC reject. This is a small pure-Python RTSP client that works around it, plus continuous recording, LAN discovery, motion detection with an optional on-device person filter (YOLOX-nano), and a `servir` command that republishes the cameras as standard RTSP through MediaMTX (so Home Assistant, Frigate and VLC just work). Code and docs are in Portuguese; the commands below work as-is.

## O problema

As câmeras Yoosee (e as genéricas com o mesmo firmware) anunciam RTSP na porta 554, mas o servidor delas foge do padrão em três pontos:

1. O SDP diz que o vídeo é **H265**, mas o `sprop-parameter-sets` traz bytes de **H.264**. Os parâmetros certos (VPS/SPS/PPS) só chegam dentro do próprio fluxo.
2. A resposta do `SETUP` tem um cabeçalho `Transport` malformado, e o ffmpeg e o VLC desistem ali.
3. A câmera começa a mandar vídeo **antes** de responder ao `PLAY`: o `RTSP/1.0 200 OK` chega no meio do RTP.

Resultado: `ffmpeg -i rtsp://ip:554/onvif1` falha, e o único jeito "oficial" de ver a câmera é o app do celular.

## A solução

Um cliente RTSP de ~300 linhas que faz o handshake na mão, recebe o RTP "interleaved" pela própria conexão TCP e remonta os NALs H.265 (RFC 7798). Daí para frente é vídeo normal:

```text
câmera Yoosee (porta 554)
 └─ rtsp.py: OPTIONS → DESCRIBE → SETUP → PLAY (tolerando as 3 falhas acima)
     └─ RTP interleaved → NALs H.265 Annex-B
         ├─ gravar:    ffmpeg -c copy → .mp4 em segmentos, uma pasta por dia
         ├─ servir:    ffmpeg -c copy → MediaMTX → RTSP padrão / WebRTC / HLS
         └─ foto e movimento: PyAV decodifica → JPEG / diferença de quadros
             └─ --pessoa: YOLOX-nano confirma pessoa com os pés na área
```

Falhas tratadas:

- **Câmera caiu ou reiniciou:** reconecta sozinho; o log registra só a queda e a volta.
- **IP mudou** (o roteador reiniciou e trocou os IPs): depois de 60 s sem resposta, procura a câmera pelo MAC na rede e segue no IP novo.
- **ffmpeg morreu, ou a câmera voltou com outros parâmetros:** sobe outro ffmpeg no próximo início de GOP, sem derrubar o resto.
- **Pacote perdido no meio de um quadro:** o quadro é descartado em vez de ir corrompido para o decodificador.

---

## Como usar

### 1. Instalar

Precisa de Python 3.10 ou mais novo. O ffmpeg já vem junto (pacote `imageio-ffmpeg`), não precisa instalar à parte.

```powershell
git clone https://github.com/joaoaracheski/yoosee-rtsp
cd yoosee-rtsp
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

No Linux e no macOS troque `.venv\Scripts\` por `.venv/bin/`.

Antes de começar, a câmera precisa estar configurada pelo app Yoosee e ligada na **mesma rede** que o computador. As câmeras testadas não pedem senha no RTSP.

> **Dica:** reserve um IP fixo para cada câmera no roteador (DHCP estático). O programa acha a câmera pelo MAC se o IP mudar, mas com IP fixo nem precisa.

### 2. Achar as câmeras

```powershell
.venv\Scripts\python main.py descobrir
```

```text
Procurando cameras em 192.168.0.0/24 ...
192.168.0.20     a0:b1:c2:d3:e4:f5
192.168.0.21     a0:b1:c2:d3:e4:a7
```

Ele varre a rede deste computador na porta 554 e só lista o que responde como Yoosee (outras câmeras RTSP ficam de fora). Para varrer outra rede: `--rede 192.168.1`.

### 3. Tirar uma foto (teste rápido)

```powershell
.venv\Scripts\python main.py foto 192.168.0.20 -o garagem.jpg
```

Se a foto saiu, está tudo funcionando. `--leve` usa o substream (640x360), mais rápido.

### 4. Gravar

```powershell
.venv\Scripts\python main.py gravar 192.168.0.20 --nome garagem --pasta D:\cameras
```

Grava sem parar até o Ctrl+C, em arquivos de 10 minutos:

```text
D:\cameras\
  2026-11-17\
    garagem_20261117_140000.mp4
    garagem_20261117_141000.mp4
  garagem_ffmpeg.log
```

| Opção | Padrão | O que faz |
|---|---|---|
| `--nome` | `camera` | prefixo dos arquivos |
| `--pasta` | `gravacoes/` | onde salvar |
| `--segmento` | `600` | segundos por arquivo |

O vídeo é copiado sem recodificar (quase zero de CPU, ~1 GB por hora a 1080p). O arquivo atual já toca enquanto grava e não se perde numa queda de energia. Para várias câmeras, rode um `gravar` para cada uma. Apagar os arquivos antigos fica por sua conta (ex.: uma tarefa agendada que apaga pastas com mais de 7 dias).

### 5. Vigiar movimento

```powershell
.venv\Scripts\python main.py movimento 192.168.0.20 --area 0,0.4,1,1 --sensibilidade 6
```

```text
14:32:10 movimento! foto em alertas\20261117_143210.jpg
14:32:41 movimento terminou
```

Cada episódio de movimento salva uma foto (um aviso por episódio, não um por segundo). A troca para o modo noturno e luz acendendo não contam como movimento.

| Opção | Padrão | O que faz |
|---|---|---|
| `--area` | tela toda | região vigiada: `esquerda,topo,direita,base` de 0 a 1 |
| `--sensibilidade` | `5` | de 1 (pouco sensível) a 10 (muito) |
| `--pessoa` | desligado | só avisa se aparecer uma pessoa |
| `--pasta` | `alertas/` | onde salvar as fotos |

**Como escolher a área:** tire uma foto e meça a proporção. `0,0.4,1,1` = largura toda, dos 40% de altura até embaixo (ignora o céu e a rua do fundo). `0.5,0,1,1` = só a metade direita.

**Filtro de pessoa (`--pessoa`):** acaba com o alarme falso de gato, sombra e farol. Baixe o modelo [`yolox_nano.onnx`](https://github.com/Megvii-BaseDetection/YOLOX/releases) (Apache 2.0, ~3,6 MB) para a pasta `modelos/`. Ele roda na CPU em ~25 ms e só é consultado enquanto há movimento. Com `--area`, a pessoa só conta se os **pés** estiverem dentro da área: quem passa na calçada pode ter a cabeça "dentro" da garagem pela perspectiva, mas os pés continuam na rua.

### 6. Usar no VLC, Home Assistant ou Frigate (`servir`)

Esses programas não entendem a Yoosee. O `servir` lê a câmera, conserta o fluxo e publica num [MediaMTX](https://github.com/bluenviron/mediamtx), um servidor de vídeo gratuito que entrega RTSP normal para quantos programas quiserem.

**a) Rodar o MediaMTX.** Baixe o zip do seu sistema na página de [releases](https://github.com/bluenviron/mediamtx/releases), extraia e rode `mediamtx.exe` (ou `./mediamtx`). A configuração padrão já serve.

**b) Publicar as câmeras:**

```powershell
.venv\Scripts\python main.py servir garagem=192.168.0.20 sala=192.168.0.21 --sub
```

```text
Enderecos para VLC / Home Assistant / Frigate:
  rtsp://<ip-deste-pc>:8554/garagem
  rtsp://<ip-deste-pc>:8554/garagem_sub
  rtsp://<ip-deste-pc>:8554/sala
  rtsp://<ip-deste-pc>:8554/sala_sub
```

| Opção | Padrão | O que faz |
|---|---|---|
| `nome=ip` | | uma ou mais câmeras; só o IP vira `camera1`, `camera2`... |
| `--sub` | desligado | publica também o substream 640x360 como `<nome>_sub` |
| `--destino` | `rtsp://127.0.0.1:8554` | MediaMTX em outra máquina |
| `--logs` | `logs/` | erros do ffmpeg, um arquivo por stream |

**c) Assistir:**

- **VLC:** Mídia → Abrir fluxo de rede → `rtsp://192.168.0.5:8554/garagem`.
- **Navegador:** o MediaMTX também serve WebRTC em `http://192.168.0.5:8889/garagem`. O navegador precisa tocar H.265 (Chrome e Edge recentes no Windows tocam; o Firefox não).
- **Home Assistant:** Configurações → Dispositivos → Adicionar integração → **Generic Camera**, com `rtsp://192.168.0.5:8554/garagem` em "Stream source".
- **Frigate:** o substream para detectar e o principal para gravar:

```yaml
cameras:
  garagem:
    ffmpeg:
      inputs:
        - path: rtsp://192.168.0.5:8554/garagem_sub
          roles: [detect]
        - path: rtsp://192.168.0.5:8554/garagem
          roles: [record]
    detect:
      width: 640
      height: 360
```

> **Limite de conexões:** cada câmera aceita poucas conexões ao mesmo tempo (nas testadas, a terceira começa a falhar). `servir --sub` já usa duas. Se for usar o `servir`, deixe a gravação com o Frigate ou com a opção `record` do MediaMTX em vez de rodar o `gravar` junto: eles leem do MediaMTX, não da câmera.

### 7. Deixar rodando sempre

**Windows (Agendador de Tarefas):** crie uma tarefa "Ao iniciar o computador", com a opção "Executar estando o usuário conectado ou não", e a ação:

```text
Programa:  C:\caminho\yoosee-rtsp\.venv\Scripts\python.exe
Argumentos: main.py servir garagem=192.168.0.20 sala=192.168.0.21 --sub
Iniciar em: C:\caminho\yoosee-rtsp
```

**Linux (systemd):** `/etc/systemd/system/yoosee.service`

```ini
[Unit]
Description=yoosee-rtsp
After=network-online.target

[Service]
WorkingDirectory=/opt/yoosee-rtsp
ExecStart=/opt/yoosee-rtsp/.venv/bin/python main.py servir garagem=192.168.0.20 --sub
Restart=always
User=yoosee

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now yoosee
```

### Problemas comuns

| Sintoma | Causa provável | O que fazer |
|---|---|---|
| `descobrir` não acha nada | câmera em outra rede (Wi-Fi de convidados, outra faixa) | confira o IP no app Yoosee e use `--rede` |
| `stream caiu (TimeoutError)` de vez em quando | a câmera já tem conexões demais abertas (app, outro programa) | feche o app no celular; ele reconecta sozinho |
| `foto` diz que a câmera não mandou imagem | câmera reiniciando ou fora do ar | espere 1 minuto e tente de novo; teste com `--leve` |
| `servir` roda mas o VLC não abre | MediaMTX parado ou firewall | rode o MediaMTX e libere as portas 8554 (RTSP) e 8889 (WebRTC) |
| imagem cinza ou borrada por 1 a 2 segundos ao conectar | normal: o vídeo só fica limpo no próximo quadro-chave | nada |

---

## Como biblioteca

```python
from threading import Event
from yoosee_rtsp import rtsp, video, movimento

parar = Event()
detector = movimento.novo_detector(area=(0, 0.4, 1, 1))

for quadro in video.gerar_quadros("192.168.0.20", parar):
    if movimento.analisar(detector, quadro) == "abriu":
        open("alerta.jpg", "wb").write(video.para_jpeg(quadro))
```

`rtsp.gerar_nals(ip, parar)` entrega os NALs H.265 crus para mandar a qualquer destino.

## Estrutura

```text
main.py                  linha de comando (descobrir, foto, gravar, movimento, servir)
yoosee_rtsp/
  rtsp.py                cliente RTSP e depacketização H.265
  alimentador.py         escreve os NALs num ffmpeg e troca o ffmpeg quando precisa
  gravacao.py            ffmpeg em segmentos .mp4, uma pasta por dia
  servidor.py            ffmpeg publicando no MediaMTX
  descoberta.py          varredura da rede /24, confirmação pelo SDP, MAC pela tabela ARP
  video.py               decodificação (PyAV) e JPEG
  movimento.py           detector por diferença de quadros
  pessoa.py              filtro de pessoa YOLOX-nano (onnxruntime)
tests/                   testes sem câmera (pacotes montados à mão, socket falso)
modelos/                 onde fica o yolox_nano.onnx (fora do git)
```

```powershell
.venv\Scripts\pip install -r requirements-dev.txt
.venv\Scripts\python -m pytest
```

## Origem

Extraído do servidor de câmeras que roda 24/7 na minha casa (gravação contínua, alertas com clipe no Telegram, ao vivo no celular), feito em Python com FastAPI e Flet.

Licença MIT.
