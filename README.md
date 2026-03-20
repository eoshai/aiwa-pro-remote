# 📺 AIWA Pro Remote

Controle remoto virtual para TV **AIWA com Android TV**, controlado via ADB over TCP/IP pela rede local.

---

## ✨ Funcionalidades

- **D-Pad completo** — navegação, OK, volume, canais
- **Atalhos rápidos** — Home, Voltar, Configurações, Menu, Power
- **Envio de texto** — digita direto no campo de busca da TV
- **Lançador de apps** — YouTube, Netflix, Prime Video, Globoplay, Spotify e mais
- **Espelhamento ao vivo** — via scrcpy com controle de bitrate, FPS e gravação em MP4
- **Screenshot** — captura a tela da TV e abre em popup para salvar
- **Monitor de desempenho** — CPU e RAM em tempo real via `/proc/stat` e `/proc/meminfo`
- **Notificações na TV** — Toast flutuante, notificação longa e overlay via APK próprio
- **Reconexão automática** — detecta desconexão e tenta reconectar em background
- **Indicador de status** — 🟢 Conectado · 🟡 Standby · 🔴 Desconectado
- **Log de comandos** — console em tempo real com timestamp

---

## 📋 Requisitos

| Dependência | Versão mínima | Instalação |
|---|---|---|
| Python | 3.10+ | [python.org](https://python.org) |
| CustomTkinter | qualquer | `pip install customtkinter` |
| Pillow | qualquer | `pip install Pillow` |
| ADB (Android Platform Tools) | qualquer | [developer.android.com](https://developer.android.com/tools/releases/platform-tools) |
| scrcpy *(opcional)* | qualquer | ver abaixo |

### Instalar dependências Python
```bash
pip install customtkinter Pillow
```

### Instalar ADB
- **Windows:** baixe o [Platform Tools](https://developer.android.com/tools/releases/platform-tools), extraia e adicione ao PATH
- **macOS:** `brew install android-platform-tools`
- **Linux:** `sudo apt install adb`

### Instalar scrcpy *(para espelhamento)*
- **Windows:** `winget install Genymobile.scrcpy`
- **macOS:** `brew install scrcpy`
- **Linux:** `sudo apt install scrcpy`

---

## 📱 Configuração da TV

### 1. Ativar Depuração USB
Na TV: **Configurações → Sobre → Informações do dispositivo** → clique 7x em **Build number** para ativar o modo desenvolvedor.

Depois: **Configurações → Preferências do dispositivo → Opções do desenvolvedor → Depuração USB** → Ativar.

### 2. Configurar IP estático
Na TV: **Configurações → Rede → Wi-Fi → (sua rede) → IP estático**

Defina um IP fixo, por exemplo `192.168.0.159`.

### 3. Ativar ADB over TCP/IP
```bash
# Conecte o cabo USB uma vez e rode:
adb tcpip 5555
adb connect 192.168.0.159:5555

# Confirme:
adb devices
```

Após isso o cabo USB não é mais necessário.

---

## 🚀 Como usar

```bash
python aiwa_pro_remote.py
```

O app conecta automaticamente ao iniciar. O IP está configurado diretamente no código (`192.168.0.159:5555`) — edite a variável `TV_HOST` no início do arquivo se necessário.

---

## 🔔 Notificações na TV

As notificações usam um APK auxiliar próprio (`aiwa-toast`) que precisa ser instalado uma vez na TV.

### Compilar o APK
O código-fonte do APK está no repositório [aiwa-toast](https://github.com/eoshai/aiwa-toast). O GitHub Actions compila automaticamente — baixe o artefato `aiwa-toast` na aba **Actions**.

### Instalar na TV
```bash
adb install app-debug.apk
```

### Testar
```bash
adb shell am broadcast \
  -a com.aiwa.SHOW_TOAST \
  -n com.aiwa.remote.toast/.ToastReceiver \
  --es message "Funcionou!"
```

---

## 🎮 Mapeamento de teclas ADB

| Botão | Keycode |
|---|---|
| Home | 3 |
| Voltar | 4 |
| Power | 26 |
| OK / Enter | 66 |
| Cima | 19 |
| Baixo | 20 |
| Esquerda | 21 |
| Direita | 22 |
| Vol+ | 24 |
| Vol- | 25 |
| Mute | 164 |
| CH+ | 166 |
| CH- | 167 |
| Backspace | 67 |

---

## 🗂️ Estrutura do projeto

```
aiwa_pro_remote.py   # aplicação principal
README.md            # este arquivo
```

---

## 🛠️ Arquitetura

- **GUI:** CustomTkinter (dark mode nativo)
- **Backend ADB:** todas as chamadas rodam em `threading.Thread` daemon — nunca na thread principal
- **Comunicação GUI ↔ Backend:** `queue.Queue` (padrão produtor/consumidor thread-safe)
- **Monitor de conexão:** loop a cada 5s via `dumpsys power`
- **Monitor de CPU/RAM:** dois samples do `/proc/stat` com intervalo de 500ms para calcular % real
- **scrcpy:** processo externo via `subprocess.Popen`, monitorado em thread separada

---

## ⚙️ Configurações no código

```python
TV_HOST         = "192.168.0.159"   # IP da TV
TV_PORT         = 5555              # porta ADB
ADB_TIMEOUT     = 8                 # timeout dos comandos (segundos)
PING_INTERVAL   = 5                 # intervalo do monitor de conexão
RECONNECT_DELAY = 10                # espera antes de reconectar
STATS_INTERVAL  = 2                 # intervalo do monitor CPU/RAM
```

---

## 📄 Licença

MIT — use, modifique e distribua à vontade.
