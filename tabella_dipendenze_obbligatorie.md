# Tabella dipendenze obbligatorie — Quelo Palinsesto Radio

Pronto da copiare (o adattare) nella pagina GitHub / README.

**Idea:** il software è portable su Linux (varie distro e architetture) **senza modificare il codice**, se queste dipendenze sono presenti con i **nomi attesi** e le **versioni minime**.  
I nomi pacchetto (`apt`, `dnf`, `pacman`, Slackware, …) cambiano: qui si indicano i **componenti runtime**, non la procedura di installazione.

**Ambiente di riferimento (testato):** Debian 13 · Python 3.13 · GStreamer 1.26 · Pulse/`pactl` 17 · FFmpeg 7.1 · PyQt6 6.9 / Qt 6.8.

---

## Dipendenze obbligatorie (runtime)

| Componente | Nome atteso / come verificarlo | Versione minima | Obbligatoria | Modalità | Note |
|------------|--------------------------------|-----------------|--------------|----------|------|
| **Python 3** | `python3 --version` | **≥ 3.10** | Sì | Desktop e `--web-only` | Anche se “già installato”: controllare la versione. Sotto 3.10 non è supportato. Testato: 3.13. |
| **sqlite3** (modulo Python) | `python3 -c "import sqlite3; print(sqlite3.sqlite_version)"` | incluso in Python ≥ 3.10 (SQLite **≥ 3.35** consigliato) | Sì | Entrambe | Di solito preinstallato con Python. Se l’import fallisce, manca il supporto sqlite nella build Python. |
| **PyGObject (gi)** | `python3 -c "import gi"` | allineato a GStreamer 1.x della distro | Sì | Entrambe | Binding Python → GStreamer. |
| **GStreamer 1.x** | `gst-launch-1.0 --version` (o via `gi`) | **≥ 1.18** (consigliato **≥ 1.20**) | Sì | Entrambe | Player, VU, FILE/PLAYLIST/LIVE/LINK, mixer taps. Testato: 1.26. |
| **Plugin GStreamer: base** | elementi tipo `audioconvert`, `volume`, `playbin` | come GStreamer sopra | Sì | Entrambe | Pacchetto tipo `gstreamer1.0-plugins-base` / `gst-plugins-base`. |
| **Plugin GStreamer: good** | elemento `level` (VU) | come sopra | Sì | Entrambe | Pacchetto tipo `…-plugins-good`. |
| **Plugin GStreamer: bad** | es. `curlhttpsrc` (stream LINK HTTP/HTTPS) | come sopra | Sì (per LINK) | Entrambe | Senza di questo, i clip LINK possono fallire. |
| **Plugin GStreamer: ugly** | codec aggiuntivi | come sopra | Consigliata / spesso necessaria | Entrambe | Decode di formati comuni in radio. |
| **Plugin GStreamer: libav** | decode ampia gamma (via libav/FFmpeg) | come sopra | Fortemente consigliata | Entrambe | Senza, molti file audio non partono. |
| **Plugin GStreamer: Pulse** | elementi `pulsesink`, `pulsesrc` | come sopra | Sì | Entrambe | Uscita audio e ingressi LIVE. |
| **Server audio Pulse-compatibile** | sessione attiva per l’utente che lancia l’app | PulseAudio **≥ 14** *oppure* PipeWire con **pipewire-pulse** (PW **≥ 0.3.40** consigliato) | Sì | Entrambe | Deve girare nella sessione dell’utente (non “audio di root” separato). |
| **`pactl`** | `pactl --version` / `command -v pactl` | API Pulse classica (testato con **17.x**) | Sì | Entrambe (MIXER e monitor) | Elenco ingressi, mute, volume, monitor uscita. Se sulla distro il binario ha **altro nome**, creare un **symlink** chiamato `pactl` sul `PATH` (non un alias di shell). |
| **`ffmpeg`** | `ffmpeg -version` | **≥ 4.4** (consigliato **≥ 5.0**) | Sì | Entrambe | Analisi/peak file; ascolto remoto browser (`--web-only`). Testato: 7.1. |
| **`ffprobe`** | `ffprobe -version` | stessa famiglia di `ffmpeg` | Sì | Entrambe | Durata e metadati FILE/PLAYLIST. Di solito nello stesso pacchetto di FFmpeg. |
| **PyQt6** | `python3 -c "from PyQt6.QtCore import PYQT_VERSION_STR; print(PYQT_VERSION_STR)"` | **≥ 6.4** | Sì **solo GUI desktop** | Solo senza `--web-only` | Con `--web-only` **non** serve. Testato: PyQt 6.9 / Qt 6.8. |

---

## Dipendenze consigliate (non bloccano l’avvio base)

| Componente | Perché | Note |
|------------|--------|------|
| **glib-networking** (o stack TLS della distro) | Stream LINK in HTTPS | Senza TLS, HTTPS può fallire. |
| **AAC / fdkaac** (plugin GStreamer) | File AAC | Allineato allo stack Audiomaker/Quelo. |
| **Bluetooth Pulse** (modulo) | Ingressi BT | Solo se servono microfoni/cuffie BT. |
| **zathura** (+ backend PDF) o **xdg-open** | Aprire il manuale PDF dal desktop | Solo comodità; il PDF web si apre dal browser. |

---

## Modalità a colpo d’occhio

| Modalità | Cosa serve in più / in meno |
|----------|-----------------------------|
| **Desktop (PyQt)** | Tutto l’obbligatorio **incluso PyQt6** |
| **`--web-only`** | Tutto l’obbligatorio **tranne PyQt6**; browser per l’interfaccia |

---

## Architetture

Stesso elenco su **x86_64**, **aarch64/ARM**, ecc., purché per quell’architettura esistano i binari/librerie sopra.  
Non c’è codice legato a “solo amd64”.

---

## Nomi diversi sulla tua distro (symlink)

Il programma cerca sul `PATH` i comandi **`pactl`**, **`ffmpeg`**, **`ffprobe`** (e lancia `python3`).

Se nella tua distro un comando obbligatorio ha un **nome diverso**, **non modificare Quelo**: crea un **symlink** (o un piccolo wrapper eseguibile) con il nome atteso in una directory del `PATH`, ad esempio:

```bash
sudo ln -s /usr/bin/NOME_REALE /usr/local/bin/pactl
```

**Non usare un `alias` di shell**: gli alias non valgono per i processi avviati dall’applicazione.

---

## Verifica rapida (copia-incolla)

```bash
python3 --version
python3 -c "import sqlite3, gi; gi.require_version('Gst','1.0'); from gi.repository import Gst; Gst.init(None); print('GStreamer', Gst.version_string()); print('sqlite', sqlite3.sqlite_version)"
command -v pactl && pactl --version | head -1
command -v ffmpeg && ffmpeg -version | head -1
command -v ffprobe && ffprobe -version | head -1
# solo se usi la GUI desktop:
python3 -c "from PyQt6.QtCore import PYQT_VERSION_STR; print('PyQt6', PYQT_VERSION_STR)"
```

Se tutti i comandi rispondono e le versioni rispettano i minimi, puoi scaricare Quelo Palinsesto Radio, impostare i permessi sulle cartelle dati, e avviarlo **senza patch al codice**.

---

## Dati a runtime (non pacchetti)

| Voce | Default |
|------|---------|
| Database | `/media/quelo-home/.palinsesto.db` se QUELO-HOME è montata, altrimenti `$HOME/.palinsesto.db` |
| Porta web | `8890` |
| Avvio | dalla sessione utente con Pulse/PipeWire attivo |

---

*File di progetto: `tabella_dipendenze_obbligatorie.md` — da allineare a `dipendenze.txt` (nomi pacchetto Debian) quando si aggiornano le dipendenze.*
