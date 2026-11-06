# Collegare il tuo notebook Kaggle privato a GitHub Actions

Questo documento serve a collegare il notebook già esistente senza rendere pubblici notebook, dataset o credenziali. **Non incollare mai token o chiavi in chat, GitHub Issues o file del repository.** Il workflow è attualmente disabilitato da GitHub e deve restare tale fino al test manuale.

## 1. Trova i due slug nella UI Kaggle

Apri il notebook esistente su Kaggle e controlla che sia **Private**. Copia dalla barra degli indirizzi l'URL, che normalmente ha forma `https://www.kaggle.com/code/UTENTE/NOME-NOTEBOOK`. Il valore da conservare localmente come `KAGGLE_KERNEL_SLUG` è `UTENTE/NOME-NOTEBOOK`. Non occorre inviarmelo.

Individua anche il dataset privato che contiene la configurazione Drive (dovrebbe contenere `config.json` e `credentials.json`). L'URL normalmente ha forma `https://www.kaggle.com/datasets/UTENTE/NOME-DATASET`; il relativo valore è `UTENTE/NOME-DATASET`, da usare come `KAGGLE_DATASET_SLUG`. Verifica che sia privato e che il notebook sia autorizzato a leggerlo. La pipeline preparata legge le credenziali Drive da quel dataset montato, non dal notebook e non da GitHub. Se le credenziali sono invece solo nelle Kaggle Secrets o incorporate direttamente nel notebook, non sono accessibili al worker così com'è: non copiarle nel codice. Sposta in un dataset privato i due file della configurazione seguendo [README Kaggle](kaggle_kernel/README.md).

La nuova implementazione usa un notebook Kaggle di tipo **script**, `trascrivi.py`, con metadati `kernel-metadata.json` e il dataset configurato come input. Il workflow genera un singolo file standalone incorporando il classifier: il client Kaggle non invia automaticamente gli altri `.py` della cartella. Non è un aggiornamento garantito del notebook notebook-style già esistente: prima di collegare slug e script, controlla in Kaggle che il notebook possa essere aggiornato come script. Se il notebook attuale è interattivo e lo vuoi conservare, non sovrascriverlo: crea un nuovo notebook script privato per il worker e mantieni il vecchio invariato.

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

Aggiungi inoltre i secret relativi agli ID Drive di `audio_incoming`, `processing_kaggle`, `trascrizioni_grezze`, `trascrizioni_elaborate`, `audio_completati`, `errori` e radice materie. Gli ID sono stati annotati privatamente nel file locale `riferimenti_drive.txt`; non sono inseriti qui per ridurre la divulgazione. Puoi aprire il file nel tuo workspace locale oppure contattarmi se vuoi che ti guidi su dove inserirli. Il mapping rispetta la gerarchia Drive già presente: `scienze/biologia`, `scienze/chimica`, `fisica`, `inglese`, `italiano/letteratura` e `italiano/purgatorio`. Ogni cartella materia finale deve già contenere `lezioni`, `argomenti` e `libro`; l’orchestratore scrive soltanto dentro `lezioni` e non crea, sposta, rinomina o cancella le cartelle esistenti. Un mapping esplicito via `MATERIE_FOLDER_IDS` viene comunque verificato rispetto a questi genitori.

Google Drive OAuth permette a GitHub e Kaggle di accedere ai file come lo stesso utente solo se il token di refresh è effettivamente condiviso tra i due ambienti e continua a funzionare. Google può revocarlo o farlo scadere, in particolare per applicazioni OAuth esterne in Testing. Lo scope Drive usato dal codice è ampio: approva soltanto se accetti che il workflow abbia accesso al Drive. La UI del progetto non mostra il token; non salvarlo in variabili non segrete.

## 4. Verifica il dataset privato

Nel dataset privato verifica che siano presenti:

- `config.json`, con `processing_folder_id` uguale all'ID della cartella processing e `transcripts_folder_id` uguale all'ID di trascrizioni grezze;
- `credentials.json`, formato OAuth `authorized_user`, creato con lo stesso progetto/identità OAuth del token Drive messo in GitHub Secret.

Per My Drive personale imposta `shared_drive_id` a `null`. Verifica il percorso effettivo di mount, che normalmente deve essere `/kaggle/input/<dataset-slug>/config.json` e `/kaggle/input/<dataset-slug>/credentials.json`. Il worker non riceve automaticamente le environment variables di GitHub: la configurazione deve essere nel dataset Kaggle. Mantieni privati notebook, dataset e output.

## 5. Sequenza di prova

Il workflow remoto è disabilitato. Prima di chiedere l'attivazione, fai una prova manuale dal notebook Kaggle con un breve file audio di prova, ottenuto con permesso, in una cartella di test dedicata. Verifica di persona che il notebook sia realmente `Complete`, che siano stati creati il TXT master integrale e il sidecar `subject-plan.json`, e che i metadati di sorgente/hash siano leggibili dall'orchestratore. Il worker classifica il TXT localmente dopo averlo prodotto; non classifica l'audio, non divide né elimina la registrazione.

Quindi verifica che le materie corrette siano state smistate dalle sole righe del TXT, che le materie non archiviate siano rimaste nel master ma escluse dai file per materia, che gli intervalli incerti non abbiano completato l'audio, e che i file Drive siano corretti; esegui tutti i test. Solo allora un amministratore con permessi su GitHub può abilitare il workflow e fare `Run workflow` una volta. Dopo la verifica completa, valuta insieme la schedulazione. Non avviare `--apply` del tool di retention durante il test. I componenti in questa repository sono codice preparato e testato con mock: nessuna esecuzione completa reale è ancora stata attestata.

## 6. Limitazioni non risolvibili dalle chiavi

Una chiave Kaggle valida non può abilitare una GPU se il tuo account o l'ambiente non ne dispone, e un kernel può rimanere in coda. GitHub Actions non attende in sicurezza un job Kaggle lungo in un singolo runner oltre i suoi limiti temporali; l'orchestratore deve fare un controllo manuale per ciclo e non inviare richieste concorrenti.

Il notebook non può leggere la schermata del telefono né cancellare la registrazione locale. Gli audio vanno caricati su Drive dall'app. Una registrazione unica viene trascritta tutta. Dopo, il modello locale classifica finestre del testo di circa 60 secondi. Non richiede che il nome audio codifichi la materia né gli orari delle lezioni. Le classificazioni a bassa confidenza restano in revisione; la segmentazione va controllata su una prova reale perché il classificatore zero-shot non garantisce confini perfetti.
