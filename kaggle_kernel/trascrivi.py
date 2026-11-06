import os
import io
import json
import whisper
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload

# ==========================================
# 1. CONFIGURAZIONE E PERCORSI KAGGLE
# ==========================================
# ATTENZIONE: Su Kaggle i file del dataset sono in /kaggle/input/NOME_DATASET/
# Devi caricare il tuo file JSON del Service Account come dataset privato su Kaggle.
# Sostituisci "nome-del-tuo-dataset" con il nome effettivo.
CREDENTIALS_PATH = "/kaggle/input/nome-del-tuo-dataset/credenziali.json"

# Gli ID delle cartelle di Drive. Puoi metterli qui o in un file config.json nel dataset.
FOLDER_PROCESSING_KAGGLE = "ID_CARTELLA_PROCESSING"
FOLDER_TRASCRIZIONI_GREZZE = "ID_CARTELLA_TRASCRIZIONI"

# ==========================================
# 2. INIZIALIZZAZIONE DRIVE E WHISPER
# ==========================================
print("Inizializzazione Google Drive...")
creds = service_account.Credentials.from_service_account_file(
    CREDENTIALS_PATH,
    scopes=['https://www.googleapis.com/auth/drive']
)
drive_service = build('drive', 'v3', credentials=creds)
DRIVE_KW = {"supportsAllDrives": True}

print("Caricamento modello Whisper Large-v3 su GPU...")
# Usa 'large-v3' per la massima precisione, o 'distil-large-v3' se vuoi più velocità
model = whisper.load_model("large-v3", device="cuda")

# ==========================================
# 3. FUNZIONI DI SUPPORTO
# ==========================================
def list_files(folder_id):
    q = f"'{folder_id}' in parents and trashed=false and mimeType!='application/vnd.google-apps.folder'"
    res = drive_service.files().list(q=q, fields="files(id, name)", pageSize=50, **DRIVE_KW).execute()
    return res.get('files', [])

def download_file(file_id, local_path):
    request = drive_service.files().get_media(fileId=file_id, **DRIVE_KW)
    with open(local_path, 'wb') as fh:
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        while done is False:
            status, done = downloader.next_chunk()
    return local_path

def upload_text(text, filename, parent_id):
    media = MediaIoBaseUpload(io.BytesIO(text.encode('utf-8')), mimetype='text/plain')
    file_metadata = {'name': filename, 'parents': [parent_id]}
    drive_service.files().create(body=file_metadata, media_body=media, fields='id', **DRIVE_KW).execute()

# ==========================================
# 4. LOGICA PRINCIPALE
# ==========================================
def main():
    audio_files = list_files(FOLDER_PROCESSING_KAGGLE)
    if not audio_files:
        print("Nessun file audio trovato in _processing_kaggle. Esco.")
        return

    print(f"Trovati {len(audio_files)} file audio da trascrivere.")

    for audio in audio_files:
        file_id = audio['id']
        file_name = audio['name']
        base_name = os.path.splitext(file_name)[0]
        local_audio_path = f"/kaggle/working/{file_name}"
        
        try:
            print(f"\n--- Elaborazione: {file_name} ---")
            
            # 1. Scarica l'audio
            print("Download in corso...")
            download_file(file_id, local_audio_path)
            
            # 2. Trascrivi con Whisper
            print("Trascrizione in corso (potrebbe volerci qualche minuto)...")
            result = model.transcribe(local_audio_path, language="it", verbose=False)
            
            # 3. Formatta il testo con i timestamp (necessari per Gemini)
            # Esempio: [00:00:05 - 00:00:12] Ciao a tutti...
            testo_formattato = ""
            for segment in result['segments']:
                start_m, start_s = divmod(int(segment['start']), 60)
                end_m, end_s = divmod(int(segment['end']), 60)
                timestamp = f"[{start_m:02d}:{start_s:02d} - {end_m:02d}:{end_s:02d}]"
                testo_formattato += f"{timestamp} {segment['text'].strip()}\n"
            
            # 4. Carica il file .txt su Drive
            print("Caricamento trascrizione su Drive...")
            txt_name = f"{base_name}.txt"
            upload_text(testo_formattato, txt_name, FOLDER_TRASCRIZIONI_GREZZE)
            
            print(f"Completato: {txt_name}")
            
        except Exception as e:
            print(f"ERRORE durante l'elaborazione di {file_name}: {e}")
        finally:
            # Pulizia locale per non esaurire lo spazio su Kaggle
            if os.path.exists(local_audio_path):
                os.remove(local_audio_path)

if __name__ == "__main__":
    main()
