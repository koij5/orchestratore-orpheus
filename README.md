# Orpheus — lezioni e materiali di studio

Orpheus è un progetto **in configurazione** per trascrivere file audio caricati dall'utente in Google Drive e archiviare il testo nella materia dichiarata nel nome file. Il notebook Kaggle e il workflow GitHub non sono ancora stati collegati e verificati end-to-end; la pipeline è fail-closed e non va attivata su audio reali finché non è completata la prova.

## Flusso previsto

1. Registra e carica **un file per singola lezione**. Puoi avviare e fermare tu la registrazione; non serve configurare gli orari scolastici.
2. Rinomina il file prima del caricamento usando il formato `YYYY-MM-DD_HH-MM_materia.estensione`, ad esempio `2026-11-06_08-00_biologia.m4a`. Materie riconosciute: `biologia`, `chimica`, `fisica`, `inglese`, `letteratura`, `purgatorio`. È ammesso anche il secondo nel timestamp: `2026-11-06_08-00-15_letteratura.m4a`.
3. Carica il file nella cartella Drive `audio_incoming`. L'orchestratore può quindi inviare un file alla volta al notebook Kaggle privato, che lo trascrive con Whisper e crea un `.txt` con timestamp e metadati che legano il transcript all'ID audio.
4. A verifica del transcript l'orchestratore smista tutto il testo alla sottocartella `lezioni` della materia nel nome. Non classifica il parlato con Gemini, non indovina dal contenuto e non usa l'orario. Se mancano materia/data/ora nel nome, se il nome è ambiguo o il transcript non è verificabile, il file resta per revisione; non viene dichiarato completato.

Un'unica registrazione che copre più lezioni non è supportata dal routing automatico: l'avvio manuale della registrazione non rivela i confini delle materie. Se non vuoi file separati, serve uno strumento di revisione manuale che assegni intervalli del transcript; non va usato Gemini API per l'uso consumer/personale o in contesti di minori senza avere prima una base consentita. Al momento tale UI di revisione segmentata non è implementata.

## Stato e limiti

- Il workflow GitHub remoto è stato **disabilitato**; la versione locale è manuale, senza cron. Rimane inattivo fino a configurazione, test autorizzato e abilitazione esplicita.
- Il worker Kaggle ora è presente nel repository ed è coperto da test mock offline, ma non è stato ancora eseguito contro l'account Kaggle dell'utente. Servono slug Kaggle reali, GPU/quota disponibili, dataset delle credenziali privato e verifica OAuth/permessi.
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

**Risultato della suite locale al 6 novembre 2026: 79 test superati**, con PDF sintetici e mock di Google/Kaggle/Gemini. I test non verificano credenziali reali, rete, permessi, GPU, quote Kaggle, accesso alle cartelle o qualità dei transcript: è ancora necessario un test remoto autorizzato.
