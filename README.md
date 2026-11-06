# Orpheus — lezioni e materiali di studio

Orpheus è un progetto **in configurazione** per trascrivere integralmente una registrazione giornaliera caricata su Google Drive e poi suddividere il TXT master per materia usando classificazione locale del testo su Kaggle. Il notebook Kaggle e il workflow GitHub non sono ancora stati collegati e verificati end-to-end; la pipeline è fail-closed e non va attivata su audio reali finché non è completata la prova.

## Flusso previsto

1. Registra l'intera giornata in **un solo file di durata variabile** e avvia/ferma manualmente la registrazione. Non servono gli orari scolastici.
2. Carica il file nella cartella Drive `audio_incoming`; il nome non deve contenere la materia. La data di archiviazione viene ricavata da `createdTime` del file Drive, convertita all'ora di Roma.
3. Il notebook Kaggle privato trascrive l'audio completo con Whisper e conserva il TXT timestampato integrale come master nella cartella delle trascrizioni.
4. Solo dopo la trascrizione, un classificatore locale zero-shot analizza il testo in finestre di circa 60 secondi e genera un piano di intervalli. L'orchestratore copia nelle cartelle `lezioni` soltanto gli intervalli assegnati con sufficiente confidenza a `biologia`, `chimica`, `fisica`, `inglese`, `letteratura` o `purgatorio`. Gli intervalli riconosciuti come altre materie sono esclusi dai file per materia ma restano nel master completo. Gli intervalli incerti bloccano il completamento e richiedono revisione: non vengono assegnati a caso.

La registrazione audio originale non viene divisa né cancellata da questa pipeline. Il classificatore opera sul TXT, non analizza l'audio e non invia le trascrizioni a Gemini. Il modello locale è un candidato multilingue con supporto italiano, ma non è stato collaudato su lezioni reali: accuratezza e confini delle finestre vanno verificati prima dell'uso. Il sistema al momento usa un titolo generico per le cartelle lezione basato sulla materia; non genera ancora un titolo argomento affidabile.

## Stato e limiti

- Il workflow GitHub remoto è stato **disabilitato**; la versione locale è manuale, senza cron. Rimane inattivo fino a configurazione, test autorizzato e abilitazione esplicita.
- Il worker Kaggle e il classificatore locale sono presenti nel repository e coperti da test mock offline, ma non sono stati ancora eseguiti contro l'account Kaggle dell'utente. La prima esecuzione deve scaricare gli asset del modello NLI dal suo repository pubblico; Kaggle richiede Internet e la GPU gratuita può essere in coda o non disponibile. Servono slug Kaggle reali, dataset privato credenziali e verifica OAuth/permessi.
- Nessun file è stato elaborato su Drive durante lo sviluppo. Nessun segreto è stato scritto nella repository. Cartelle preesistenti di materia non vengono rinominate o eliminate.
- L'app del registratore e l'upload su Drive sono manuali: il progetto non controlla il microfono del telefono e non cancella registrazioni locali.
- Le registrazioni possono includere compagni o insegnanti, anche minorenni. Prima di registrarle o condividerle controlla le regole scolastiche e le autorizzazioni necessarie, limita gli accessi e non pubblicare audio, transcript o libri.
- Gli strumenti `book_tools` suddividono PDF autorizzati in locale e offrono funzioni Gemini opt-in separate. Non scaricano libri, non aggirano DRM e non sovrascrivono i PDF originali.

Le condizioni Gemini API aggiornate al 6 novembre 2026 richiedono almeno 18 anni, destinano AI Studio/API a sviluppatori per finalità professionali o aziendali e non per uso consumer; vietano inoltre client rivolti o probabilmente accessibili a minori e prevedono condizioni territoriali. **L'orchestratore non invia trascrizioni a Gemini.** Per lo studio personale descritto non attivare i comandi Gemini di `book_tools` senza un chiarimento applicabile. L'app consumer Gemini è un servizio distinto.

## Configurazione

Per collegare notebook, dataset e credenziali private segui la [guida passo-passo Kaggle e GitHub Actions](SETUP_KAGGLE_GITHUB.md) e il [README del worker Kaggle](kaggle_kernel/README.md). Non serve condividere qui il link al notebook: lo slug `owner/kernel` si ricava dal suo URL e si conserva direttamente come GitHub secret. Il dataset montato dal worker deve essere privato e contenere `config.json` e `credentials.json`; le credenziali Drive devono essere OAuth utente autorizzate, accessibili al medesimo progetto/identità OAuth usato anche dall'orchestratore. La chiave API Kaggle per GitHub è diversa dalle credenziali Google Drive.

GitHub Actions richiede autorizzazioni repository per aggiungere i secrets. Il gate `ORPHEUS_WORKER_CONTRACT_VERIFIED` resta inattivo finché non si è verificato il worker privato reale, i metadati e i permessi; non impostarlo ora. Dopo la configurazione, esegui una prova breve con registrazione autorizzata e controlla manualmente il file prodotto. Solo dopo valuta `Run workflow` manuale una volta. Non abilitare il cron prima del collaudo.

## Libri e materiali

Per suddividere un PDF in capitoli, prepara un manifest con intervalli di pagine fisiche PDF e segui [le istruzioni di `book_tools`](book_tools/README.md). La suddivisione è locale, conserva le pagine complete e non modifica l'originale. OCR, estrazione automatica dell'indice e sincronizzazione dei libri con Drive non sono implementati. Citazioni o materiali generati sono bozze da verificare.

## Test offline

Dalla radice del repository:

```bash
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
```

La suite offline copre parser, segmentazione e routing con classifier/API mockati. Non sostituisce una prova su una registrazione autorizzata. I test non verificano credenziali reali, rete, permessi, GPU, quote Kaggle, accesso alle cartelle o qualità dei transcript: è ancora necessario un test remoto autorizzato.
