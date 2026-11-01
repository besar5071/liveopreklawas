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

Di tab **🔴 Live 24 Jam**, buka **Tambah / hubungkan channel YouTube Live via OAuth**. Klik otorisasi, tempel URL redirect Google, pilih sampai lima channel akun, atur judul/visibilitas, lalu klik **Siapkan broadcast untuk channel terpilih**. Aplikasi membuat broadcast dan stream YouTube untuk tiap akun, kemudian menyiapkan tujuan RTMP otomatis. Pilih sampai lima tujuan di pemilih **Tujuan Live serentak** lalu mulai live. Channel harus sudah memenuhi syarat YouTube Live. Otorisasi Shorts lama perlu diulang agar memberikan izin YouTube Live.

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

`default` menentukan channel yang otomatis terpilih saat membuka app; profil lain juga dapat dipilih. Pemilih tujuan mendukung maksimal **lima channel live serentak**, termasuk gabungan tujuan OAuth, profil Secrets, dan satu tujuan manual. Satu proses FFmpeg meneruskan stream yang sama ke semua target; trafik keluar akan bertambah kira-kira sebanding dengan jumlah channel. Stream key hanya disimpan di Secrets / session state dan tidak ditampilkan di UI.

Fitur API pihak ketiga memerlukan kredensial dan kuota masing-masing. Render video menggunakan gambar Clipdrop dan FFmpeg lokal; upload dilakukan ke channel yang dipilih dengan visibilitas yang dipilih pengguna.
