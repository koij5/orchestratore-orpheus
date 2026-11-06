# Worker Kaggle: Drive → Whisper → Drive

Lo script legge **solo** gli audio supportati già presenti nella cartella Drive di lavorazione, li trascrive con OpenAI Whisper `large-v3` e crea transcript grezzi `.txt` in un'altra cartella Drive. L'audio originale non viene modificato, spostato o cancellato dal worker. Demucs **non** è richiesto né viene eseguito: è uno strumento opzionale di separazione, spesso orientato alla musica.

## 1. Dataset Kaggle privato e configurazione

Preparare **fuori dal repository** un dataset Kaggle privato con slug `orpheus-private-config`, contenente questi due file:

```text
/kaggle/input/orpheus-private-config/
├── config.json
└── credentials.json
```

Il percorso predefinito letto dallo script è esattamente `/kaggle/input/orpheus-private-config/config.json`. Se il dataset è montato altrove, usare `ORPHEUS_CONFIG_PATH` con il percorso **assoluto** del `config.json` effettivo; adattare anche `credentials_path` nel JSON. La variabile deve essere effettivamente impostata nell'ambiente di esecuzione Kaggle: impostarla solo nel workflow GitHub **non** la trasferisce automaticamente al processo remoto. In alternativa, mantenere slug e percorso predefiniti. Verificare il percorso di mount mostrato da Kaggle per la propria esecuzione.

Contenuto esatto di `config.json` (gli ID sono da sostituire; non contengono chiavi):

```json
{
  "credentials_path": "/kaggle/input/orpheus-private-config/credentials.json",
  "processing_folder_id": "INSERISCI_ID_CARTELLA_PROCESSING",
  "transcripts_folder_id": "INSERISCI_ID_CARTELLA_TRASCRIZIONI_GREZZE",
  "shared_drive_id": null,
  "language": "it",
  "require_gpu": true,
  "device": "cuda",
  "bootstrap_dependencies": true
}
```

| Campo | Tipo e significato |
| --- | --- |
| `credentials_path` | **Obbligatorio**: percorso assoluto al file credenziali privato nel dataset montato. Il worker non usa credenziali incorporate o ADC. |
| `processing_folder_id` | **Obbligatorio**: ID della cartella Drive in cui l'orchestratore ha già spostato gli audio. |
| `transcripts_folder_id` | **Obbligatorio**: ID della cartella di destinazione dei `.txt` grezzi; deve essere diverso dal precedente. |
| `shared_drive_id` | Opzionale, default `null`. Per My Drive personale senza Drive condivisi, lasciare `null`. Se valorizzato, è l'ID del **Drive condiviso**, non della cartella: limita scansione e ricerca duplicati a quel Drive, che deve quindi contenere anche eventuali archivi. |
| `language` | Opzionale, default `"it"`; codice lingua riconosciuto da Whisper, oppure `null` per rilevamento automatico. |
| `require_gpu` | Opzionale, booleano, default `true`: se CUDA non è disponibile il worker termina con errore, senza ripiegare su CPU. |
| `device` | Opzionale, default `"cuda"`; valori `"cuda"`, `"auto"` o `"cpu"`. `"cuda"` richiede sempre CUDA. Per consentire il ripiego CPU usare `"auto"` e `require_gpu: false`; per forzare CPU usare `"cpu"` e `require_gpu: false`. |
| `bootstrap_dependencies` | Opzionale, booleano, default `true`: installa con pip solo i pacchetti mancanti. Se `false`, tutte le dipendenze devono essere già presenti. |

Chiavi sconosciute, valori non validi e percorsi credenziali relativi sono rifiutati. Con `shared_drive_id: null` si cerca nel corpus `allDrives` accessibile, inclusi My Drive e Drive condivisi. La ricerca usa `supportsAllDrives` e `includeItemsFromAllDrives`, gestisce tutte le pagine e interrompe l'operazione se Drive segnala `incompleteSearch`, evitando di considerare affidabile un risultato parziale.

## 2. Scegliere le credenziali

**My Drive personale senza Drive condivisi: preferire OAuth utente.** I service account non hanno una propria quota di archiviazione Drive e non possono essere proprietari dei file: il loro upload su My Drive può fallire anche quando hanno accesso alla cartella. Con OAuth, i file sono creati dall'utente autorizzato e usano la sua quota. Abilitare l'API Google Drive nel progetto Cloud, creare un client OAuth, ottenere con consenso un refresh token per `https://www.googleapis.com/auth/drive` e preparare nel dataset privato `credentials.json` con questa struttura (i placeholder non sono credenziali valide):

```json
{
  "type": "authorized_user",
  "client_id": "INSERISCI_CLIENT_ID",
  "client_secret": "INSERISCI_CLIENT_SECRET",
  "refresh_token": "INSERISCI_REFRESH_TOKEN",
  "token_uri": "https://oauth2.googleapis.com/token",
  "scopes": ["https://www.googleapis.com/auth/drive"]
}
```

L'autorizzazione per lo scope Drive deve essere **realmente concessa dall'utente** durante il flusso OAuth offline: scrivere lo scope nel file non concede permessi. Il token deve essere valido e rinnovabile; scadenza/revoca o restrizioni del progetto OAuth possono impedirne il rinnovo. Il worker usa `google.oauth2.credentials.Credentials.from_authorized_user_file(..., scopes=[...])` e non avvia un flusso interattivo. Creare il file localmente con un normale flusso OAuth per applicazioni desktop, non inserirlo nel repository e non stamparlo.

In alternativa il file `credentials.json` può essere il JSON standard di un **service account** Google con `"type": "service_account"`, usato tramite `Credentials.from_service_account_file`. Il service account deve avere accesso in lettura alla sorgente e in scrittura alla destinazione, e una modalità valida di archiviazione/quota (tipicamente un Drive condiviso dove sia membro con i permessi necessari). Il worker usa lo scope completo `https://www.googleapis.com/auth/drive`; l'account scelto deve poter leggere anche gli eventuali transcript già archiviati.

**Sicurezza:** il dataset **e** il kernel devono restare privati; un refresh token e una chiave di service account sono segreti riutilizzabili con privilegi elevati. Limitare accessi e collaboratori, non metterli in Git/GitHub, metadata, output, log, prompt o variabili esposte, e revocarli/ruotarli se compromessi. Non è necessario inserire la chiave in `config.json`: il campo `credentials_path` contiene solo un percorso. Le `appProperties` di Drive sono private per applicazione: per leggerle da worker e orchestratore, usare credenziali/identità applicativa compatibili dello stesso progetto Google Cloud e concedere accesso ai medesimi file; verificare questa visibilità nell'ambiente reale. Scope, condivisioni e permessi non vengono creati dal worker.

## 3. Metadata e runtime Kaggle

`kernel-metadata.json` descrive uno **script** Python privato (`code_file: trascrivi.py`), con GPU e Internet abilitati. Prima del push, il workflow o la configurazione deve sostituire `replace-kaggle-username` con l'username reale sia in `id` (`username/orpheus-whisper`) sia in `dataset_sources` (`username/orpheus-private-config`); modificare lo slug se necessario e allineare i percorsi del JSON. I placeholder non identificano risorse reali. Questo componente non configura, lancia o pubblica il workflow.

Il bootstrap esplicito installa, se mancanti, `google-auth==2.38.0`, `google-api-python-client==2.167.0` e `openai-whisper==20240930`, usando `python -m pip`; PyTorch è una dipendenza di Whisper o può essere già disponibile nell'immagine Kaggle. Il bootstrap non aggiorna i pacchetti già importabili e sopprime l'output di pip. Deve essere disponibile anche l'eseguibile **ffmpeg** nell'immagine Kaggle: se manca il worker fallisce; non tenta installazioni di sistema. La prima esecuzione scarica i pesi `large-v3` tramite Whisper e richiede Internet, tempo, spazio e memoria GPU sufficienti. Non sono richiesti Demucs né i relativi pesi. Se le librerie esistenti sono incompatibili, disabilitare o gestire il bootstrap non basta: occorre risolvere l'ambiente Kaggle prima di rilanciare.

## 4. Contratto Drive e output

* Entrano in elaborazione solo file con estensione `.mp3`, `.m4a`, `.wav`, `.flac`, `.ogg`, `.opus`, `.webm`, `.aac`, `.aiff`, `.aif` o `.amr` **e** MIME in `audio/mpeg`, `audio/mp3`, `audio/mp4`, `audio/x-m4a`, `audio/m4a`, `audio/wav`, `audio/wave`, `audio/x-wav`, `audio/vnd.wave`, `audio/flac`, `audio/x-flac`, `audio/ogg`, `audio/opus`, `audio/webm`, `audio/aac`, `audio/aiff`, `audio/x-aiff`, `audio/amr` oppure `application/octet-stream`. Estensione e MIME sono confrontati senza distinzione maiuscole/minuscole. Gli altri file vengono ignorati, inclusi cartelle, scorciatoie e video. Questi metadati non garantiscono che il contenuto binario sia audio valido: eventuali errori di decodifica fanno fallire il file.
* Il file remoto `lezione.mp3` produce `lezione.txt`. Ogni segmento inizia con `[HH:MM:SS - HH:MM:SS] `; le ore comprendono durate oltre 59 minuti. Il testo di ogni segmento Whisper è copiato integralmente, senza `strip`, riscrittura, traduzione o correzione; i timestamp e i separatori di riga vengono aggiunti attorno ai segmenti. Eventuali a-capo interni al testo sono conservati. Risultati vuoti e segmenti/timestamp non validi sono errori, non transcript di successo.
* Ogni file creato ha `appProperties`: `orpheus_source_id` uguale all'ID Drive dell'audio; `orpheus_kind` uguale a `raw_transcript`; `orpheus_original_name` uguale al nome audio originale completo. Il nome originale non viene troncato: se una proprietà supera il limite Drive di 124 byte UTF-8 per chiave più valore, il file fallisce e non viene caricato. Nomi con barre, backslash o caratteri di controllo sono rifiutati.
* Prima di ogni upload viene cercato nel corpus visibile un transcript **non cestinato** con lo stesso source ID e kind, anche se spostato in un'altra cartella. I nomi uguali non sono considerati duplicati: due audio con ID diversi vengono trattati separatamente. File cestinati non contano come output disponibili. L'account deve avere accesso alla cartella di archivio per rilevare output già spostati.
* La ricerca per proprietà seguita dalla creazione non è una transazione atomica Drive: occorre **una sola esecuzione worker alla volta**. In caso di errore di rete ambiguo durante la creazione o di indicizzazione non ancora aggiornata, la creazione può essere avvenuta anche se il worker segnala errore; prima di un retry verificare manualmente in Drive che non esista già il transcript con lo stesso source ID. Il worker non ritenta automaticamente una `create` ambigua. Non c'è garanzia di esattamente-una-volta in esecuzioni concorrenti o subito dopo errori ambigui.

**Compatibilità orchestratore:** l'orchestratore corrente collega i file solo tramite `orpheus_source_id` e non confronta gli stem dei nomi. Per archiviare la trascrizione in una materia richiede inoltre che il nome dell'audio sia `YYYY-MM-DD_HH-MM[_SS]_<materia>.<estensione>` con un unico valore esatto fra `biologia`, `chimica`, `fisica`, `inglese`, `letteratura` e `purgatorio`. Registra e carica un file per lezione. Una registrazione giornaliera, un nome senza materia o una materia ambigua resta in revisione; l'orchestratore non invia questo transcript a Gemini e non deduce la materia dal solo testo.

## 5. Errori, log e pulizia

Il worker non marca né sposta l'audio come completato. Se download, modello, trascrizione o upload falliscono, continua con gli altri file, non dichiara successo per il file fallito e termina con codice **1** se c'è almeno un errore; un errore globale di configurazione, dipendenze, CUDA o scansione restituisce anch'esso **1**. Il codice **0** indica che non ci sono stati errori; una scansione vuota o un transcript già presente sono normali. L'orchestratore gestisce separatamente gli audio rimasti in processing e gli eventuali retry.

I messaggi controllati sono scritti su stderr e in `/kaggle/working/orpheus-worker.log`: riportano conteggi e fase del guasto, mai contenuto di configurazione, credenziali, nomi file, transcript, stringhe di eccezione o traceback. Il log non è un archivio di audio o transcript; proteggerlo comunque come output del kernel privato. L'audio viene scaricato con un nome locale fisso in una sottocartella temporanea casuale, non derivata dal nome remoto: non è possibile usare il nome Drive per attraversare percorsi locali. La pulizia elimina soltanto la directory temporanea appena creata dal singolo file, anche in caso di errore; non elimina altri file locali né file Drive. I pesi in cache e il log non sono considerati temporanei del singolo audio.

## 6. Test offline

Dalla radice del repository, senza credenziali né connessioni a Drive/Kaggle:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -p 'test_kaggle_worker.py' -v
```

I test usano `unittest` e mock per credenziali, API e modello. Non verificano permessi effettivi, disponibilità GPU/ffmpeg, quote, visibilità delle proprietà tra applicazioni, comportamento di rete/indicizzazione o esecuzione reale su Kaggle: tali aspetti richiedono una verifica separata autorizzata.
