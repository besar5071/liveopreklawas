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
live_model = "gemma4:31b"

[kie]
api_key = "KIE_AI_API_KEY"

[youtube_oauth]
client_id = "GOOGLE_OAUTH_CLIENT_ID"
client_secret = "GOOGLE_OAUTH_CLIENT_SECRET"
redirect_uri = "https://redirect1x.streamlit.app/"
state_secret = "RANDOM_LONG_PRIVATE_VALUE"
```

Di Google Cloud Console, aktifkan **YouTube Data API v3**, buat OAuth client yang sesuai dengan redirect URI di atas, dan izinkan akses YouTube. Setelah otorisasi, tempel URL redirect lengkap pada panel Shorts atau Live. Refresh token tersimpan di `data/youtube_accounts.json` dengan izin file terbatas; file runtime di dalam `data/` diabaikan Git. Pada Streamlit Cloud, filesystem bersifat sementara, sehingga koneksi channel dapat hilang saat instance diganti. Akses YouTube Live mencakup pembuatan broadcast dan stream, jadi channel yang pernah dihubungkan untuk Shorts perlu menyetujui scope Live yang lebih luas dengan login OAuth ulang.

## Music Studio AI

Tab **🎼 Musik AI** mengikuti alur pembuat lagu pada referensi: buat draf judul/lirik/style dari ide menggunakan Ollama Cloud, edit hasilnya, lalu kirim ke Kie.ai untuk membuat lagu vokal atau instrumental. Tersedia model V6, V6 Mini, dan V6 Wild, opsi karakter vokal, durasi, variasi, style weight, serta tingkat eksperimen. Tugas berjalan di Kie.ai; status diperiksa otomatis dan lagu dapat diputar. Untuk tiap hasil, pilih **Siapkan file unduhan** untuk mengunduh audio ke perangkat, atau **Simpan ke Library** agar file tersimpan di folder Audio aplikasi. Pengunduhan dibatasi 150 MB per hasil. Task ID bisa dipulihkan pada sesi berikutnya jika masih tersedia.

Atur `[kie] api_key` dan, jika ingin draf lirik AI, `[ollama] api_key` di Streamlit Secrets seperti contoh di atas. Key yang diketik langsung berlaku untuk sesi browser saat ini saja dan tidak disimpan ke data job. Penggunaan Kie.ai dapat menghabiskan kredit sesuai model/tarif akun; cek judul, lirik, style dan status sebelum mencoba ulang. Jika koneksi terputus setelah permintaan dikirim, aplikasi menahan pengiriman ulang sampai statusnya diperiksa untuk membantu mencegah biaya ganda. Riwayat Task ID disimpan di sesi Streamlit (bukan file audio); unduh lagu yang ingin disimpan karena URL provider dapat kedaluwarsa.

## Tujuan Live tersimpan

Di tab **🔴 Live 24 Jam**, buka **Tambah / hubungkan channel YouTube Live via OAuth**. Klik otorisasi, tempel URL redirect Google, pilih akun, lalu atur jumlah Live per akun dan judul/visibilitas. Maksimal lima broadcast/slot disiapkan total per sesi; satu akun dapat memiliki hingga lima slot dengan broadcast dan stream key unik. Setelah klik **Siapkan broadcast/slot Live**, pilih slot di pemilih tujuan. Mode **Serentak** mengirim feed video/audio acak yang sama ke semua slot terpilih; mode **Bergantian (manual)** menjalankan satu slot setiap kali—hentikan siaran lalu pilih slot berikutnya untuk berganti. Channel harus sudah memenuhi syarat YouTube Live. Otorisasi Shorts lama perlu diulang agar memberikan izin YouTube Live.

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

`default` menentukan channel yang otomatis terpilih saat membuka app; profil lain juga dapat dipilih. Maksimal lima slot/broadcast dapat dipilih total, termasuk beberapa slot dari satu channel OAuth dan gabungan tujuan Secrets/manual. Dalam mode serentak, satu encode dari playlist video/audio acak yang dipilih diteruskan ke semua slot; upload bandwidth bertambah kira-kira sebanding dengan jumlah slot. Mode bergantian bersifat manual: pilih satu slot, hentikan Live aktif, kemudian pilih slot lain dan mulai lagi. Semua slot serentak memakai feed media yang sama, bukan playlist independen per slot. Stream key hanya disimpan di Secrets/session state dan tidak ditampilkan di UI.

Panel **✨ Buat judul + hashtag dan deskripsi otomatis (Ollama Cloud)** menganalisis hingga tiga audio terpilih dengan mengekstrak PCM mono 16 kHz, menghitung RMS, zero-crossing rate, spectral centroid/rolloff/flux dan estimasi tempo, serta membuat spektrogram nyata. Fitur numerik dan gambar spektrogram dikirim ke model vision Ollama Cloud bersama riset web untuk meracik metadata. Ini analisis sinyal heuristik—bukan pengenal lagu/artis atau jaminan genre/viral. Hasil judul dan deskripsi (termasuk hashtag) bisa diedit dan dipakai saat menyiapkan broadcast OAuth. API key dibaca dari `[ollama] api_key` atau input sesi; model vision Live default `gemma4:31b` dapat diganti lewat `[ollama] live_model`. Fitur numerik dan gambar spektrogram dikirim ke Ollama Cloud; PCM mentah tetap diproses lokal. Pastikan kamu memiliki hak untuk memproses audio tersebut. Referensi web hanya inspirasi kata kunci, bukan jaminan jumlah penonton. Untuk tujuan RTMP manual/Secrets, gunakan metadata yang dihasilkan sebagai referensi dan salin ke platform tujuan.

Fitur API pihak ketiga memerlukan kredensial dan kuota masing-masing. Render video menggunakan gambar Clipdrop dan FFmpeg lokal; upload dilakukan ke channel yang dipilih dengan visibilitas yang dipilih pengguna.
