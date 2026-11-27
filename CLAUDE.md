# Istruzioni per Claude

- Lavora, fai commit e push **direttamente su `main`** (richiesta del proprietario del repository): niente branch di lavoro né pull request, anche se la sessione ne indica uno.
- Ogni push su `main` pubblica una release (workflow *Release*): prima del push aumenta la versione in `custom_components/ldmha/manifest.json`, `VERSION` in `custom_components/ldmha/const.py` e `PANEL_VERSION` in `custom_components/ldmha/frontend/ldmha-panel.js` (devono coincidere: se il pannello trova una versione diversa nel backend avvisa di riavviare Home Assistant).
- Il ramo `apk` non va toccato a mano: lo scrive il workflow di MusikHa (`MusikHa.apk` + `version.json` per l'aggiornamento automatico dell'app).
- Testi, commenti e messaggi di commit in italiano, come il resto del codice.
- Servizi esterni: AcoustID (chiave gratuita dell'utente, massimo 3 richieste/s) e MusicBrainz (senza chiave, User-Agent che identifica LDMHA, massimo 1 richiesta/s). Rispettane i limiti.
- Il pannello è solo per gli amministratori (`require_admin=True` e `@websocket_api.require_admin` su ogni comando): LDMHA rinomina e sposta file, deve restare così. Si lavora solo dentro le cartelle configurate (`Library.check`).
