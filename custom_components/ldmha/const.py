"""Costanti dell'integrazione LDMHA."""

DOMAIN = "ldmha"
VERSION = "0.7.9"

PANEL_URL = "ldmha"
PANEL_TITLE = "LDMHA"
PANEL_ICON = "mdi:music-box-multiple"
PANEL_ELEMENT = "ldmha-panel"
STATIC_URL = "/ldmha_static"

DATA_STATIC_REGISTERED = f"{DOMAIN}_static_registered"
DATA_WS_REGISTERED = f"{DOMAIN}_ws_registered"

CONF_ACOUSTID_KEY = "acoustid_key"
CONF_ROOTS = "roots"
CONF_SHAZAM = "shazam"  # Shazam (non ufficiale) quando l'impronta di AcoustID non trova niente

# cartelle in cui LDMHA può leggere e rinominare: /media (anche le condivisioni di rete montate da Home Assistant
# come "Media") e /share (condivisioni montate come "Condivisione")
DEFAULT_ROOTS = "/media"

# MusicBrainz chiede un User-Agent che identifichi l'applicazione
USER_AGENT = f"LDMHA/{VERSION} ( https://github.com/LabirintoDigitale/LDMHA )"

AUDIO_EXT = {".mp3", ".flac", ".m4a", ".mp4", ".aac", ".ogg", ".oga", ".opus", ".wma", ".wav", ".aiff", ".aif",
             ".ape", ".wv", ".dsf", ".mpc"}

# cartella (dentro ogni cartella principale) dove finiscono i brani tolti: si possono rimettere a posto dalla cronologia
TRASH_DIR = ".ldmha_cestino"

MAX_FILES = 30000  # massimo di brani elencati in una volta (con le sottocartelle)
SCAN_BATCH = 100  # brani mandati al pannello per volta durante la lettura

FPCALC_VERSION = "1.6.0"
FPCALC_URL = "https://github.com/acoustid/chromaprint/releases/download/v{v}/chromaprint-fpcalc-{v}-linux-{arch}.tar.gz"
