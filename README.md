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

Di Google Cloud Console, aktifkan **YouTube Data API v3**, buat OAuth client yang sesuai dengan redirect URI di atas, dan izinkan akses YouTube. Setelah otorisasi, tempel URL redirect lengkap pada panel Shorts atau Live. Refresh token tersimpan di `data/youtube_accounts.json` dengan izin file terbatas; file runtime di dalam `data/` diabaikan Git. Pada Streamlit Cloud, filesystem bersifat sementara, sehingga koneksi channel dapat hilang saat instance diganti. Akses YouTube Live mencakup pembuatan broadcast dan stream, jadi channel yang pernah dihubungkan untuk Shorts perlu menyetujui scope Live yang lebih luas dengan login OAuth ulang.

## Tujuan Live tersimpan

Di tab **🔴 Live 24 Jam**, buka **Tambah / hubungkan channel YouTube Live via OAuth**. Klik otorisasi, tempel URL redirect Google, pilih channel yang sudah terhubung, atur judul/visibilitas, lalu klik **Siapkan broadcast & stream key otomatis**. Aplikasi membuat broadcast dan stream YouTube, kemudian memilih tujuan RTMP otomatis. Channel harus sudah memenuhi syarat YouTube Live. Otorisasi Shorts lama perlu diulang agar memberikan izin YouTube Live.

Sebagai alternatif, tab ini juga dapat menampilkan daftar channel RTMP tersimpan dari Secrets. Tambahkan bagian ini di Streamlit Secrets, lalu masukkan masing-masing URL dan stream key dari YouTube Live Control Room:

```toml
[live_channels]
default = "ChannelUtama"

[live_channels.ChannelUtama]
title = "Channel Utama"
rtmp_url = "rtmps://a.rtmps.youtube.com:443/live2"
stream_key = "STREAM_KEY_CHANNEL_UTAMA"

[live_channels.ChannelKedua]
title = "Channel Kedua"
rtmp_url = "rtmps://a.rtmps.youtube.com:443/live2"
stream_key = "STREAM_KEY_CHANNEL_KEDUA"
```

`default` menentukan channel yang otomatis terpilih saat membuka app; channel lain dapat dipilih dari dropdown. Mode ini menyiarkan ke **satu channel yang dipilih per sesi**, bukan simulcast serentak. Stream key hanya disimpan di Secrets dan tidak ditampilkan di UI. Tanpa profil tersebut, input RTMP manual lama tetap tersedia.

Fitur API pihak ketiga memerlukan kredensial dan kuota masing-masing. Render video menggunakan gambar Clipdrop dan FFmpeg lokal; upload dilakukan ke channel yang dipilih dengan visibilitas yang dipilih pengguna.
