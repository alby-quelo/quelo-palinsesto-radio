Quelo-palinsesto-radio — sorgente standalone (spin-off da Quelo Audiomaker)
==========================================================================

Copia di sviluppo: la distro Audiomaker resta intatta e continua a includere
l’app. Qui NON si produce ISO.

Layout
------
  bin/quelo-palinsesto-radio     launcher
  bin/quelo-manuale-palinsesto   apre docs/manuale_palinsesto.pdf
  share/quelo-palinsesto-radio/  codice Python (+ engine/web)
  desktop/                       .desktop di riferimento
  docs/                          manuale PDF + icona

Avvio desktop (PyQt)
--------------------
  ./bin/quelo-palinsesto-radio

Avvio via browser (come Anti Bianco, modalità headless)
-------------------------------------------------------
  ./bin/quelo-palinsesto-radio --web-only

  Poi apri nel browser (default porta 8890, bind LAN):
    http://127.0.0.1:8890/
    http://<IP-LAN>:8890/

  Opzioni:
    --port 8890
    --bind 0.0.0.0          (LAN; usa 127.0.0.1 per solo locale)
    --web-only

  Funzioni web (prima versione):
    Start/Stop, VU, volume, timeline settimanale, CRUD clip,
    browser file, SETTING (silence-gate / ANTI BIANCO / aspetto),
    MIXER ingressi Pulse, cambio porta/bind.

  Nota: desktop Qt e --web-only sono processi separati; non avviarli
  insieme sullo stesso DB se entrambi devono pilotare l’audio.

Dipendenze tipiche (Debian)
---------------------------
  python3 python3-pyqt6 python3-gi gir1.2-gstreamer-1.0
  gstreamer1.0-plugins-base gstreamer1.0-plugins-good
  gstreamer1.0-plugins-bad gstreamer1.0-libav gstreamer1.0-pulseaudio
  pulseaudio (o pipewire-pulse)

Database
--------
  Come in distro: /media/quelo-home/.palinsesto.db se QUELO-HOME è montata,
  altrimenti $HOME/.palinsesto.db
