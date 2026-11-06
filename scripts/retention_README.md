# Retention Drive Orpheus (strumento autonomo)

## Perimetro e garanzie

`retention.py` lavora esclusivamente sui figli immediati degli ID espliciti passati con `--lesson-root` (ripetibile) e/o `--completed-audio`; nelle cartelle lezione marcate visita solo i figli immediati. Non cerca nell'intero Drive, non deduce alcuna identità dal nome e non scarica/legge contenuti. La modalità predefinita è dry run; l'unica mutazione prevista è impostare `trashed=true` su un singolo file. Non elimina mai definitivamente e non cestina mai cartelle, radici, né dati senza marker esatto. Il cestino è recuperabile via Drive; questa utility non svuota né gestisce il cestino. Non può eliminare copie locali già presenti su telefoni o altri dispositivi.

Prima di qualunque mutazione esegue l'intero inventario con paginazione e interrompe in caso di ricerca incompleta o risposta ambigua. Per ciascun file candidato rilegge da Drive, per due cicli distinti e immediatamente prima della modifica, radice, eventuale cartella lezione e file stesso. Confronta ogni campo ricevuto, inclusi `version`, parent, proprietà e `createdTime`; per il file richiede inoltre in entrambe le letture lo stesso ETag HTTP forte e applica `If-Match` all'aggiornamento. L'operazione condizionale e singola non ha retry automatico. Drive non fornisce qui una transazione né un lock multi-file: sospendere gli scrittori (pipeline, upload e interventi manuali) per tutto il comando. `--confirm-exclusive` è un'attestazione operativa dell'utente, non un lock. In caso di race o errore di rete l'esito della singola mutazione può essere incerto: verificare manualmente il cestino prima di riprovare.

I marker `appProperties` sono privati dell'applicazione Drive: autorizzare lo stesso progetto Google OAuth dell'orchestratore e verificare con le stesse identità che gli marker siano visibili. Se incompleti, invisibili, incongruenti o non supportati dai metadati restituiti, lo script esclude l'elemento o si arresta. Non stampa token, nomi, contenuti, stringhe d'eccezione API o traceback.

## Contratto esatto dei marker e prerequisiti d'integrazione

Il codice attuale nel repository scrive tipi `lesson` e `lesson_text`, e non attesta sull'audio revisione positiva né risultati verificati. **Quindi l'orchestratore corrente non soddisfa il contratto sotto e questa retention non cestina i suoi dati finché non viene coordinato e verificato un aggiornamento**. Non basta rinominare i valori da una procedura di retention. Audio senza attestazione e TXT/cartelle con i tipi attualmente prodotti sono deliberatamente no-op. Questo intervento non modifica altri file, non applica marker retroattivi e non dichiara riuscite revisioni storiche.

L'orchestratore deve attestare e verificare in Drive questi `appProperties` private:

* Cartella generata per una lezione: `orpheus_kind=lesson_folder`, `orpheus_source_id=<ID audio sorgente>`, `orpheus_segment_key=<indice iniziale>:<indice finale>`, `orpheus_plan_hash=<SHA-256 esadecimale lowercase di 64 caratteri>`. Segmenti sono due interi decimali canonici non negativi `start:end`, con `start <= end`.
* Figlio TXT generato: `orpheus_kind=lesson_txt`, `orpheus_source_id=<stesso ID sorgente della cartella>`, `orpheus_segment_key=<stessa chiave di segmento della cartella>`, `orpheus_plan_hash=<stesso hash della cartella>`. Il controllo confronta l'intera identità (sorgente, chiave di segmento e hash) della cartella con quella del file, non solo il `source_id`; un disaccordo esclude il file. In particolare cartella e file devono avere lo stesso `orpheus_source_id`, `orpheus_segment_key` e `orpheus_plan_hash`; nessun valore viene dedotto dal nome. Sono ammessi solo file `text/plain`, figli diretti di quella cartella, con capacità Drive esplicita `capabilities.canTrash=true`.
* Audio completato: figlio diretto dell'ID fornito con `--completed-audio`, MIME che inizia con `audio/`, `orpheus_kind=completed_audio`, `orpheus_source_id=<ID dello stesso file audio>`, `orpheus_routing_complete=true`, `orpheus_review_status=approved`, `orpheus_results_verified=true`. L'orchestratore deve impostare i due marker di review/verifica soltanto dopo effettiva revisione positiva e verifica indipendente dei risultati e deve rileggere e verificare in Drive la persistenza di ogni marker prima di segnare l'audio eleggibile. Una semplice trascrizione, uno stato "completato", la cartella o il nome non bastano.

È ammesso anche `orpheus_status=complete` oppure `orpheus_status=approved`; se presente ogni altro valore (in particolare `review`, `failed` o `rejected`) è un veto. Marker con valore, nome, MIME, parent o stato mancante/non attestato implicano esclusione. Le cartelle lezione devono essere cartelle generate marcate, figlie immediate dell'ID `--lesson-root`; i TXT devono essere file generati marcati in tali cartelle. Le cartelle non sono mai obiettivi di cestinamento. `createdTime` del TXT deve essere un timestamp ISO 8601 con fuso e l'età si calcola conservativamente in UTC; devono essere trascorsi almeno 30×24 ore esatte. Un timestamp mancante/non valido/futuro non è eleggibile; nomi e date nel nome non sono usati.

## OAuth e uso

Usare esclusivamente un JSON OAuth `authorized_user` in un file privato fuori dal repository, oppure fornirlo nella variabile d'ambiente `GOOGLE_AUTHORIZED_USER_JSON`. Non mettere token nella riga di comando, nei log o nel repository. L'identità autorizzata deve poter leggere i metadati e spostare nel cestino i file richiesti; usare credenziali reali solo dopo configurazione e test separati.

Dry run (nessun aggiornamento):

```sh
/home/ubuntu/work/venv/bin/python scripts/retention.py \
  --lesson-root ID_CARTELLA_LEZIONI --completed-audio ID_COMPLETATI \
  --credentials-json /percorso/privato/oauth.json
```

Applicazione deliberata: ispezionare la lista per ID, fermare tutti gli scrittori, poi aggiungere **entrambi** i flag espliciti:

```sh
/home/ubuntu/work/venv/bin/python scripts/retention.py \
  --lesson-root ID_CARTELLA_LEZIONI --completed-audio ID_COMPLETATI \
  --credentials-json /percorso/privato/oauth.json \
  --apply --confirm-exclusive
```

`--confirm-exclusive` significa che l'operatore ha sospeso pipeline, upload e modifiche manuali durante l'intera operazione; da solo non fornisce una garanzia tecnica di lock. Gli ID radice devono essere espliciti, validi e distinti. Le API sono usate solo quando si avvia deliberatamente la CLI dopo aver fornito credenziali OAuth; suite di test e funzioni di policy usano un servizio fittizio e non richiedono credenziali né connessioni.

## Test offline

Dalla radice del repository:

```sh
/home/ubuntu/work/venv/bin/python -m pytest -q tests/test_retention.py
```
