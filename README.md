# LDMHA

Integrazione per Home Assistant che **mette in ordine la musica**: un pannello nella barra laterale (visibile solo agli amministratori) per riconoscere i brani, correggere i tag, rinominare i file e togliere i doppioni.

- **Riconosce i brani dall'audio**: impronta con [Chromaprint](https://acoustid.org/chromaprint) (`fpcalc`, scaricato da solo al primo uso) cercata su [AcoustID](https://acoustid.org), poi album, anno e numero di traccia da [MusicBrainz](https://musicbrainz.org). Funziona anche con file chiamati `traccia01.mp3` e senza tag.
- **Se l'audio non basta** cerca su MusicBrainz per nome del file e tag, oppure a mano (*Cerca…*).
- **Tag e nome nuovo**: scegli fra le proposte, correggi i campi se serve e premi *Applica*: scrive i tag (titolo, artista, album, artista dell'album, anno, numero, ID MusicBrainz) e rinomina il file con lo schema che vuoi, per esempio `{artist} - {title}` oppure `{artist}/{album}/{track} - {title}` (con le sottocartelle).
- **Tutti insieme**: *Riconosci tutti* passa la cartella (anche con le sottocartelle), *Applica i sicuri* sistema i brani riconosciuti dall'audio con sicurezza almeno del 90%.
- **Doppioni**: lo stesso brano in più file viene segnalato; *Cestino* lo sposta in `.ldmha_cestino` dentro la cartella della musica.
- **Si può sempre tornare indietro**: ogni modifica (tag, nome, cestino) resta nella *Cronologia* e si annulla con un tocco.

Il pannello regge anche librerie grandi: albero delle cartelle a sinistra, tabella compatta (ordinabile, con ricerca e filtri: da riconoscere, sicuri, incerti, doppioni…), selezione multipla (con Maiusc per un intervallo) e scheda del brano a destra. Con la scheda aperta: ↑ ↓ per passare al brano vicino, Invio per applicare e andare al successivo.

**Archivio**: i brani letti (con i tag) e i riconoscimenti restano salvati in `ldmha/ldmha.db` nella configurazione di Home Assistant. Riaprendo una cartella già letta l'elenco compare subito; *⟳ Aggiorna* la rilegge dal disco aprendo solo i file nuovi o cambiati e toglie quelli spariti. Dalla scheda *Registro* si può svuotare l'archivio (i file non vengono toccati).

Dopo le modifiche, Music Assistant vede i nomi e i tag nuovi alla prossima sincronizzazione della libreria.

## Installazione

1. HACS → menu ⋮ → *Repository personalizzati* → `https://github.com/LabirintoDigitale/LDMHA`, categoria *Integrazione* → installa **LDMHA** e riavvia Home Assistant.
2. *Impostazioni → Dispositivi e servizi → Aggiungi integrazione → LDMHA*.
3. **Chiave AcoustID** (gratuita): crea un'applicazione su https://acoustid.org/new-application e copia la chiave. Senza chiave i brani si cercano solo per nome e tag.
4. **Cartelle della musica**, separate da virgola (predefinita `/media`). LDMHA legge e rinomina solo lì dentro.

### Musica sul NAS

Il provider SMB di Music Assistant collega il NAS solo dentro Music Assistant: Home Assistant (e quindi LDMHA) non lo vede. Monta la condivisione in *Impostazioni → Sistema → Archiviazione → Aggiungi archiviazione di rete* con uso **Media**: compare sotto `/media/<nome>` e LDMHA la vede (aggiungila alle cartelle se non hai messo `/media`). La condivisione deve essere montata in lettura **e scrittura** per poter rinominare.

## MusikHa

Il ramo [`apk`](https://github.com/LabirintoDigitale/LDMHA/tree/apk) contiene l'APK di **MusikHa** (lettore di Music Assistant a tutto schermo per Android), aggiornato a ogni compilazione:
https://github.com/LabirintoDigitale/LDMHA/raw/apk/MusikHa.apk
