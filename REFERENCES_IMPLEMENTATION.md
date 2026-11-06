# Riferimenti ufficiali verificati il 6 novembre 2026

- Gemini API Additional Terms, effective March 23, 2026: https://ai.google.dev/gemini-api/terms — requisiti di età, scopi professionali/business, divieto d'uso consumer, accesso agli utenti SEE solo paid services, trattamento dei dati e istruzioni di non inviare dati sensibili/confidenziali/personali a servizi unpaid.
- Google Drive API scopes: https://developers.google.com/workspace/drive/api/guides/api-specific-auth — `drive.file` autorizza i file creati dall'app o esplicitamente aperti/condivisi tramite Picker; `drive` è uno scope ristretto di accesso generale.
- Google OAuth 2.0: https://developers.google.com/identity/protocols/oauth2 — token refresh, client di tipo applicativo, scadenza di 7 giorni in alcuni progetti esterni in Testing.
- Shared Drives: https://developers.google.com/workspace/drive/api/guides/about-shareddrives — account Gmail personali non possono crearli, ma possono essere membri se autorizzati.
- Errori Drive: https://developers.google.com/workspace/drive/api/guides/handle-errors — errori di quota di archiviazione Drive.
- Android FGS background restrictions: https://developer.android.com/develop/background-work/services/fgs/restrictions-bg-start
- Android FGS microphone type: https://developer.android.com/develop/background-work/services/fgs/service-types
- Android exact alarms: https://developer.android.com/about/versions/14/changes/schedule-exact-alarms
- Android notifications: https://developer.android.com/develop/ui/views/notifications/notification-permission
- Audio concurrent capture: https://developer.android.com/media/platform/sharing-audio-input
- Kaggle Notebooks documentation: https://www.kaggle.com/docs/notebooks
- Kaggle kernel metadata spec: https://github.com/Kaggle/kaggle-api/blob/main/docs/kernels_metadata.md
- Kaggle API kernel CLI: https://github.com/Kaggle/kaggle-cli/blob/main/docs/kernels.md
- Whisper resource requirements/model sizes: https://github.com/openai/whisper
- Demucs: https://github.com/facebookresearch/demucs
- Gemini API rate limits: https://ai.google.dev/gemini-api/docs/rate-limits
- GitHub Actions limits: https://docs.github.com/en/actions/reference/limits
- GitHub cron behavior: https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows
- GitHub Actions billing: https://docs.github.com/en/actions/concepts/billing-and-usage

Operational interpretation: l’utente produce una sola registrazione giornaliera di durata variabile, avviata e fermata manualmente. Kaggle trascrive l’audio intero con Whisper e conserva il TXT master; successivamente un classificatore NLI locale lavora sul testo, crea un sidecar con intervalli e l’orchestratore smista solo finestre classificate sopra soglia. L’audio non viene segmentato né cancellato, l’upload resta manuale e gli intervalli incerti richiedono revisione. Le cartelle reali di Drive sono `scienze/biologia`, `scienze/chimica`, `fisica`, `inglese`, `italiano/letteratura` e `italiano/purgatorio`; ognuna contiene `lezioni`, `argomenti` e `libro`. Il modello zero-shot resta euristico e va collaudato su contenuti scolastici reali; non è una classificazione validata. La pipeline audio non invia testo a Gemini. Le quote GPU Kaggle cambiano e dipendono dall’account. Android recording/upload, conversione di libri digitali protetti da codici, eliminazione dell’audio dal telefono/cloud e cancellazione mensile automatica dei TXT non sono attivati; sono fuori dal collaudo corrente. GitHub Actions non deve aspettare sincronicamente un’esecuzione Kaggle lunga e resta disabilitato finché il test manuale non è autorizzato.


## Classificazione locale del transcript

- Model card mDeBERTa multilingual XNLI zero-shot: https://huggingface.co/MoritzLaurer/mDeBERTa-v3-base-xnli-multilingual-nli-2mil7 — dichiara supporto italiano in training NLI (27 lingue include `it`), licenza MIT e uso Transformers zero-shot. La valutazione riporta benchmark NLI generali, non classificazione di lezioni scolastiche; non è prova dell'accuratezza nel caso specifico. Il checkpoint ha circa 279 milioni di parametri e richiede un collaudo locale.
- Hugging Face model API metadata (license/model card data): https://huggingface.co/api/models/MoritzLaurer/mDeBERTa-v3-base-xnli-multilingual-nli-2mil7
- Transformers `ZeroShotClassificationPipeline`: https://huggingface.co/docs/transformers/main_classes/pipelines — classifica usando coppie premise/hypothesis e classi candidate; più flessibile ma più lento di classificatori con classi fisse.
- Kaggle Notebooks documentation: https://www.kaggle.com/docs/notebooks — GPU gratuita a disponibilità limitata e possibile coda; le risorse hardware e i limiti delle sessioni possono variare nel tempo.

Progettazione per Orpheus: classificare finestre temporali dal transcript con il modello localmente su Kaggle, non trasmettere il testo a Gemini, preservare l'intero master, produrre un piano di intervalli timestampati e smistare solo le sei materie coperte da cartelle. La classificazione NLI zero-shot è un'euristica di primo passaggio: confidenza bassa, intervalli non supportati o cambi di materia incerti devono rimanere espliciti e richiedere revisione; va collaudata su trascritti rappresentativi prima dell'uso operativo.
