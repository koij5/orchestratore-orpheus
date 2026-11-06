# book_tools

Componente indipendente per dividere un PDF locale autorizzato in capitoli e preparare bozze di studio verificabili. **Non** scarica libri, effettua scraping, aggira DRM o acquisisce materiali da siti esterni. Fornisci tu un PDF che hai titolo a usare, inclusa l'eventuale autorizzazione a estrarlo e a inviarne il testo a un servizio esterno. Usa file di lezioni in testo UTF-8 di cui puoi disporre. Il programma rifiuta PDF che richiedono password e quelli che disabilitano la copia: non gestisce o aggira protezioni.

## Importante: limiti e condizioni di Gemini

La divisione PDF è sempre locale e non usa Gemini. I comandi `materiali` e `sottolinea` trasmettono **a Google via Internet** il testo estratto dalle pagine selezionate e il contenuto integrale delle lezioni eventualmente specificate. Non trasmettono le immagini del PDF. Non inviano nulla senza le due opzioni esplicite indicate sotto; non vengono inviate chiavi o testi a servizi durante i test. Valuta diritti, riservatezza, dati personali, condizioni del servizio, costi e impostazioni del tuo account prima di avviare una richiesta. Il programma non può garantire come il fornitore gestisca i dati: consulta le sue condizioni.

Le [condizioni Gemini API](https://ai.google.dev/gemini-api/terms), efficaci dal 23 marzo 2026, richiedono almeno **18 anni** e limitano l'uso a sviluppatori che operano per scopi **professionali o aziendali**, escludendo l'uso consumer. Vietano anche l'uso in applicazioni rivolte a minori o a cui è probabile che accedano minori; prevedono altre restrizioni territoriali e di pagamento. **Non usare qui Gemini per studio personale consumer o in un'app destinata o probabilmente accessibile a minori.** Controlla le condizioni aggiornate e la loro applicabilità al tuo caso. Il programma non verifica automaticamente età, scopo, territorio o conformità.

Gemini è un'integrazione opzionale: non c'è alcun passaggio automatico a modelli locali o a Kaggle. Se l'uso non è consentito, usa soltanto `dividi` e non avviare gli altri comandi. I materiali generati sono **bozze da controllare**: un prompt può chiedere di non inventare, ma non garantisce accuratezza, citazioni, formule o soluzioni corrette. Immagini, grafici ed esercizi sotto forma di immagini non sono disponibili al modello, benché siano preservati visivamente nei PDF divisi. I PDF di sole immagini richiedono testo ottenuto separatamente, ad esempio mediante OCR autorizzato; questo strumento non lo esegue.

## Installazione

Dalla radice del progetto, con Python 3.9+ in un ambiente Python scelto dall'utente:

```bash
python -m pip install 'PyMuPDF>=1.24,<2'
python -m book_tools --help
```

Per i test si usa `unittest` della libreria standard. Gemini usa HTTPS con la libreria standard, senza SDK Google. Non è necessario modificare `requirements.txt`.

## 1. Suddividere un PDF autorizzato

Crea un manifest JSON, ad esempio `dati/manifest.json`:

```json
{
  "pdf": "libro.pdf",
  "capitoli": [
    {"nome": "01-introduzione.pdf", "da": 1, "a": 5},
    {"nome": "02-argomento.pdf", "da": 6, "a": 12}
  ]
}
```

`pdf` può essere assoluto oppure relativo alla cartella del manifest. `da` e `a` sono **numeri fisici delle pagine PDF**, da 1 e inclusivi, non le eventuali pagine stampate nel libro. Sono ammessi intervalli non contigui ma non sovrapposti, nomi di file semplici `.pdf` distinti e un solo PDF sorgente. Il programma convalida tutti gli intervalli e controlla i file di destinazione prima di creare i capitoli.

```bash
python -m book_tools dividi --manifest dati/manifest.json --output-dir capitoli
```

La suddivisione copia le pagine, non il solo testo, mantenendo quindi immagini, grafica, esercizi e le annotazioni supportate da PyMuPDF. Le caratteristiche speciali dei PDF complessi, come collegamenti a pagine escluse o moduli, vanno controllate manualmente. I file esistenti, PDF originale incluso, non vengono mai sovrascritti. Se un'operazione fallisce dopo aver creato file di capitolo, quelli creati in quella operazione vengono rimossi.

## 2. Preparare una bozza con Gemini, soltanto se consentito

Servono un capitolo PDF con testo estraibile, facoltativamente uno o più file `.txt` di lezioni e la chiave `GEMINI_API_KEY` disponibile nell'ambiente **solo a runtime**. Nessuna chiave viene scritta su file, stampata o inclusa negli URL. Ad esempio, in Bash si può leggere senza visualizzarla e poi esportarla nella sessione (non inserire una chiave nei comandi salvati in cronologia):

```bash
read -rs -p 'Gemini API key: ' GEMINI_API_KEY; echo
export GEMINI_API_KEY
```

Questi esempi si applicano **solo se l'uso è consentito dai termini sopra**. Entrambe le opzioni sono necessarie: `--invia-a-gemini` autorizza l'invio; `--conferma-uso-consentito` dichiara che età, scopo e tutte le condizioni applicabili sono rispettati.

```bash
mkdir -p risultati
python -m book_tools materiali \
  --pdf capitoli/01-introduzione.pdf \
  --materia fisica \
  --lezione lezioni/lezione1.txt --lezione lezioni/lezione2.txt \
  --da 1 --a 5 --output risultati/fisica.md \
  --invia-a-gemini --conferma-uso-consentito
```

Materie: `scienze`, `fisica`, `letteratura`, `inglese`, `purgatorio`. I prompt richiedono rispettivamente concetti da evidenziare; formule ed esercizi; analisi testuali e interrogazioni; analisi dei testi inglesi e interrogazioni; analisi dei passi del Purgatorio. L'output `.md` o `.txt` è locale e chiaramente una bozza da verificare. Senza `--lezione` il prompt segnala che le lezioni mancano. Il modello predefinito è `gemini-3.8-flash`, sostituibile con `--modello` se abilitato nel proprio account; la disponibilità può variare.

`--da` e `--a` sono opzionali ma vanno specificati insieme: si riferiscono alle pagine fisiche **del PDF fornito**, non al libro prima della suddivisione. Omettili per l'intero PDF. Ogni pagina selezionata deve contenere testo estraibile; in caso contrario l'elaborazione si interrompe indicando le pagine vuote. Non vengono letti né trasmessi i contenuti di immagini o grafici. Pagine con testo *e* immagini possono comunque avere informazioni mancanti. Non c'è troncamento automatico: per capitoli grandi lavora per sezioni esplicite, mantenendo output diversi, e verifica i limiti del modello.

## 3. Sottolineare solo citazioni verificate

Sempre e soltanto nell'eventualità di uso consentito:

```bash
python -m book_tools sottolinea \
  --pdf capitoli/01-introduzione.pdf --materia scienze \
  --lezione lezioni/lezione1.txt --da 1 --a 5 \
  --output risultati/capitolo-sottolineato.pdf \
  --rapporto risultati/citazioni.json \
  --invia-a-gemini --conferma-uso-consentito
```

Il modello riceve istruzioni di restituire esclusivamente JSON con citazioni esatte, solo dal PDF e con numero pagina. Le lezioni sono solo contesto. **La risposta non è attendibile automaticamente**: il programma verifica localmente ogni citazione, distinguendo maiuscole, spazi e caratteri, rispetto al testo e ai caratteri di PyMuPDF sulla pagina indicata. Una corrispondenza deve essere interamente su una singola riga orizzontale; citazioni su più righe, testo ruotato o caratteri estratti diversamente non vengono annotate. Se la citazione compare più volte sulla pagina, vengono annotate tutte le occorrenze esatte. Il rapporto JSON contiene `annotate`, numero di occorrenze, e `non_trovate` con motivo; pagine fuori sezione sono rifiutate. Nessuna citazione non trovata o parafrasi genera annotazioni. Nessuna immagine o pagina viene ridisegnata o inventata.

L'output è una **copia dell'intero PDF di input** con sole annotazioni aggiunte alle pagine selezionate; l'originale resta intatto. La presenza esatta di una citazione non ne verifica la rilevanza o l'interpretazione: controlla sempre PDF e rapporto. Se la risposta Gemini è incompleta, non è JSON valido o ha struttura errata, non vengono creati output.

## Test offline

```bash
PYTHONDONTWRITEBYTECODE=1 python -m unittest discover -s tests -p 'test_book_tools.py' -v
```

I test generano PDF sintetici in directory temporanee e simulano le risposte Gemini: non richiedono chiavi né accesso alla rete. Non creano o modificano PDF dell'utente.
