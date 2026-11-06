# Collegare il tuo notebook Kaggle privato a GitHub Actions

Questo documento serve a collegare il notebook già esistente senza rendere pubblici notebook, dataset o credenziali. **Non incollare mai token o chiavi in chat, GitHub Issues o file del repository.** Il workflow è attualmente disabilitato da GitHub e deve restare tale fino al test manuale.

## 1. Trova i due slug nella UI Kaggle

Apri il notebook esistente su Kaggle e controlla che sia **Private**. Copia dalla barra degli indirizzi l'URL, che normalmente ha forma `https://www.kaggle.com/code/UTENTE/NOME-NOTEBOOK`. Il valore da conservare localmente come `KAGGLE_KERNEL_SLUG` è `UTENTE/NOME-NOTEBOOK`. Non occorre inviarmelo.

Individua anche il dataset privato che contiene la configurazione Drive (dovrebbe contenere `config.json` e `credentials.json`). L'URL normalmente ha forma `https://www.kaggle.com/datasets/UTENTE/NOME-DATASET`; il relativo valore è `UTENTE/NOME-DATASET`, da usare come `KAGGLE_DATASET_SLUG`. Verifica che sia privato e che il notebook sia autorizzato a leggerlo. La pipeline preparata legge le credenziali Drive da quel dataset montato, non dal notebook e non da GitHub. Se le credenziali sono invece solo nelle Kaggle Secrets o incorporate direttamente nel notebook, non sono accessibili al worker così com'è: non copiarle nel codice. Sposta in un dataset privato i due file della configurazione seguendo [README Kaggle](kaggle_kernel/README.md).

La nuova implementazione usa un notebook Kaggle di tipo **script**, `trascrivi.py`, con metadati `kernel-metadata.json` e il dataset configurato come input. Non è un aggiornamento garantito del notebook notebook-style già esistente: prima di collegare slug e script, controlla in Kaggle che il notebook possa essere aggiornato come script. Se il notebook attuale è interattivo e lo vuoi conservare, non sovrascriverlo: crea un nuovo notebook script privato per il worker e mantieni il vecchio invariato.

## 2. Crea la credenziale Kaggle per GitHub Actions

In Kaggle apri il menu del profilo > **Settings** > **API** e crea un nuovo token/API key. Kaggle fornisce il nome utente e un segreto API: questi servono a GitHub Actions per inviare un aggiornamento del notebook e interrogarne lo stato. È una credenziale distinta da Google Drive OAuth. Conservala nel password manager e non inserirla nel dataset né nel repository.

## 3. Aggiungi i GitHub repository secrets

Nel repository GitHub `koij5/orchestratore-orpheus`, apri **Settings → Secrets and variables → Actions → New repository secret**. Aggiungi i segreti seguenti uno per volta. I valori rimangono nella UI GitHub e non devono apparire nei commit:

| Secret GitHub | Contenuto |
|---|---|
| `KAGGLE_USERNAME` | Username Kaggle |
| `KAGGLE_KEY` | Chiave API Kaggle generata al passaggio 2 |
| `KAGGLE_KERNEL_SLUG` | `UTENTE/NOME-NOTEBOOK` del notebook/script worker privato |
| `KAGGLE_DATASET_SLUG` | `UTENTE/NOME-DATASET` del dataset credenziali privato |
| `GOOGLE_AUTHORIZED_USER_JSON` | JSON OAuth Drive autorizzato, serializzato come una singola stringa; oppure `GOOGLE_SERVICE_ACCOUNT_KEY` se effettivamente supportato e dotato di quota Drive adeguata |

Aggiungi inoltre i secret relativi agli ID Drive di `audio_incoming`, `processing_kaggle`, `trascrizioni_grezze`, `trascrizioni_elaborate`, `audio_completati`, `errori` e radice materie. Gli ID sono stati annotati privatamente nel file locale `riferimenti_drive.txt`; non sono inseriti qui per ridurre la divulgazione. Puoi aprire il file nel tuo workspace locale oppure contattarmi se vuoi che ti guidi su dove inserirli. Le cartelle di materia che il codice si aspetta sono `biologia`, `chimica`, `fisica`, `inglese`, e sotto `italiano` le cartelle `letteratura` e `purgatorio`; ciascuna deve contenere una sottocartella `lezioni`. Non spostare o rinominare le cartelle preesistenti.

Google Drive OAuth permette a GitHub e Kaggle di accedere ai file come lo stesso utente solo se il token di refresh è effettivamente condiviso tra i due ambienti e continua a funzionare. Google può revocarlo o farlo scadere, in particolare per applicazioni OAuth esterne in Testing. Lo scope Drive usato dal codice è ampio: approva soltanto se accetti che il workflow abbia accesso al Drive. La UI del progetto non mostra il token; non salvarlo in variabili non segrete.

## 4. Verifica il dataset privato

Nel dataset privato verifica che siano presenti:

- `config.json`, con `processing_folder_id` uguale all'ID della cartella processing e `transcripts_folder_id` uguale all'ID di trascrizioni grezze;
- `credentials.json`, formato OAuth `authorized_user`, creato con lo stesso progetto/identità OAuth del token Drive messo in GitHub Secret.

Per My Drive personale imposta `shared_drive_id` a `null`. Verifica il percorso effettivo di mount, che normalmente deve essere `/kaggle/input/<dataset-slug>/config.json` e `/kaggle/input/<dataset-slug>/credentials.json`. Il worker non riceve automaticamente le environment variables di GitHub: la configurazione deve essere nel dataset Kaggle. Mantieni privati notebook, dataset e output.

## 5. Sequenza di prova

Il workflow remoto è disabilitato. Prima di chiedere l'attivazione, fai una prova manuale dal notebook Kaggle con un breve file audio di prova, ottenuto con permesso, in una cartella di test dedicata. Verifica di persona che il notebook sia realmente `Complete`, che sia stato creato un `.txt` nella cartella corretta e che i metadati `orpheus_source_id` e `orpheus_kind=raw_transcript` siano leggibili dall'orchestratore. Il worker carica il file grezzo, ma non classifica né sposta o elimina l'audio.

Quindi verifica il routing manuale per materia, le cartelle Drive e tutti i test. Solo allora un amministratore con permessi su GitHub può abilitare il workflow e fare `Run workflow` una volta. Dopo la verifica completa, valuta insieme la schedulazione. Non avviare `--apply` del tool di retention durante il test. I componenti in questa repository sono codice preparato e testato con mock: nessuna esecuzione completa reale è ancora stata attestata.

## 6. Limitazioni non risolvibili dalle chiavi

Una chiave Kaggle valida non può abilitare una GPU se il tuo account o l'ambiente non ne dispone, e un kernel può rimanere in coda. GitHub Actions non attende in sicurezza un job Kaggle lungo in un singolo runner oltre i suoi limiti temporali; l'orchestratore deve fare un controllo manuale per ciclo e non inviare richieste concorrenti.

Il notebook non può leggere la schermata del telefono né cancellare la registrazione locale. Gli audio vanno caricati su Drive dall'app. Se usi una sola registrazione per l'intera giornata, l'avvio manuale non informa il programma dei cambi di materia. Per lo smistamento automatico senza modello, ferma e riavvia la registrazione a ogni lezione e assegna un nome materia; altrimenti il file resta in revisione invece di essere attribuito a caso.
