# liveopreklawas

Studio Streamlit untuk impor media, normalisasi, live streaming, dan pembuatan YouTube Shorts vertikal.

## YouTube Shorts

Panel **📱 Viral Shorts** mendukung pembuatan latar Clipdrop, kartu teks 9:16, metadata Ollama Cloud, OAuth YouTube, dan upload resumable. `ffmpeg` harus tersedia; font DejaVu Sans dipakai bila ada.

API keys bisa dimasukkan di panel Shorts untuk sesi aktif, atau disimpan di `.streamlit/secrets.toml` (jangan commit file secrets):

```toml
[clipdrop]
api_key = "CLIPDROP_API_KEY"

[ollama]
api_key = "OLLAMA_CLOUD_API_KEY"
model = "gpt-oss:20b"

[youtube_oauth]
client_id = "GOOGLE_OAUTH_CLIENT_ID"
client_secret = "GOOGLE_OAUTH_CLIENT_SECRET"
redirect_uri = "https://redirect1x.streamlit.app/"
state_secret = "RANDOM_LONG_PRIVATE_VALUE"
```

Di Google Cloud Console, aktifkan **YouTube Data API v3**, buat OAuth client yang sesuai dengan redirect URI di atas, dan izinkan scope upload YouTube. Setelah otorisasi, tempel URL redirect lengkap pada panel Shorts. Refresh token tersimpan di `data/youtube_accounts.json` dengan izin file terbatas; file runtime di dalam `data/` diabaikan Git. Pada Streamlit Cloud, filesystem bersifat sementara, sehingga koneksi channel dapat hilang saat instance diganti.

Fitur API pihak ketiga memerlukan kredensial dan kuota masing-masing. Render video menggunakan gambar Clipdrop dan FFmpeg lokal; upload dilakukan ke channel yang dipilih dengan visibilitas yang dipilih pengguna.
