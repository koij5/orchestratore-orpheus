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

Operational interpretation: Android foreground microphone service started manually still shows an ongoing user-visible notification and can be interrupted; launching manually does not label subject boundaries. One full-day recording without class timetable/subject markers cannot be split reliably into classes just because recording was manually initiated. For manual subject-specific recording, label/upload one file per subject; otherwise schedule or explicit transcript review is needed. Whisper timestamps are relative audio times; Gemini results cannot be presumed accurate or permissible. Kaggle free quotas and GPU allocations change/are account-specific; the notebook's private status and shared dataset credentials must be verified on Kaggle. GitHub Actions cron is not guaranteed precise; run finite jobs, do not wait synchronously for long Kaggle execution in a GitHub job.
