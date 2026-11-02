import hashlib
import html
import os
import secrets
import shutil
import time
from functools import partial
from pathlib import Path
from urllib.parse import quote

import pandas as pd
import streamlit as st

import audio_analysis as audioanalysis
import mediatools as mt
import services as svc
import uploader
import ytauth
import ollama_cloud
import music_studio as musicapi

st.set_page_config(page_title="Studio Render, Live & Musik", page_icon="📡", layout="wide")


KIND_LABEL = {"video": "🎞️ Video", "audio": "🎵 Audio", "output": "📦 Hasil",
              "thumb": "🖼️ Thumbnail", "other": "🗂️ Lainnya"}
STATUS_ICON = {"antre": "⏳ antre", "berjalan": "🔄 berjalan", "selesai": "✅ selesai",
               "gagal": "❌ gagal", "dibatalkan": "🚫 dibatalkan", "diulang": "🔁 diulang"}
HINT_LABEL = {"auto": "Otomatis (deteksi isi file)", "video": "Video", "audio": "Audio"}


@st.cache_data(ttl=15, show_spinner=False)
def _disk_info() -> dict:
    return svc.disk_info()


@st.cache_resource
def get_runner(name: str, workers: int) -> svc.JobRunner:
    return svc.JobRunner(workers)


@st.cache_resource
def get_stream() -> svc.StreamManager:
    return svc.StreamManager()


@st.cache_resource
def get_upload_server():
    """Server upload resumable di port sendiri (default 8502) + tunnel opsional."""
    port = int(os.environ.get("UPLOAD_PORT", "8502"))
    token = os.environ.get("UPLOAD_TOKEN")
    if not token:
        try:
            token = st.secrets.get("UPLOAD_TOKEN")
        except Exception:  # noqa: BLE001
            token = None
    token = token or secrets.token_urlsafe(18)
    srv = uploader.UploadServer(port, token)
    srv.start()
    return srv, uploader.Tunnel(port)


dl_jobs = get_runner("download", 2)   # 2 unduhan paralel
jobs = get_runner("process", 1)       # render/merge satu per satu (hemat CPU/RAM)
stream = get_stream()

if shutil.which("ffmpeg") is None:
    st.error("ffmpeg tidak ditemukan. Pastikan `packages.txt` berisi `ffmpeg`.")
    st.stop()


def protected_files():
    return list(stream.files) if stream.running else []


def jobs_table(rows, key=""):
    if not rows:
        st.caption("Belum ada job.")
        return
    df = pd.DataFrame(rows)
    df["Status"] = df["Status"].map(lambda s: STATUS_ICON.get(s, s))
    st.dataframe(df, hide_index=True, width="stretch")
    failed = [r for r in rows if r["Status"] == "gagal"]
    if failed:
        with st.expander(f"❌ Detail error ({len(failed)} gagal)", expanded=True):
            for r in failed[:8]:
                st.markdown(f"**{r['Job']}** · `{r['ID']}`")
                st.code(str(r["Info"]) or "(tanpa pesan)", language="text")


# ───────────── sidebar ─────────────
with st.sidebar:
    st.title("📡 Studio")
    di = _disk_info()
    c1, c2 = st.columns(2)
    c1.metric("Disk kosong", f"{di['free_gb']:.1f} GB")
    c2.metric("Data app", f"{di['data_gb']:.2f} GB")
    disk_min = st.number_input("Batas minimum sisa disk (GB)", 0.0, 50.0, 0.5, 0.1, key="disk_min",
                               help="Unduh, render, dan merge dibatalkan jika sisa disk di bawah angka ini.")
    st.caption(f"Antrean — unduh: {dl_jobs.pending()} · render/merge: {jobs.pending()}")
    st.caption("🔴 Live: " + ("berjalan" if stream.running else "mati"))
    if st.button("🧹 Bersihkan folder tmp", width="stretch"):
        if dl_jobs.pending() or jobs.pending():
            st.toast("Tunggu semua job selesai dulu.")
        else:
            svc.clear_tmp()
            st.toast("Folder tmp dibersihkan")
    with st.expander("🔧 Komponen"):
        ts = svc.tool_status()
        st.write(("✅" if ts["ffmpeg"] else "❌") + " ffmpeg")
        st.write(("✅" if ts["deno"] else "⚠️") + " deno (untuk YouTube)")
        st.write(("✅ yt-dlp " + ts["yt-dlp"]) if ts["yt-dlp"] else "❌ yt-dlp")
    st.divider()
    st.caption("⚠️ Penyimpanan Streamlit Cloud tidak permanen. File bisa hilang saat app "
               "restart/redeploy; download hasil penting dari tab Library.")

st.title("Studio Render, Live & Musik")

tab_lib, tab_imp, tab_shorts, tab_norm, tab_live, tab_music, tab_jobs = st.tabs(
    ["📁 Library", "⬇️ Impor", "📱 Viral Shorts", "🧰 Normalisasi & Tes", "🔴 Live 24 Jam", "🎼 Musik AI", "⚙️ Job"])

# ───────────── LIBRARY ─────────────
with tab_lib:
    counts = {k: len(svc.list_paths(k)) for k in svc.DIRS}
    kind = st.radio("Folder", list(svc.DIRS), horizontal=True, key="lib_kind",
                    format_func=lambda k: f"{KIND_LABEL[k]} ({counts[k]})")
    files = svc.list_files(kind)
    if not files:
        st.info("Folder kosong. Tambahkan file lewat tab **Impor**.")
    else:
        q = st.text_input("🔎 Cari file", key=f"lib_q_{kind}", placeholder="ketik sebagian nama file…")
        shown = [f for f in files if q.lower() in f["name"].lower()] if q else files
        ver_k, all_k = f"tv_{kind}", f"tall_{kind}"
        s1, s2, s3 = st.columns([1, 1, 4])
        if s1.button("☑️ Pilih semua", key=f"sa_{kind}", disabled=not shown, width="stretch"):
            st.session_state[all_k] = True
            st.session_state[ver_k] = st.session_state.get(ver_k, 0) + 1
            st.rerun()
        if s2.button("⬜ Kosongkan", key=f"sn_{kind}", width="stretch"):
            st.session_state[all_k] = False
            st.session_state[ver_k] = st.session_state.get(ver_k, 0) + 1
            st.rerun()
        s3.caption(f"{len(shown)} file ditampilkan" + (f" dari {len(files)}" if q else "")
                   + " · centang manual per baris, atau pakai Pilih semua.")
        df = pd.DataFrame({"Pilih": bool(st.session_state.get(all_k, False)) if shown else [],
                           "File": [f["name"] for f in shown],
                           "MB": [round(f["mb"], 1) for f in shown],
                           "Diubah": [f["mtime"] for f in shown]})
        # kunci tabel ikut berubah saat daftar file / filter berubah, agar centang tidak bergeser ke baris lain
        sig = hashlib.md5("|".join(f["name"] for f in shown).encode()).hexdigest()[:8]
        tkey = f"tbl_{kind}_{st.session_state.get(ver_k, 0)}_{sig}"
        edited = st.data_editor(df, key=tkey, hide_index=True, width="stretch",
                                disabled=["File", "MB", "Diubah"],
                                column_config={"Pilih": st.column_config.CheckboxColumn("Pilih", width="small")})
        chosen = [svc.DIRS[kind] / n for n in edited.loc[edited["Pilih"], "File"]]
        if chosen:
            tot_mb = sum(f["mb"] for f in shown if f["name"] in {c.name for c in chosen})
            st.caption(f"✔ {len(chosen)} file dipilih · {tot_mb:,.1f} MB")
            a_, b_, c_ = st.columns([1, 1, 1])
            with a_.popover(f"🗑️ Hapus {len(chosen)} file", width="stretch"):
                st.warning(f"{len(chosen)} file akan dihapus permanen.")
                if st.button("Ya, hapus sekarang", type="primary", key=f"lib_del_{kind}"):
                    n = svc.delete_files(chosen, protected=protected_files())
                    st.session_state[all_k] = False
                    st.session_state[ver_k] = st.session_state.get(ver_k, 0) + 1
                    skipped = len(chosen) - n
                    st.toast(f"{n} file dihapus" + (f" · {skipped} dilewati (sedang dipakai live)" if skipped else ""))
                    st.rerun()
            dest = b_.selectbox("Pindahkan ke", [k for k in svc.DIRS if k != kind],
                                format_func=lambda k: KIND_LABEL[k], label_visibility="collapsed")
            if c_.button("📂 Pindahkan", key=f"lib_move_{kind}", width="stretch"):
                n = svc.move_files(chosen, dest, protected=protected_files())
                st.session_state[all_k] = False
                st.session_state[ver_k] = st.session_state.get(ver_k, 0) + 1
                st.toast(f"{n} file dipindahkan ke {KIND_LABEL[dest]}")
                st.rerun()
        with st.expander("Preview & download"):
            paths = svc.list_paths(kind)
            sel = st.selectbox("File", paths, format_func=lambda p: p.name, index=None,
                               placeholder="Pilih file...", key="lib_prev")
            if sel is not None:
                mb = sel.stat().st_size / 1024 / 1024
                _srv, _tn = get_upload_server()
                fq = f"/file?kind={kind}&name={quote(sel.name)}&t={_srv.token}"
                base_url = _tn.url if _tn.running and _tn.url else None
                ext = sel.suffix.lower()
                if ext in svc.AUDIO_EXTS and mb < 200:
                    st.audio(str(sel))
                elif ext in svc.IMAGE_EXTS:
                    st.image(str(sel), width=240)
                elif ext in svc.VIDEO_EXTS and mb < 200:
                    st.video(str(sel))
                elif ext in svc.VIDEO_EXTS | svc.AUDIO_EXTS and base_url:
                    tag = "video" if ext in svc.VIDEO_EXTS else "audio"
                    st.components.v1.html(
                        f'<{tag} controls preload="metadata" style="width:100%;max-height:420px" '
                        f'src="{base_url}{fq}"></{tag}>', height=440 if tag == "video" else 60)
                else:
                    st.caption("Preview file besar butuh tunnel (buka dari tab Impor → Upload file besar).")
                if mb < 300:
                    st.download_button("⬇️ Download ke komputer", sel.read_bytes(),
                                       file_name=sel.name, key="lib_dl")
                elif base_url:
                    st.link_button(f"⬇️ Download {mb:.0f} MB (streaming, bisa dilanjut)",
                                   f"{base_url}{fq}&dl=1")
                else:
                    st.caption(f"File {mb:.0f} MB: buka tunnel di tab Impor dulu agar link download "
                               "streaming muncul (tanpa batas ukuran, tidak memakai RAM).")

# ───────────── IMPOR ─────────────
with tab_imp:
    def _cookie_text() -> str:
        """Cookies dari file upload (prioritas) atau dari kotak teks."""
        ss = st.session_state
        up_file = ss.get(f"ck_up_{ss.get('ck_up_n', 0)}")
        if up_file is not None:
            return up_file.getvalue().decode("utf-8", errors="replace")
        return ss.get("imp_cookies", "")

    def queue_downloads():
        """Callback tombol: antrekan semua link, lalu hapus link & cookies dari form."""
        ss = st.session_state
        urls_ = svc.parse_urls(ss.get("imp_raw", ""))
        if not urls_:
            return
        audio_opts = {"codec": ss.get("au_codec", "mp3"), "bitrate": ss.get("au_bitrate", "192"),
                      "embed": ss.get("au_embed", True), "thumb": ss.get("au_thumb", True)}
        box = None
        ctext = _cookie_text().strip()
        if ctext and any(svc.classify_url(u) == "web" for u in urls_):
            try:
                netscape, _, _ = svc.parse_cookies(ctext)
            except ValueError as e:
                ss["imp_msg"] = ("error", f"Cookies tidak valid: {e}")
                return
            box = svc.CookieBox(netscape)
        runner = get_runner("download", 2)
        for u in urls_:
            use = box if (box is not None and svc.classify_url(u) == "web") else None
            runner.submit(
                svc.short_title(u),
                partial(svc.run_download, url=u, hint=ss.get("imp_hint", "auto"),
                        height=ss.get("imp_height", 720), noplaylist=ss.get("imp_noplaylist", True),
                        disk_min_gb=ss.get("disk_min", 0.5), audio_opts=audio_opts, cookies=use),
                cleanup=use.make_releaser() if use is not None else None)
        ss["imp_raw"] = ""
        ss["imp_cookies"] = ""
        ss["ck_up_n"] = ss.get("ck_up_n", 0) + 1     # reset uploader cookies
        ss["imp_msg"] = ("success", f"{len(urls_)} unduhan diantrekan"
                                    + (" (dengan cookies)" if box is not None else ""))

    with st.container(border=True):
        st.subheader("🔗 Impor dari link")
        st.caption("Tempel banyak link sekaligus, dipisah koma, spasi, atau baris baru. "
                   "Jenis link dikenali otomatis: Google Drive (file/folder), YouTube dan situs "
                   "lain yang didukung yt-dlp, atau link langsung ke file.")
        flash = st.session_state.pop("imp_msg", None)
        if flash:
            (st.success if flash[0] == "success" else st.error)(flash[1])
        raw = st.text_area("Link", height=150, key="imp_raw", label_visibility="collapsed",
                           placeholder="https://drive.google.com/file/d/.../view, https://youtu.be/...")
        urls = svc.parse_urls(raw)
        o1, o2, o3 = st.columns(3)
        hint = o1.radio("Simpan sebagai", list(HINT_LABEL), format_func=HINT_LABEL.get, key="imp_hint",
                        help="Otomatis memeriksa isi file dengan ffprobe lalu memasukkannya ke folder "
                             "yang tepat. Pilih Audio untuk mengambil audio (MP3/M4A) dari link YouTube.")
        o2.selectbox("Maks. tinggi video (yt-dlp)", [360, 480, 720, 1080], index=2, key="imp_height",
                     disabled=hint == "audio")
        o3.checkbox("Abaikan playlist (1 video saja)", value=True, key="imp_noplaylist",
                    help="Matikan untuk mengunduh seluruh playlist dari link playlist.")

        with st.expander("🎧 Opsi audio (MP3 / M4A, cover, thumbnail)", expanded=(hint == "audio")):
            a1, a2 = st.columns(2)
            a1.selectbox("Format", ["mp3", "m4a"], key="au_codec",
                         format_func=lambda c: {"mp3": "MP3", "m4a": "M4A (AAC)"}[c])
            a2.selectbox("Bitrate (kbps)", ["320", "256", "192", "128"], index=2, key="au_bitrate")
            st.checkbox("Sematkan cover & metadata ke dalam file audio", value=True, key="au_embed")
            st.checkbox("Simpan thumbnail .jpg terpisah (folder Thumbnail)", value=True, key="au_thumb")
            st.caption("Berlaku saat 'Simpan sebagai' = Audio. Bitrate di atas kualitas asli YouTube tidak "
                       "menambah kualitas; 192 kbps cukup untuk kebanyakan kasus. Jika cover/thumbnail "
                       "gagal diproses, unduhan otomatis diulang dengan opsi lebih sederhana.")

        with st.expander("🍪 Cookies untuk yt-dlp (opsional)"):
            st.caption("Untuk video yang meminta login atau error \"not a bot\". Login ke YouTube di browser → "
                       "ekstensi Cookie-Editor → Export → JSON → upload file atau tempel di bawah. "
                       "Dipakai untuk semua link web/yt-dlp dalam satu batch (bulk), bukan untuk Google Drive.")
            ck_n = st.session_state.setdefault("ck_up_n", 0)
            st.file_uploader("Upload cookies (.json / .txt)", type=["json", "txt"], key=f"ck_up_{ck_n}")
            st.text_area("Atau tempel cookies JSON", height=120, key="imp_cookies",
                         placeholder="[{\"domain\": \".youtube.com\", \"name\": \"...\", \"value\": \"...\"}, ...]")
            ctext = _cookie_text().strip()
            if ctext:
                try:
                    _, n_cookie, doms = svc.parse_cookies(ctext)
                    st.success(f"{n_cookie} cookie terbaca · domain: {', '.join(doms[:6])}"
                               + (" …" if len(doms) > 6 else ""))
                except ValueError as e:
                    st.error(f"Cookies tidak valid: {e}")
            st.warning("Cookies = akses ke akunmu. Jangan dibagikan atau di-screenshot. Cookies hanya "
                       "disimpan di memori server selama batch berjalan, ditulis ke file sementara "
                       "per unduhan, lalu dihapus. Form dikosongkan otomatis setelah diantrekan. "
                       "Pakai akun cadangan dan jaga agar app ini privat.")

        if urls:
            st.dataframe(
                pd.DataFrame({"No": range(1, len(urls) + 1),
                              "Sumber": [svc.SOURCE_LABEL[svc.classify_url(u)] for u in urls],
                              "Link": [u[:90] for u in urls]}),
                hide_index=True, width="stretch")
        st.button(f"⬇️ Mulai unduh {len(urls)} link", type="primary", disabled=not urls,
                  key="imp_go", on_click=queue_downloads)

    @st.fragment(run_every=2)
    def dl_progress():
        c = dl_jobs.counts()
        total = sum(c.values())
        if not total:
            return
        done = c["selesai"] + c["gagal"]
        st.progress(done / total, text=f"{done}/{total} selesai · {c['berjalan']} berjalan · "
                                       f"{c['antre']} antre · {c['gagal']} gagal")
        jobs_table(dl_jobs.snapshot()[:30])

    dl_progress()
    r1, r2, r3 = st.columns(3)
    if r1.button("🔁 Coba ulang yang gagal", key="dl_retry"):
        st.toast(f"{dl_jobs.retry_failed()} unduhan diulang")
    if r2.button("🚫 Batalkan yang antre", key="dl_cancel"):
        st.toast(f"{dl_jobs.cancel_queued()} dibatalkan")
    if r3.button("🧽 Bersihkan riwayat", key="dl_clear"):
        dl_jobs.clear_finished()
        st.rerun()

    with st.container(border=True):
        st.subheader("📥 Upload dari komputer")
        up_n = st.session_state.setdefault("up_n", 0)
        for msg in st.session_state.pop("up_msg", []):
            st.success(msg)
        up = st.file_uploader("Pilih file apa saja (video, audio, dll.)", accept_multiple_files=True,
                              key=f"imp_up_{up_n}")
        if up and st.button(f"💾 Simpan {len(up)} file (mode: {HINT_LABEL[hint]})", key="up_go"):
            msgs = []
            for f in up:
                k, dest = svc.import_bytes(f.name, f.getbuffer(), hint)
                msgs.append(f"{dest.name} → {KIND_LABEL[k]}")
            st.session_state["up_msg"] = msgs
            st.session_state["up_n"] = up_n + 1
            st.rerun()
        st.caption("Upload lewat tombol di atas dibatasi (default 200 MB per file). Untuk file besar, "
                   "pakai upload resumable di bawah atau link Google Drive.")

    with st.expander("🚀 Upload file besar (pilih file di browser, resumable)", expanded=True):
        up_srv, up_tunnel = get_upload_server()
        if up_srv.error:
            st.error(up_srv.error)
        else:
            st.markdown("File besar dikirim per potongan langsung ke folder internal app (tanpa batas 200 MB). "
                        "Jika koneksi putus, pilih file yang sama lagi — upload **lanjut** dari bagian terakhir.")
            st.markdown("Streamlit Cloud hanya membuka satu port, jadi halaman upload dibuka lewat tunnel "
                        "publik (cloudflared, tanpa akun).")
            t1, t2 = st.columns(2)
            if t1.button("🌐 Buka tunnel upload", key="tn_open", disabled=up_tunnel.running):
                up_tunnel.start()
            if t2.button("⛔ Tutup tunnel", key="tn_close", disabled=not up_tunnel.running):
                up_tunnel.stop()

            @st.fragment(run_every=3)
            def tunnel_panel():
                if up_tunnel.error:
                    st.error(up_tunnel.error)
                if up_tunnel.running and not up_tunnel.url:
                    st.info("Menyiapkan tunnel... (beberapa detik)")
                link = f"{up_tunnel.url}/#t={up_srv.token}" if up_tunnel.url else None
                if link:
                    st.link_button("📂 Buka halaman upload (tab baru)", link, type="primary")
                    st.caption("Link ini sudah berisi token — jangan dibagikan. Di HP/komputer lain cukup "
                               "buka link yang sama.")
                else:
                    st.caption(f"Tanpa tunnel (VPS/lokal): buka `http://IP-SERVER:{up_srv.port}/#t=TOKEN`.")
                    st.caption("Token:")
                    st.code(up_srv.token, language="text")
                parts = up_srv.partials()
                if parts:
                    st.caption("Upload terjeda/sedang jalan: " + ", ".join(
                        f"{x['name']} ({x['mb']:.0f} MB)" for x in parts))

            tunnel_panel()
            if up_tunnel.url:
                st.components.v1.iframe(f"{up_tunnel.url}/#t={up_srv.token}", height=520, scrolling=True)

# ───────────── VIRAL SHORTS ─────────────
def _secret_section(name: str) -> dict:
    try:
        value = st.secrets.get(name, {})
        return dict(value) if value else {}
    except Exception:  # noqa: BLE001
        return {}


def _sync_youtube_accounts() -> list[dict]:
    try:
        return ytauth.saved_accounts()
    except Exception:  # noqa: BLE001
        return []


def _youtube_account_access_token(cfg: dict, account_id: str | None = None) -> tuple[str, dict]:
    return ytauth.account_access_token(cfg, account_id)


with tab_shorts:
    st.caption("Susun kartu kata 9:16 dengan latar Clipdrop, siapkan metadata dengan Ollama Cloud, lalu render dan unggah ke YouTube Shorts. API key dan OAuth client secret diambil dari **Settings → Secrets** atau kolom sesi di bawah; rahasia OAuth tidak ditampilkan.")
    ollama_shorts_cfg = _secret_section("ollama")
    clipdrop_cfg = _secret_section("clipdrop")
    oauth_shorts_cfg = _secret_section("youtube_oauth")
    shorts_ollama_model = str(ollama_shorts_cfg.get("model") or "gpt-oss:20b")
    shorts_cd_key = st.session_state.get("vs_clipdrop_key", str(clipdrop_cfg.get("api_key") or ""))
    shorts_ollama_key = st.session_state.get("vs_ollama_key", str(ollama_shorts_cfg.get("api_key") or ""))
    with st.expander("🔑 API Clipdrop & Ollama Cloud", expanded=not bool(shorts_cd_key or shorts_ollama_key)):
        shorts_cd_key = st.text_input("Clipdrop API key", value=shorts_cd_key, type="password", key="vs_clipdrop_key")
        shorts_ollama_key = st.text_input("Ollama Cloud API key", value=shorts_ollama_key, type="password", key="vs_ollama_key")
        st.caption("Untuk menghindari input ulang, simpan key di Streamlit Secrets sebagai `[clipdrop] api_key` dan `[ollama] api_key`.")

    # OAuth credentials themselves are read only from secrets/environment, never from a user-visible field.
    oauth_shorts_cfg = {
        "client_id": str(oauth_shorts_cfg.get("client_id") or ""),
        "client_secret": str(oauth_shorts_cfg.get("client_secret") or ""),
        "redirect_uri": str(oauth_shorts_cfg.get("redirect_uri") or "https://redirect1x.streamlit.app/"),
        "state_secret": str(oauth_shorts_cfg.get("state_secret") or oauth_shorts_cfg.get("client_secret") or ""),
    }
    shorts_yt_accounts = _sync_youtube_accounts()
    selected_shorts_acct_id = None
    selected_shorts_channel_title = "YouTube"
    vs_upload_privacy = "public"
    vs_auto_upload_after_render = False
    with st.expander("📤 Channel tujuan & visibilitas upload", expanded=bool(shorts_yt_accounts)):
        if shorts_yt_accounts:
            acct_map = {a["id"]: f"📺 {a.get('channel', {}).get('title', 'Channel YouTube')}" for a in shorts_yt_accounts}
            up_c1, up_c2, up_c3 = st.columns([1.7, 1, 1.4])
            selected_shorts_acct_id = up_c1.selectbox("Channel YouTube", list(acct_map), format_func=lambda aid: acct_map.get(aid, aid), key="vs_yt_account_id")
            chosen_account = next((a for a in shorts_yt_accounts if a["id"] == selected_shorts_acct_id), {})
            selected_shorts_channel_title = (chosen_account.get("channel") or {}).get("title") or "YouTube"
            privacy_opts = {"public": "🌍 Publik", "unlisted": "🔗 Tidak Publik", "private": "🔒 Pribadi"}
            vs_upload_privacy = up_c2.selectbox("Visibilitas", list(privacy_opts), format_func=privacy_opts.get, key="vs_upload_privacy")
            vs_auto_upload_after_render = up_c3.checkbox("🚀 Upload otomatis setelah render", key="vs_auto_upload_yt", help="Video diunggah setelah file MP4 berhasil dibuat.")
        else:
            st.info("Belum ada channel YouTube terhubung. Tambahkan OAuth client di Secrets untuk mengaktifkan upload.")
        if oauth_shorts_cfg["client_id"] and oauth_shorts_cfg["client_secret"] and oauth_shorts_cfg["state_secret"]:
            with st.expander("🔐 Hubungkan / tambah channel YouTube", expanded=not bool(shorts_yt_accounts)):
                state_secret = oauth_shorts_cfg["state_secret"]
                s_state = st.session_state.get("yt_oauth_state")
                if not s_state or not ytauth.verify_state(s_state, state_secret):
                    s_state = ytauth.make_state(state_secret)
                    st.session_state["yt_oauth_state"] = s_state
                s_auth_url = ytauth.authorization_url(oauth_shorts_cfg["client_id"], oauth_shorts_cfg["redirect_uri"], s_state)
                st.link_button("🔐 Login & otorisasi YouTube", s_auth_url, type="primary")
                st.caption("Setelah memberi izin Google, salin URL redirect lengkap dan tempel di bawah. Redirect URI harus sama dengan yang didaftarkan di Google Cloud Console.")
                oc1, oc2 = st.columns([2.5, 1])
                vs_redir_url = oc1.text_input("URL redirect OAuth", type="password", key="vs_yt_redirect_url", placeholder="https://redirect1x.streamlit.app/?state=...&code=...")
                if oc2.button("🔄 Simpan channel", key="vs_yt_oauth_exchange", width="stretch"):
                    try:
                        code = ytauth.parse_redirect_url(vs_redir_url, st.session_state.get("yt_oauth_state", ""), state_secret)
                        token = ytauth.exchange_code(oauth_shorts_cfg["client_id"], oauth_shorts_cfg["client_secret"], oauth_shorts_cfg["redirect_uri"], code)
                        channel = ytauth.get_my_channel(token["access_token"])
                        ytauth.upsert_saved_account(token, channel)
                        st.session_state.pop("yt_oauth_state", None)
                        st.success(f"Channel siap digunakan: {channel['title']}")
                        st.rerun()
                    except Exception as exc:  # noqa: BLE001
                        st.error(f"Gagal menghubungkan channel: {exc}")
        elif not shorts_yt_accounts:
            st.caption("Tambahkan `[youtube_oauth] client_id`, `client_secret`, dan `redirect_uri` di Streamlit Secrets.")

    for key, default in {
        "vs_header": "Sehat Selalu Istriku ❤️",
        "vs_body": "Semoga selalu sehat, kuat, dan bahagia.\n\nTerima kasih sudah menjadi pendamping hidupku.\n\nMari bersama sampai tua nanti.",
        "vs_clipdrop_prompt": "cinematic warm sunset, peaceful Indonesian home, soft golden light, elegant floral details, vertical composition, no text, no letters",
        "vs_yt_title": "Sehat Selalu Istriku ❤️ Pesan Menyentuh Hati #Shorts",
        "vs_yt_desc": "Pesan tulus dan doa untuk orang tersayang.\n\n#Shorts #KataKataMenyentuhHati #QuotesIndonesia",
        "vs_yt_tags": "shorts, pesan menyentuh hati, quotes indonesia, kata kata bijak",
    }.items():
        st.session_state.setdefault(key, default)
    pending_draft = st.session_state.pop("_vs_pending_draft", None)
    if isinstance(pending_draft, dict):
        for src, dst in (("header", "vs_header"), ("body", "vs_body"), ("clipdrop_prompt", "vs_clipdrop_prompt"),
                         ("youtube_title", "vs_yt_title"), ("youtube_description", "vs_yt_desc")):
            if pending_draft.get(src):
                st.session_state[dst] = pending_draft[src]
        if pending_draft.get("youtube_tags"):
            tags = pending_draft["youtube_tags"]
            st.session_state["vs_yt_tags"] = ", ".join(tags) if isinstance(tags, list) else str(tags)
        st.session_state["vs_last_research"] = pending_draft

    topic_map = {"relationship": "❤️ Cinta & rumah tangga", "motivation": "✨ Motivasi", "family": "👨‍👩‍👧 Keluarga", "wisdom": "🌱 Kata bijak", "custom": "✍️ Topik kustom"}
    topic_key = st.selectbox("🎯 Tema Shorts", list(topic_map), format_func=topic_map.get, key="vs_topic")
    custom_topic = st.text_input("Fokus / kata kunci tambahan", key="vs_custom_topic", placeholder="Contoh: doa untuk istri yang sedang sakit", disabled=topic_key != "custom")
    watermark = st.text_input("Watermark / nama akun (opsional)", key="vs_watermark", placeholder="@namaakun")
    if topic_key == "custom" and not custom_topic.strip():
        st.info("Isi fokus spesifik agar Ollama dapat menyusun naskah sesuai topik.")

    ed_col, prev_col = st.columns([1.45, 1])
    with ed_col:
        draft_header = st.text_input("Judul pita / headline kartu", max_chars=100, key="vs_header")
        draft_body = st.text_area("Isi bait (pisahkan paragraf dengan satu baris kosong)", height=220, key="vs_body")
        draft_cd_prompt = st.text_area("Prompt gambar latar Clipdrop (bahasa Inggris, tanpa teks)", height=90, key="vs_clipdrop_prompt")
        draft_yt_title = st.text_input("Judul YouTube Shorts (maks. 100 karakter)", max_chars=100, key="vs_yt_title")
        draft_yt_desc = st.text_area("Deskripsi, ajakan komentar & hashtag", height=100, key="vs_yt_desc")
        draft_yt_tags = st.text_input("Tags (pisahkan dengan koma)", key="vs_yt_tags")
        audio_choices = [""] + [str(p) for p in svc.list_paths("audio")]
        audio_choice = st.selectbox("Audio latar (opsional)", audio_choices, format_func=lambda p: "Tanpa audio" if not p else Path(p).name, key="vs_audio_choice")
        audio_volume = st.slider("Volume audio (%)", 0, 200, 100, key="vs_audio_vol")
        out_name = st.text_input("Nama file (opsional)", key="vs_output_name", placeholder="Otomatis dari judul")
        auto_upload = vs_auto_upload_after_render
        can_upload = bool(shorts_yt_accounts and oauth_shorts_cfg["client_secret"])
        if auto_upload and not can_upload:
            st.warning("Hubungkan channel YouTube dahulu atau nonaktifkan upload otomatis.")
        b1, b2 = st.columns(2)
        if b1.button("🔍 Buat draf tren + metadata (Ollama)", key="vs_btn_research_draft", width="stretch"):
            if not shorts_ollama_key:
                st.error("Tambahkan API key Ollama Cloud di Secrets atau kolom di atas.")
            elif topic_key == "custom" and not custom_topic.strip():
                st.error("Isi fokus topik kustom terlebih dahulu.")
            else:
                try:
                    opt_tok = _youtube_account_access_token(oauth_shorts_cfg, selected_shorts_acct_id)[0] if shorts_yt_accounts else ""
                    with st.spinner("Ollama sedang menyusun naskah dan metadata..."):
                        result = ollama_cloud.generate_viral_short_script(shorts_ollama_key, topic_key=topic_key, custom_topic=custom_topic.strip(), watermark=watermark.strip(), model=shorts_ollama_model, youtube_access_token=opt_tok)
                    st.session_state["_vs_pending_draft"] = result
                    st.rerun()
                except Exception as exc:  # noqa: BLE001
                    st.error(f"Gagal membuat draf: {exc}")
        if b2.button("✨ Optimalkan metadata (Ollama)", key="vs_btn_regen_draft_seo", width="stretch"):
            if not shorts_ollama_key:
                st.error("Tambahkan API key Ollama Cloud terlebih dahulu.")
            elif not draft_header.strip() and not draft_body.strip():
                st.error("Isi headline atau bait terlebih dahulu.")
            else:
                try:
                    opt_tok = _youtube_account_access_token(oauth_shorts_cfg, selected_shorts_acct_id)[0] if shorts_yt_accounts else ""
                    with st.spinner("Ollama meracik judul, deskripsi, hashtag, dan tags..."):
                        result = ollama_cloud.generate_viral_short_upload_metadata(shorts_ollama_key, header=draft_header, body=draft_body, topic_key=topic_key, custom_topic=custom_topic.strip(), watermark=watermark.strip(), model=shorts_ollama_model, youtube_access_token=opt_tok)
                    st.session_state["_vs_pending_draft"] = result
                    st.rerun()
                except Exception as exc:  # noqa: BLE001
                    st.error(f"Gagal membuat metadata: {exc}")
        render_label = "🎬 Render + upload Shorts" if auto_upload else "🎬 Render video Shorts 9:16"
        if st.button(render_label, key="vs_btn_render_draft", type="primary", width="stretch", disabled=auto_upload and not can_upload):
            if not shorts_cd_key:
                st.error("Tambahkan Clipdrop API key terlebih dahulu.")
            elif not draft_header.strip() or not draft_body.strip():
                st.error("Headline dan isi kartu wajib diisi.")
            elif not draft_cd_prompt.strip():
                st.error("Prompt latar Clipdrop wajib diisi.")
            else:
                try:
                    yt_tok, yt_ch_title = _youtube_account_access_token(oauth_shorts_cfg, selected_shorts_acct_id) if auto_upload else ("", "YouTube")
                    last_res = st.session_state.get("vs_last_research") or {}
                    job = svc.submit_viral_short_video(jobs, clipdrop_api_key=shorts_cd_key.strip(), header=draft_header.strip(), body=draft_body.strip(), clipdrop_prompt=draft_cd_prompt.strip(), youtube_title=draft_yt_title.strip(), youtube_description=draft_yt_desc, youtube_tags=draft_yt_tags, output_name=out_name.strip(), youtube_access_token=yt_tok, youtube_references=last_res.get("youtube_references"), trending_keywords=last_res.get("trending_keywords"), topic_key=topic_key, custom_topic=custom_topic.strip(), watermark=watermark.strip(), audio_path=audio_choice or None, audio_volume=audio_volume, disk_min_gb=float(disk_min), auto_upload_youtube=bool(auto_upload), upload_privacy=vs_upload_privacy, upload_channel_title=yt_ch_title)
                    st.success(f"Render Shorts diantrekan · Job `{job['id']}`")
                except Exception as exc:  # noqa: BLE001
                    st.error(f"Gagal mengantrekan render: {exc}")
    with prev_col:
        st.markdown("#### 🪄 Pratinjau kartu 9:16")
        st.markdown(f"<div style='aspect-ratio:9/16;max-width:320px;margin:auto;padding:28px;display:flex;flex-direction:column;justify-content:center;border-radius:24px;color:white;text-align:center;background:linear-gradient(145deg,#19233e,#b8615e 55%,#1b1028);box-shadow:0 12px 40px #0005'><div style='font-size:25px;font-weight:800'>{html.escape(draft_header or 'Headline Shorts')}</div><div style='white-space:pre-wrap;font-size:16px;margin-top:22px'>{html.escape(draft_body or 'Isi kata-kata tampil di sini')}</div><small style='margin-top:26px'>{html.escape(watermark)}</small></div>", unsafe_allow_html=True)
        st.caption("Pratinjau ini hanya simulasi tata letak; video akhir memakai latar gambar Clipdrop.")
        st.markdown("#### ⚙️ Job Shorts")

        @st.fragment(run_every=3)
        def viral_shorts_job_panel():
            short_rows = [r for r in jobs.snapshot() if str(r.get("Job", "")).startswith(("Viral Shorts", "Upload Shorts → YouTube:"))]
            jobs_table(short_rows[:8], key="viral_shorts_jobs")

        viral_shorts_job_panel()

    st.divider()
    st.markdown("#### 🚀 Upload video Shorts yang sudah ada")
    recent_shorts = svc.list_recent_shorts()
    shorts_map = {str(Path(r["video_path"]).resolve()): r for r in recent_shorts if r.get("video_path")}
    available = list(dict.fromkeys([*shorts_map.keys(), *[str(p.resolve()) for p in svc.library_videos()]]))
    if not available:
        st.caption("Belum ada video. Render video lewat editor di atas atau impor video 9:16 ke Library.")
    else:
        def _short_label(path: str) -> str:
            rec = shorts_map.get(str(Path(path).resolve()), {})
            prefix = "✅ Sudah di-upload" if rec.get("shorts_url") else "📌 Siap upload"
            return f"{prefix} · {Path(path).name} — {(rec.get('youtube_title') or rec.get('header') or '')[:48]}"
        picked_vpath_str = st.selectbox("Pilih video", available, format_func=_short_label, key="vs_pick_upload_video")
        picked_vpath = Path(picked_vpath_str)
        picked_rec = shorts_map.get(str(picked_vpath.resolve()), {})
        if st.session_state.get("_vs_last_loaded_vid") != picked_vpath_str:
            st.session_state["vs_up_title"] = picked_rec.get("youtube_title") or f"{picked_vpath.stem.replace('_', ' ')} #Shorts"[:100]
            st.session_state["vs_up_desc"] = picked_rec.get("youtube_description") or "Pesan penuh makna. Bagikan ke orang tersayangmu.\n\n#Shorts #QuotesIndonesia"
            st.session_state["vs_up_tags"] = ", ".join(picked_rec.get("youtube_tags") or ["shorts", "quotes indonesia", "kata kata menyentuh hati"])
            st.session_state["_vs_last_loaded_vid"] = picked_vpath_str
        upc1, upc2 = st.columns([1, 1.5])
        with upc1:
            if picked_vpath.is_file():
                st.video(str(picked_vpath))
                if picked_vpath.stat().st_size <= 80 * 1024 * 1024:
                    st.download_button("⬇️ Download MP4", picked_vpath.read_bytes(), file_name=picked_vpath.name, mime="video/mp4", key="vs_download_mp4")
        with upc2:
            if picked_rec.get("shorts_url"):
                st.success(f"✅ Terupload ke {picked_rec.get('uploaded_channel', 'YouTube')} · {picked_rec.get('uploaded_privacy', 'public')} · {picked_rec.get('uploaded_at', '')}")
                st.link_button("▶️ Buka YouTube Shorts", picked_rec["shorts_url"], type="primary")
            up_title_val = st.text_input("Judul YouTube Shorts", max_chars=100, key="vs_up_title")
            up_desc_val = st.text_area("Deskripsi & hashtag", height=110, key="vs_up_desc")
            up_tags_val = st.text_input("Tags / keywords", key="vs_up_tags")
            if st.button("🚀 Upload ke YouTube Shorts", key="vs_btn_upload_short_now", type="primary", disabled=not shorts_yt_accounts, width="stretch"):
                if not up_title_val.strip():
                    st.error("Judul YouTube Shorts tidak boleh kosong.")
                else:
                    try:
                        yt_tok, yt_channel = _youtube_account_access_token(oauth_shorts_cfg, selected_shorts_acct_id)
                        job = svc.submit_upload_short_to_youtube(jobs, video_path=picked_vpath, youtube_access_token=yt_tok, channel_title=yt_channel.get("title", selected_shorts_channel_title), privacy=vs_upload_privacy, title=up_title_val, description=up_desc_val, tags=up_tags_val, auto_ollama_metadata=False)
                        st.success(f"Upload diantrekan · Job `{job['id']}`")
                    except Exception as exc:  # noqa: BLE001
                        st.error(f"Gagal mengantrekan upload: {exc}")
            if st.button("⚡ Ollama + langsung upload", key="vs_btn_ollama_and_upload", disabled=not (shorts_yt_accounts and shorts_ollama_key), width="stretch"):
                try:
                    yt_tok, yt_channel = _youtube_account_access_token(oauth_shorts_cfg, selected_shorts_acct_id)
                    job = svc.submit_upload_short_to_youtube(jobs, video_path=picked_vpath, youtube_access_token=yt_tok, channel_title=yt_channel.get("title", selected_shorts_channel_title), privacy=vs_upload_privacy, title=up_title_val, description=up_desc_val, tags=up_tags_val, auto_ollama_metadata=True, ollama_api_key=shorts_ollama_key, ollama_model=shorts_ollama_model, header=picked_rec.get("header") or picked_vpath.stem, body=picked_rec.get("body") or picked_vpath.stem, topic_key=picked_rec.get("topic_key") or topic_key, custom_topic=custom_topic.strip(), watermark=picked_rec.get("watermark") or watermark.strip())
                    st.success(f"Ollama + upload diantrekan · Job `{job['id']}`")
                except Exception as exc:  # noqa: BLE001
                    st.error(f"Gagal mengantrekan Ollama + upload: {exc}")

# ───────────── NORMALISASI & TES ─────────────
LMODE = {"two": "Presisi (2-pass, disarankan)", "one": "Cepat (1-pass)",
         "off": "Tanpa loudness (hanya samakan format)"}
OUTMODE = {"new": "File baru (*_norm), aslinya dipertahankan", "replace": "Ganti file asli (setelah lolos verifikasi)"}
DECMODE = {"none": "Tidak", "quick": "Cepat (60 dtk awal + akhir)", "full": "Penuh (lambat, paling teliti)"}


def _pick_files(prefix: str, paths: list, noun: str) -> list:
    if st.checkbox(f"Semua {noun} ({len(paths)})", value=True, key=f"mt_{prefix}_all"):
        return list(paths)
    return st.multiselect(f"Pilih {noun}", paths, format_func=svc.label, key=f"mt_{prefix}_sel")


def _report(rows: list, kind: str, key: str, tgt_audio=None) -> dict:
    S = mt.summarize(rows, kind, tgt_audio)
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Diuji", S["total"])
    c2.metric("✅ Seragam & aman", S["ok"])
    c3.metric("⚠️ Tidak seragam", S["warn"])
    c4.metric("❌ Bermasalah", S["bad"])
    if S["uniform"]:
        st.success("Semua file seragam dan aman dipakai.")
    elif S["bad"]:
        st.error(f"{S['bad']} file bermasalah (rusak / terpotong / senyap). Jangan dipakai live: ganti atau hapus.")
    else:
        st.warning(f"{S['warn']} file belum seragam. Jalankan normalisasi di bawah.")
    for n in S["notes"]:
        st.info(n)
    if S["groups"]:
        st.caption("Format terdeteksi: " + " · ".join(f"{g} (×{n})" for g, n in S["groups"]))
    only = st.checkbox("Tampilkan hanya yang tidak OK", value=S["total"] > 8, key=f"mt_{key}_only")
    df = pd.DataFrame(rows)
    if only:
        df = df[df["Status"] != mt.OK]
    if len(df):
        st.dataframe(df.drop(columns=["_path"]), hide_index=True, width="stretch")
    return S


with tab_norm:
    st.caption("Samakan format semua file agar live stabil dan volume tidak naik-turun: audio → AAC .m4a "
               "(sample rate, channel, bitrate, loudness seragam), video → H.264 dengan ukuran & FPS seragam. "
               "Alur: **Tes** dulu untuk melihat mana yang tidak seragam, lalu **Normalisasi**. Hasil ditulis ke "
               "file sementara, diverifikasi, baru masuk Library.")
    if stream.running:
        st.warning("Live sedang berjalan. Tes & normalisasi berjalan dengan prioritas rendah, tetapi tetap memakai "
                   "CPU; untuk live yang paling aman, lakukan saat live mati atau batasi thread di bawah.")
    n_aud, n_vid = st.tabs(["🎵 Audio (AAC / M4A)", "🎞️ Video (H.264 seragam)"])

    # ---------------- AUDIO ----------------
    with n_aud:
        apaths = svc.list_paths("audio")
        if not apaths:
            st.info("Folder audio kosong. Tambahkan file lewat tab **Impor**.")
        else:
            xf_now = float(st.session_state.get("l_xfmax", 8.0)) if st.session_state.get("l_xfon", True) else 0.0
            with st.expander("🎯 Target audio seragam", expanded=True):
                c1, c2, c3, c4 = st.columns(4)
                a_sr = c1.selectbox("Sample rate (Hz)", [44100, 48000], key="mt_a_sr",
                                    help="44100 = sama dengan jalur live (tanpa resample tambahan).")
                a_ch = c2.selectbox("Channel", [2, 1], key="mt_a_ch", format_func={2: "Stereo", 1: "Mono"}.get)
                a_br = c3.selectbox("Bitrate AAC (kbps)", [128, 160, 192, 256], index=1, key="mt_a_br")
                a_lufs = c4.number_input("Loudness target (LUFS)", -30.0, -8.0, -16.0, 0.5, key="mt_a_lufs",
                                         help="-16 LUFS = umum untuk musik streaming/YouTube. Semua lagu dibawa ke "
                                              "angka ini sehingga volume antar lagu sama.")
                d1, d2 = st.columns(2)
                a_tol = d1.number_input("Toleransi selisih loudness (LU)", 0.5, 6.0, 2.0, 0.5, key="mt_a_tol")
                a_tp = d2.number_input("Batas true-peak (dBTP)", -6.0, -0.1, -1.5, 0.1, key="mt_a_tp",
                                       help="Puncak sinyal maksimum; -1.5 memberi ruang agar tidak clipping saat di-mix.")
            tgt_a = mt.AudioTarget(sr=a_sr, ch=a_ch, bitrate_k=a_br, lufs=a_lufs, tp=a_tp, tol_lu=a_tol,
                                   min_dur=3 * xf_now)

            st.markdown("##### 🧪 Tes audio")
            sel_a = _pick_files("a", apaths, "file audio")
            t1, t2 = st.columns(2)
            do_loud = t1.checkbox("Ukur loudness (LUFS & puncak)", value=True, key="mt_a_loud")
            do_dec = t2.checkbox("Tes decode penuh (deteksi file rusak / terpotong)", value=True, key="mt_a_dec")
            if st.button(f"🧪 Tes {len(sel_a)} file audio", type="primary", key="mt_a_test", disabled=not sel_a):
                bar = st.progress(0.0, text="Menguji...")
                st.session_state["mt_a_rows"] = mt.run_tests(
                    sel_a, lambda p: mt.audio_row(p, tgt_a, do_loud, do_dec),
                    progress=lambda d, t: bar.progress(d / t, text=f"{d}/{t} file diuji"))
                bar.empty()
            rows_a = st.session_state.get("mt_a_rows") or []
            if rows_a:
                _report(rows_a, "audio", "a", tgt_a)
                st.caption("Hasil tes dihitung pada target saat tes dijalankan. Setelah normalisasi selesai, "
                           "jalankan tes lagi untuk memastikan.")

            st.markdown("##### 🔧 Normalisasi audio")
            n1, n2 = st.columns(2)
            a_lmode = n1.selectbox("Loudness", list(LMODE), format_func=LMODE.get, key="mt_a_lmode")
            a_out = n2.selectbox("Hasil", list(OUTMODE), format_func=OUTMODE.get, key="mt_a_out")
            n3, n4 = st.columns(2)
            a_skip = n3.checkbox("Lewati file yang sudah seragam", value=True, key="mt_a_skip")
            a_strip = n4.checkbox("Buang metadata & cover", value=False, key="mt_a_strip")
            fix_a = [r["_path"] for r in rows_a if r["Status"] == mt.WARN]

            def _submit_a(paths: list) -> None:
                opts = {"mode": a_out, "loud": a_lmode, "skip_ok": a_skip, "strip_meta": a_strip}
                n = mt.submit_normalize(jobs, "audio", paths, tgt_a, opts, disk_min, protected_files)
                st.toast(f"{n} job normalisasi audio diantrekan")

            b1, b2 = st.columns(2)
            if b1.button(f"🔧 Normalisasi {len(fix_a)} file yang tidak seragam", type="primary",
                         key="mt_a_fix", disabled=not fix_a, width="stretch"):
                _submit_a(fix_a)
            if b2.button(f"🔧 Normalisasi semua {len(sel_a)} file terpilih", key="mt_a_fixall",
                         disabled=not sel_a, width="stretch"):
                _submit_a(sel_a)

    # ---------------- VIDEO ----------------
    with n_vid:
        vpaths = svc.library_videos()
        if not vpaths:
            st.info("Belum ada video. Tambahkan file lewat tab **Impor**.")
        else:
            with st.expander("🎯 Target video seragam", expanded=True):
                c1, c2, c3 = st.columns(3)
                v_h = c1.selectbox("Resolusi", [480, 720, 1080], index=1, key="mt_v_h",
                                   format_func=lambda h: f"{h}p ({svc.RES[h][0]}x{svc.RES[h][1]})")
                v_fps = c2.selectbox("FPS", [24, 25, 30], index=2, key="mt_v_fps")
                v_br = c3.selectbox("Bitrate video (kbps)", [0, 1500, 2500, 4000, 6000], key="mt_v_br",
                                    format_func=lambda b: "Auto (sesuai resolusi)" if b == 0 else str(b))
                d1, d2, d3 = st.columns(3)
                v_aud = d1.radio("Audio di dalam video", ["keep", "drop"], horizontal=True, key="mt_v_aud",
                                 format_func={"keep": "Pertahankan (AAC)", "drop": "Hapus"}.get,
                                 help="Live memakai audio dari panel audio, jadi audio video tidak terpakai. "
                                      "Menghapusnya menghemat disk.")
                v_pre = d2.selectbox("Kecepatan encode", ["superfast", "veryfast", "faster", "medium"], index=1,
                                     key="mt_v_pre", help="Lebih lambat = file lebih kecil / kualitas lebih baik.")
                v_thr = d3.selectbox("Thread CPU", [0, 1, 2, 4], index=2, key="mt_v_thr",
                                     format_func=lambda n: "Otomatis" if n == 0 else str(n),
                                     help="Batasi agar live yang sedang berjalan tidak tersendat.")
            tgt_v = mt.VideoTarget(height=v_h, fps=v_fps, vbr_k=v_br, keep_audio=v_aud == "keep", preset=v_pre)
            st.caption(f"Target: H.264 High · yuv420p · {tgt_v.size[0]}x{tgt_v.size[1]} · {v_fps} fps · "
                       f"{tgt_v.bitrate} kbps · keyframe tiap 2 detik → langsung bisa dikirim tanpa re-encode.")

            st.markdown("##### 🧪 Tes video")
            sel_v = _pick_files("v", vpaths, "video")
            t1, t2 = st.columns(2)
            do_gop = t1.checkbox("Ukur jarak keyframe", value=True, key="mt_v_gop",
                                 help="YouTube Live butuh keyframe ≤ 4 detik.")
            do_vdec = t2.selectbox("Tes decode (deteksi file rusak / terpotong)", list(DECMODE), index=1,
                                   format_func=DECMODE.get, key="mt_v_dec")
            if st.button(f"🧪 Tes {len(sel_v)} file video", type="primary", key="mt_v_test", disabled=not sel_v):
                bar = st.progress(0.0, text="Menguji...")
                st.session_state["mt_v_rows"] = mt.run_tests(
                    sel_v, lambda p: mt.video_row(p, tgt_v, do_gop, do_vdec),
                    progress=lambda d, t: bar.progress(d / t, text=f"{d}/{t} file diuji"))
                bar.empty()
            rows_v = st.session_state.get("mt_v_rows") or []
            if rows_v:
                S_v = _report(rows_v, "video", "v")
                st.caption(f"{S_v['ok']} dari {S_v['total']} video siap dikirim apa adanya saat live (CPU ≈ 0). "
                           "Hasil tes dihitung pada target saat tes dijalankan.")

            st.markdown("##### 🔧 Normalisasi video")
            v_out = st.selectbox("Hasil", list(OUTMODE), format_func=OUTMODE.get, key="mt_v_out")
            v_skip = st.checkbox("Lewati file yang sudah seragam", value=True, key="mt_v_skip")
            fix_v = [r["_path"] for r in rows_v if r["Status"] == mt.WARN]

            def _submit_v(paths: list) -> None:
                opts = {"mode": v_out, "skip_ok": v_skip, "threads": v_thr}
                n = mt.submit_normalize(jobs, "video", paths, tgt_v, opts, disk_min, protected_files)
                st.toast(f"{n} job normalisasi video diantrekan")

            b1, b2 = st.columns(2)
            if b1.button(f"🔧 Normalisasi {len(fix_v)} video yang tidak seragam", type="primary",
                         key="mt_v_fix", disabled=not fix_v, width="stretch"):
                _submit_v(fix_v)
            if b2.button(f"🔧 Normalisasi semua {len(sel_v)} video terpilih", key="mt_v_fixall",
                         disabled=not sel_v, width="stretch"):
                _submit_v(sel_v)

    @st.fragment(run_every=3)
    def norm_status():
        rows_ = [r for r in jobs.snapshot() if str(r["Job"]).startswith("Normalisasi")]
        if rows_:
            st.markdown("##### 📋 Antrean normalisasi")
            jobs_table(rows_[:30])
            st.caption("Diproses satu per satu. Job yang berjalan tidak bisa dibatalkan; yang antre bisa "
                       "dibatalkan dari tab Job.")

    norm_status()

# ───────────── LIVE ─────────────
def _mmss(sec: float) -> str:
    sec = int(sec)
    return f"{sec // 3600}:{(sec % 3600) // 60:02d}:{sec % 60:02d}"


@st.cache_data(show_spinner=False)
def _dur(path_str: str, mtime: float) -> float:
    return svc.media_info(path_str)["dur"]


VOL_DEFAULT = {"l_vrand": 1.0}


def _push_vol():
    """Teruskan posisi slider ke stream yang sedang berjalan (tanpa restart)."""
    if "l_vrand" in st.session_state:
        stream.set_volume("r", st.session_state["l_vrand"])


def _reset_vol():
    st.session_state.update(VOL_DEFAULT)
    _push_vol()


with tab_live:
    st.caption("Satu proses live memakai satu encode untuk mengirim siaran ke maksimal lima channel YouTube secara bersamaan. "
               "Pergantian video/lagu tidak memutus sesi. Alur video: opening opsional sekali di awal → video acak berulang. "
               "Audio RANDOM diputar utuh terus-menerus secara acak, independen dari pergantian video.")
    vlib = svc.library_videos()
    alib = svc.list_paths("audio")

    st.markdown("#### 🎞️ Alur video")
    use_opening = st.checkbox("Putar opening sekali saat live dimulai", value=True, key="l_open_on")
    f1, f2 = st.columns(2)
    v_open = f1.multiselect("1️⃣ Opening (acak jika memilih beberapa)", vlib, format_func=svc.label,
                            key="l_open", placeholder="Pilih video opening...", disabled=not use_opening)
    v_rand = f2.multiselect("2️⃣ Video acak (diputar penuh dan berulang)", vlib, format_func=svc.label,
                            key="l_rand", placeholder="Pilih video acak...")
    active_opening = v_open if use_opening else []
    st.caption("Opening boleh kosong atau dimatikan; live langsung memutar video acak. "
               "Setelah opening selesai, video acak terus berjalan. Pilih lebih dari satu opening "
               "agar video awal dipilih acak. Video acak wajib diisi.")

    st.markdown("#### 🎵 Audio random kontinu")
    a_rand = st.multiselect(
        "🎲 Pilih file audio untuk diputar acak terus-menerus",
        alib,
        format_func=svc.label,
        key="l_arand",
        placeholder="Pilih satu atau beberapa file audio...",
    )
    st.caption("Setiap file diputar sampai selesai. Jika memilih beberapa file, urutannya diacak tanpa lagu "
               "yang sama berturut-turut; setelah semua file diputar, urutan diacak kembali. Satu file akan diputar ulang.")

    st.markdown("##### 🎚️ Volume (bisa diubah saat live berjalan, langsung terdengar)")
    for _k, _v in VOL_DEFAULT.items():
        st.session_state.setdefault(_k, _v)
    h1, h2 = st.columns([3, 1.4])
    vol_rand = h1.slider("Volume audio random", 0.0, 2.0, step=0.05, key="l_vrand", on_change=_push_vol)
    h2.button("↩️ Default", key="l_vreset", on_click=_reset_vol, width="stretch",
              help="Kembalikan volume audio random ke 1,00.")

    all_v = list(dict.fromkeys([*active_opening, *v_rand]))
    all_a = list(dict.fromkeys(a_rand))
    if all_v or all_a:
        rt = sum(_dur(str(p), p.stat().st_mtime) for p in v_rand)
        rat = sum(_dur(str(p), p.stat().st_mtime) for p in a_rand)
        opening_label = f"Opening ({len(active_opening)}) → " if active_opening else ""
        st.info(f"Alur: {opening_label}Video acak ({len(v_rand)} file · "
                f"{_mmss(rt)} per putaran)   |   Audio random: {len(a_rand)} file · {_mmss(rat)}")

    live_oauth_cfg = _secret_section("youtube_oauth")
    live_oauth_cfg = {
        "client_id": str(live_oauth_cfg.get("client_id") or ""),
        "client_secret": str(live_oauth_cfg.get("client_secret") or ""),
        "redirect_uri": str(live_oauth_cfg.get("redirect_uri") or "https://redirect1x.streamlit.app/"),
        "state_secret": str(live_oauth_cfg.get("state_secret") or live_oauth_cfg.get("client_secret") or ""),
    }
    ollama_live_cfg = _secret_section("ollama")
    ollama_live_model_default = str(ollama_live_cfg.get("live_model") or "gemma4:31b")
    live_ollama_key_default = str(ollama_live_cfg.get("api_key") or st.session_state.get("vs_ollama_key") or "")
    st.session_state.setdefault("l_live_ollama_key", live_ollama_key_default)
    if not st.session_state["l_live_ollama_key"] and live_ollama_key_default:
        st.session_state["l_live_ollama_key"] = live_ollama_key_default
    st.session_state.setdefault("l_live_ollama_model", ollama_live_model_default)
    st.session_state.setdefault("l_live_broadcast_title", "Live 24 Jam")
    st.session_state.setdefault("l_live_broadcast_description", "")
    with st.expander("✨ Buat judul + hashtag dan deskripsi otomatis (Ollama Cloud)"):
        st.caption("Analisis memakai PCM 16 kHz, fitur spektral, estimasi tempo, gambar spektrogram, dan riset web; pilih hingga tiga audio live. Ini heuristik konten, bukan pengenal lagu/artis dan tidak menjamin penonton/viral.")
        ollama_live_key = st.text_input("Ollama Cloud API key", type="password", key="l_live_ollama_key")
        ollama_live_model = st.text_input(
            "Model Ollama Cloud (harus mendukung vision)", key="l_live_ollama_model",
            help="Default gemma4:31b. Bisa diatur melalui [ollama] live_model di Secrets.",
        )
        if a_rand:
            prior_audio_selection = st.session_state.get("l_live_metadata_audio", list(a_rand[:3]))
            if not isinstance(prior_audio_selection, list):
                prior_audio_selection = [prior_audio_selection] if prior_audio_selection in a_rand else list(a_rand[:3])
            st.session_state["l_live_metadata_audio"] = [path for path in prior_audio_selection if path in a_rand][:3]
            analysis_audios = st.multiselect(
                "Audio live untuk dianalisis (maksimal 3 track)", a_rand,
                format_func=svc.label, max_selections=3, key="l_live_metadata_audio",
                help="Pilih track musik yang mewakili playlist Live; sampel PCM 20 detik per track dianalisis.",
            )
        else:
            analysis_audios = []
            st.info("Pilih audio di bagian ‘Audio random kontinu’ untuk dianalisis.")
        live_metadata_context = st.text_input(
            "Topik / nama program untuk riset web (opsional)", key="l_live_metadata_context",
            placeholder="Contoh: musik relaksasi, lagu rohani, suara alam",
        )
        if st.button("🎧 Analisis PCM + spektrogram + riset & buat metadata", key="l_live_metadata_generate",
                     disabled=not (ollama_live_key.strip() and analysis_audios and ollama_live_model.strip()), width="stretch"):
            try:
                analysis_dir = svc.TMP / f"live-audio-analysis-{secrets.token_hex(6)}"
                with st.spinner("Mengekstrak PCM, menghitung fitur audio, membuat spektrogram, dan meriset topik dengan Ollama Cloud..."):
                    evidence = audioanalysis.analyze_audio(analysis_audios, analysis_dir)
                    research_token = ""
                    selected_research_accounts = st.session_state.get("l_live_oauth_account_ids", [])
                    if isinstance(selected_research_accounts, list) and selected_research_accounts:
                        try:
                            research_token, _ = _youtube_account_access_token(live_oauth_cfg, selected_research_accounts[0])
                        except Exception:  # noqa: BLE001
                            research_token = ""
                    spectrograms = [track["spectrogram"] for track in evidence.get("tracks", [])]
                    metadata = ollama_cloud.generate_live_metadata(
                        ollama_live_key, audio_name=", ".join(Path(path).name for path in analysis_audios),
                        context=live_metadata_context, model=ollama_live_model.strip(),
                        youtube_access_token=research_token, audio_analysis=evidence,
                        spectrogram_paths=spectrograms,
                    )
                st.session_state["l_live_audio_analysis"] = evidence
                st.session_state["l_live_audio_metadata_result"] = metadata
                st.session_state["l_live_broadcast_title"] = metadata["title"]
                st.session_state["l_live_broadcast_description"] = metadata["description"]
                st.success("Metadata dibuat dari fitur PCM/spektrogram dan riset. Periksa lalu edit sebelum menyiapkan broadcast.")
            except Exception as exc:  # noqa: BLE001
                st.error(f"Gagal membuat metadata Live dengan Ollama Cloud: {exc}")
        evidence = st.session_state.get("l_live_audio_analysis")
        if isinstance(evidence, dict) and evidence.get("tracks"):
            with st.container(border=True):
                st.caption("Fitur audio hasil analisis lokal (heuristik, bukan pengenal lagu/artis):")
                display_tracks = [{k: v for k, v in item.items() if k != "spectrogram"}
                                  for item in evidence["tracks"] if isinstance(item, dict)]
                if display_tracks:
                    st.dataframe(pd.DataFrame(display_tracks), hide_index=True, width="stretch")
                image_cols = st.columns(min(3, len(evidence["tracks"])))
                for index, track in enumerate(evidence["tracks"][:3]):
                    image_path = Path(track.get("spectrogram", ""))
                    if image_path.is_file():
                        image_cols[index].image(str(image_path), caption=f"Spektrogram · {track.get('filename', '')}", width="stretch")
            metadata_result = st.session_state.get("l_live_audio_metadata_result", {})
            research = metadata_result.get("research", {}) if isinstance(metadata_result, dict) else {}
            if research.get("warning"):
                st.caption(research["warning"])
            for ref in research.get("references", [])[:6]:
                st.markdown(f"- [{html.escape(str(ref.get('title') or ref.get('url') or 'Referensi'))}]({ref.get('url', '')})")
        st.text_input("Judul Live + hashtag", max_chars=100, key="l_live_broadcast_title")
        st.text_area("Deskripsi Live + hashtag", height=110, max_chars=5000,
                     key="l_live_broadcast_description")
        st.caption("Riset web hanya bahan inspirasi dan dapat kosong jika tidak tersedia; jangan anggap sebagai jaminan tren/penonton.")

    live_oauth_accounts = _sync_youtube_accounts()
    with st.expander("🔐 Tambah / hubungkan channel YouTube Live via OAuth", expanded=not bool(live_oauth_accounts)):
        if not (live_oauth_cfg["client_id"] and live_oauth_cfg["client_secret"] and live_oauth_cfg["state_secret"]):
            st.warning("Isi `[youtube_oauth] client_id`, `client_secret`, dan `redirect_uri` di Streamlit Secrets terlebih dahulu.")
        else:
            state_secret = live_oauth_cfg["state_secret"]
            oauth_state = st.session_state.get("yt_oauth_state")
            if not oauth_state or not ytauth.verify_state(oauth_state, state_secret):
                oauth_state = ytauth.make_state(state_secret)
                st.session_state["yt_oauth_state"] = oauth_state
            auth_url = ytauth.authorization_url(live_oauth_cfg["client_id"], live_oauth_cfg["redirect_uri"], oauth_state)
            st.link_button("🔐 Klik untuk otorisasi / tambah channel YouTube", auth_url, type="primary")
            st.caption("Akses ini dipakai untuk membuat broadcast dan mengambil stream key Live otomatis. Channel yang dulu hanya dihubungkan untuk Shorts perlu menyetujui izin Live ini sekali lagi.")
            oc1, oc2 = st.columns([2.5, 1])
            live_redirect_url = oc1.text_input("URL redirect Google setelah otorisasi", type="password", key="l_yt_redirect_url", placeholder="https://redirect1x.streamlit.app/?state=...&code=...")
            if oc2.button("🔄 Simpan / hubungkan", key="l_yt_oauth_exchange", width="stretch"):
                try:
                    code = ytauth.parse_redirect_url(live_redirect_url, st.session_state.get("yt_oauth_state", ""), state_secret)
                    token = ytauth.exchange_code(live_oauth_cfg["client_id"], live_oauth_cfg["client_secret"], live_oauth_cfg["redirect_uri"], code)
                    channel = ytauth.get_my_channel(token["access_token"])
                    ytauth.upsert_saved_account(token, channel)
                    st.session_state.pop("yt_oauth_state", None)
                    st.success(f"Channel terhubung: {channel['title']}")
                    st.rerun()
                except Exception as exc:  # noqa: BLE001
                    st.error(f"Gagal menghubungkan channel Live: {exc}")
            if live_oauth_accounts:
                live_account_map = {a["id"]: f"📺 {a.get('channel', {}).get('title', 'Channel YouTube')}" for a in live_oauth_accounts}
                live_account_ids = st.multiselect(
                    "Pilih akun/channel YouTube Live (maksimal 5 akun)",
                    list(live_account_map), format_func=lambda aid: live_account_map.get(aid, aid),
                    max_selections=5, key="l_live_oauth_account_ids",
                )
                live_broadcast_counts = {}
                if live_account_ids:
                    count_cols = st.columns(min(3, len(live_account_ids)))
                    for index, account_id in enumerate(live_account_ids):
                        count_key = f"l_live_broadcast_count_{account_id}"
                        try:
                            prior_count = int(st.session_state.get(count_key, 1))
                        except (TypeError, ValueError):
                            prior_count = 1
                        st.session_state[count_key] = max(1, min(5, prior_count))
                        with count_cols[index % len(count_cols)]:
                            live_broadcast_counts[account_id] = st.number_input(
                                f"Jumlah Live · {live_account_map.get(account_id, account_id)}",
                                min_value=1, max_value=5, step=1, key=count_key,
                                help="Setiap Live mendapat broadcast dan stream key YouTube yang unik.",
                            )
                total_prepared_lives = sum(live_broadcast_counts.values())
                if total_prepared_lives > 5:
                    st.error(f"Total slot yang diminta {total_prepared_lives}; batas aplikasi adalah 5 Live per sesi. Kurangi jumlah pada salah satu akun.")
                elif live_account_ids:
                    st.caption(f"Akan disiapkan {total_prepared_lives} broadcast unik dari {len(live_account_ids)} akun (maksimal 5 total).")
                live_broadcast_title = str(st.session_state.get("l_live_broadcast_title", "Live 24 Jam"))[:100]
                live_broadcast_description = str(st.session_state.get("l_live_broadcast_description", ""))[:5000]
                live_privacy_map = {"public": "🌍 Publik", "unlisted": "🔗 Tidak Publik", "private": "🔒 Pribadi"}
                live_broadcast_privacy = st.selectbox("Visibilitas broadcast", list(live_privacy_map), format_func=live_privacy_map.get, key="l_live_broadcast_privacy")
                if st.button("⚙️ Siapkan broadcast/slot Live", key="l_live_prepare_oauth",
                             disabled=stream.running or total_prepared_lives > 5, width="stretch"):
                    if not live_account_ids:
                        st.warning("Pilih minimal satu channel YouTube terlebih dahulu.")
                    elif total_prepared_lives > 5:
                        st.error("Maksimal lima broadcast/slot Live dapat disiapkan dalam satu sesi.")
                    else:
                        prepared = []
                        failures = []
                        live_session_profiles = st.session_state.setdefault("l_live_oauth_profiles", {})
                        with st.spinner(f"Menyiapkan {total_prepared_lives} broadcast unik di YouTube..."):
                            for account_id in live_account_ids:
                                channel_label = live_account_map.get(account_id, account_id)
                                try:
                                    live_token, live_channel = _youtube_account_access_token(live_oauth_cfg, account_id)
                                except Exception as exc:  # noqa: BLE001
                                    failures.append(f"{channel_label}: {exc}")
                                    continue
                                channel_title = live_channel.get("title") or channel_label
                                slot_count = int(live_broadcast_counts.get(account_id, 1))
                                for slot_index in range(1, slot_count + 1):
                                    suffix = f" · Live {slot_index}"
                                    session_title = live_broadcast_title
                                    if slot_count > 1:
                                        session_title = f"{live_broadcast_title[:100 - len(suffix)].rstrip()}{suffix}"
                                    live_profile_name = f"OAuth · {channel_title} ({account_id[-5:]})"
                                    if slot_count > 1:
                                        live_profile_name += suffix
                                    try:
                                        live_session = ytauth.create_live_session(
                                            live_token, session_title, live_broadcast_privacy,
                                            description=live_broadcast_description,
                                        )
                                        live_session_profiles[live_profile_name] = {
                                            "title": live_session["title"], "description": live_broadcast_description,
                                            "rtmp_url": live_session["ingestion_address"],
                                            "stream_key": live_session["stream_key"], "broadcast_id": live_session["broadcast_id"],
                                            "stream_id": live_session["stream_id"], "watch_url": live_session["watch_url"],
                                            "privacy": live_session["privacy"], "channel_id": account_id,
                                        }
                                        prepared.append({"profile": live_profile_name, **live_session})
                                    except Exception as exc:  # noqa: BLE001
                                        failures.append(f"{channel_label} · Live {slot_index}: {exc}")
                        st.session_state["l_channel_profiles"] = [item["profile"] for item in prepared]
                        st.session_state["l_live_last_prepared"] = prepared
                        st.session_state["l_live_prepare_errors"] = failures
                        st.rerun()
                prepared_items = st.session_state.get("l_live_last_prepared", [])
                if isinstance(prepared_items, dict):
                    prepared_items = [prepared_items]
                if not isinstance(prepared_items, (list, tuple)):
                    prepared_items = []
                for prepared_item in prepared_items:
                    if not isinstance(prepared_item, dict):
                        continue  # Ignore stale entries from older app/session-state versions.
                    st.success(f"Broadcast siap: {prepared_item.get('title', 'Live')} · {prepared_item.get('privacy', '')} · {prepared_item.get('profile', '')}")
                    if prepared_item.get("watch_url"):
                        st.link_button("🔗 Buka halaman siaran YouTube", prepared_item["watch_url"], key=f"l_live_watch_{prepared_item.get('broadcast_id', 'live')}")
                for failure in st.session_state.get("l_live_prepare_errors", []):
                    st.error(f"Broadcast tidak berhasil disiapkan: {failure}")
                st.caption("Siapkan broadcast baru untuk setiap sesi YouTube. Semua slot serentak memakai feed video/audio yang sama.")

    RTMP_PRESETS = {"RTMP YouTube (standar)": "rtmp://a.rtmp.youtube.com/live2",
                    "RTMPS YouTube (terenkripsi, lebih tahan putus)": "rtmps://a.rtmps.youtube.com:443/live2",
                    "Kustom": ""}
    live_channels_cfg = _secret_section("live_channels")
    default_live_channel = str(live_channels_cfg.get("default") or "")
    live_profiles = {
        str(name): dict(config)
        for name, config in live_channels_cfg.items()
        if name != "default" and isinstance(config, dict)
    }
    for name, config in st.session_state.get("l_live_oauth_profiles", {}).items():
        if isinstance(config, dict):
            live_profiles[str(name)] = dict(config)
    manual_live_target = "Manual / Kustom"
    live_options = [*live_profiles, manual_live_target]
    if "l_channel_profiles" not in st.session_state:
        if default_live_channel in live_profiles:
            initial_live_targets = [default_live_channel]
        elif live_profiles:
            initial_live_targets = [next(iter(live_profiles))]
        else:
            initial_live_targets = [manual_live_target]
        st.session_state["l_channel_profiles"] = initial_live_targets
    current_live_targets = st.session_state.get("l_channel_profiles", [])
    if not isinstance(current_live_targets, list):
        current_live_targets = []
    st.session_state["l_channel_profiles"] = [name for name in current_live_targets if name in live_options][:5]
    if not live_profiles:
        st.caption("Hubungkan beberapa channel via OAuth di atas atau simpan tujuan di Secrets. Mode manual juga tersedia. Maksimal lima tujuan live bersamaan.")
    live_run_mode = st.radio(
        "Mode menjalankan slot Live",
        ["Serentak", "Bergantian (manual)"], horizontal=True, key="l_live_run_mode",
        help="Serentak: satu feed video/audio yang sama dikirim ke semua slot terpilih. Bergantian: pilih satu slot per sesi; hentikan Live aktif lalu pilih slot berikutnya secara manual.",
    )
    selected_live_targets = st.multiselect(
        "📺 Pilih slot/broadcast Live (maksimal 5)", live_options,
        max_selections=5, key="l_channel_profiles",
        help="Slot OAuth dari akun yang sama menggunakan stream key berbeda. Satu feed FFmpeg dipakai bersama semua slot pada mode serentak.",
    )
    rtmp, skey = "", ""
    if manual_live_target in selected_live_targets:
        l0, l1, l2 = st.columns([1.2, 1.2, 1])
        srv = l0.selectbox("Server tujuan manual", list(RTMP_PRESETS), key="l_srv")
        rtmp = l1.text_input("RTMP URL", RTMP_PRESETS[srv] or "rtmp://", key=f"l_rtmp_{srv}",
                             disabled=srv != "Kustom")
        skey = l2.text_input("Stream Key manual", type="password", key="l_key")

    live_outputs = []
    invalid_live_targets = []
    for target_name in selected_live_targets:
        if target_name == manual_live_target:
            profile_url, profile_key = rtmp.strip(), skey.strip()
            display_name = "Tujuan manual"
        else:
            target_profile = live_profiles[target_name]
            profile_url = str(target_profile.get("rtmp_url") or "").strip()
            profile_key = str(target_profile.get("stream_key") or "").strip()
            display_name = str(target_profile.get("title") or target_name)
        if not profile_url or not profile_key:
            if target_name != manual_live_target:
                invalid_live_targets.append(target_name)
            continue
        live_outputs.append({"name": display_name, "rtmp_url": profile_url, "stream_key": profile_key})
    if live_run_mode == "Bergantian (manual)":
        if len(selected_live_targets) == 1:
            st.info("Mode bergantian manual: 1 slot untuk sesi ini. Hentikan siaran, pilih slot berikutnya, lalu mulai lagi.")
        else:
            st.warning(f"Mode bergantian memerlukan tepat satu slot terpilih; saat ini {len(selected_live_targets)} dipilih.")
    elif live_outputs:
        st.info(f"Mode serentak: **{len(live_outputs)} / 5 Live** · feed video/audio yang sama dikirim ke semua slot terpilih.")
        if len(live_outputs) > 1:
            st.caption(f"Perhatikan bandwidth upload: trafik keluar kira-kira {len(live_outputs)}× bitrate satu tujuan. Jika satu tujuan gagal, FFmpeg berusaha melanjutkan tujuan lain; periksa Log ffmpeg untuk status tiap RTMP.")
    if invalid_live_targets:
        st.error("Periksa URL RTMP dan stream key untuk: " + ", ".join(invalid_live_targets))

    with st.expander("⚙️ Pengaturan lanjutan"):
        vmode = st.radio("Mode video", ["auto", "normalize", "copy"], horizontal=True, key="l_vmode",
                         format_func={"auto": "Otomatis (disarankan)",
                                      "normalize": "Selalu samakan",
                                      "copy": "Kirim apa adanya"}.get,
                         help="Otomatis: jika semua video sudah seragam & cocok untuk live, dikirim apa adanya "
                              "(CPU ≈ 0). Jika tidak, tiap video disamakan SEKALI ke H.264 seragam (disimpan di cache), "
                              "lalu stream tetap tanpa re-encode. 'Kirim apa adanya' hanya jika kamu yakin file seragam.")
        n1, n2, n3 = st.columns(3)
        l_h = n1.selectbox("Resolusi (jika disamakan)", [480, 720, 1080], index=1)
        l_fps = n2.selectbox("FPS (jika disamakan)", [24, 25, 30], index=2)
        l_br = n3.selectbox("Bitrate video kbps (jika disamakan)", [0, 1500, 2500, 4000, 6000], index=0,
                            format_func=lambda b: "Auto (sesuai resolusi, disarankan)" if b == 0 else str(b),
                            help="Auto: 480p≈1500 · 720p≈3000 · 1080p≈5000 kbps (×1.4 jika FPS di atas 30). "
                                 "Otomatis diturunkan bertahap jika jaringan upload lambat / sesi sering putus.")
        o1, o2 = st.columns(2)
        l_shuf = o1.checkbox("Acak video pada tiap pergantian", value=True, key="l_shuf",
                             help="Video berikutnya dipilih acak tanpa pengulangan berurutan.")
        l_hours = o2.number_input("Batas durasi (jam, 0 = tanpa batas)", 0.0, 720.0, 0.0, 1.0)
        if st.button("🧹 Hapus cache hasil penyamaan", key="l_clrcache"):
            st.toast(f"{svc.clear_live_cache()} file cache dihapus")

    s1, s2 = st.columns(2)
    if s1.button("▶️ Mulai Live", type="primary",
                 disabled=stream.running or (live_run_mode == "Bergantian (manual)" and len(selected_live_targets) > 1),
                 width="stretch"):
        if not v_rand:
            st.error("Pilih minimal 1 video acak. Opening bersifat opsional.")
        elif not a_rand:
            st.error("Pilih minimal satu file audio random.")
        elif not selected_live_targets:
            st.error("Pilih minimal satu tujuan Live.")
        elif invalid_live_targets or not live_outputs:
            st.error("Lengkapi konfigurasi semua tujuan yang dipilih sebelum memulai.")
        elif live_run_mode == "Bergantian (manual)" and len(live_outputs) != 1:
            st.error("Mode bergantian menjalankan satu slot per sesi. Pilih satu slot, mulai/hentikan, lalu pilih slot berikutnya.")
        elif len(live_outputs) > 5:
            st.error("Maksimal lima tujuan Live dapat dipilih bersamaan.")
        else:
            stream.start({"videos": all_v, "audios": all_a,
                          "v_open": active_opening, "v_rand": v_rand,
                          "a_loop": [], "a_rand": a_rand,
                          "vol_rand": vol_rand,
                          "outputs": live_outputs,
                          "rtmp_url": live_outputs[0]["rtmp_url"],
                          "stream_key": live_outputs[0]["stream_key"],
                          "vmode": vmode, "height": l_h, "fps": l_fps, "vbr_k": l_br,
                          "shuffle": l_shuf, "disk_min_gb": disk_min}, l_hours)
            st.rerun()
    if s2.button("⏹️ Stop Live", disabled=not stream.running, width="stretch"):
        stream.stop()
        st.rerun()

    @st.fragment(run_every=3)
    def live_status():
        icon = {"live": "🔴 LIVE berjalan", "menyiapkan": "🛠️ Menyiapkan file...",
                "menghubungkan": "🔌 Menghubungkan ke YouTube...", "menyambung ulang": "🔁 Menyambung ulang...",
                "selesai": "✅ Selesai", "error": "❌ Gagal"}.get(stream.state, "⚪ Tidak streaming")
        (st.success if stream.state == "live" else st.error if stream.state == "error" else st.warning)(
            icon + (f" — {stream.detail}" if stream.detail else ""))
        a, b, c, d, d2 = st.columns(5)
        a.metric("Uptime", stream.uptime())
        b.metric("Auto-restart", stream.restarts)
        pr = stream.progress or {}
        c.metric("Kecepatan", pr.get("speed", "-") or "-")
        d.metric("Bitrate", (pr.get("bitrate", "-") or "-").strip())
        d2.metric("Kualitas kirim", stream.health if stream.running else "-")
        if stream.running:
            e, f2_, g = st.columns(3)
            e.metric("Video diputar", (stream.now_video or "-")[:28])
            f2_.metric("Audio random", (stream.now_audio or "-")[:28])
            g.metric("Pergantian video", stream.switches)
            if stream.prep_info:
                st.caption("🛠️ " + stream.prep_info)
        for n in stream.notes:
            st.caption("• " + n)
        with st.expander("Log ffmpeg"):
            st.code(stream.log_tail() or "(kosong)", language="text")

    live_status()

# ───────────── MUSIC STUDIO ─────────────
with tab_music:
    st.caption("Susun lirik dari ide dengan Ollama Cloud, lalu buat lagu yang bisa diputar dan diunduh melalui Kie.ai. "
               "Fitur ini mengikuti alur pada file referensi; bagian tilawah dari file tersebut tidak dimasukkan ke aplikasi.")
    kie_cfg = _secret_section("kie")
    ollama_cfg = _secret_section("ollama")
    kie_secret = str(kie_cfg.get("api_key") or "").strip()
    ollama_secret = str(ollama_cfg.get("api_key") or "").strip()
    st.session_state.setdefault("music_kie_api_key", "")
    st.session_state.setdefault("music_ollama_api_key", "")
    key_col, model_col = st.columns([1.3, 1])
    with key_col:
        music_kie_key_input = st.text_input(
            "Kie.ai API key", type="password", key="music_kie_api_key",
            help="Kunci hanya dipakai dari server untuk permintaan Kie.ai dan tidak disimpan ke riwayat lagu.",
        )
        music_kie_key = music_kie_key_input.strip() or kie_secret
        st.caption("Kunci dari Secrets `[kie] api_key` tersedia." if kie_secret and not music_kie_key_input.strip()
                   else "Key sesi tidak persisten; simpan permanen di Secrets server bila diperlukan.")
    with model_col:
        music_ollama_key_input = st.text_input(
            "Ollama Cloud API key (opsional, untuk draf lirik)", type="password", key="music_ollama_api_key",
            help="Bisa juga dibaca dari Secrets `[ollama] api_key`.",
        )
        music_ollama_key = music_ollama_key_input.strip() or ollama_secret
        st.session_state.setdefault("music_ollama_model", str(ollama_cfg.get("model") or "gpt-oss:20b"))
        music_ollama_model = st.text_input("Model draf lagu", key="music_ollama_model")

    with st.expander("✨ Buat draf judul, lirik, dan style dari ide lagu", expanded=False):
        with st.form("music_lyric_draft_form"):
            music_idea = st.text_area("Ceritakan ide atau pesan lagu", max_chars=1200, height=100,
                                      key="music_idea", placeholder="Contoh: Perantau rindu rumah saat hujan pertama turun di kota...")
            idea_cols = st.columns(2)
            idea_language = idea_cols[0].selectbox("Bahasa lirik", ["Bahasa Indonesia", "English", "Jawa", "Sunda", "Melayu", "Lainnya"], key="music_idea_language")
            idea_genre = idea_cols[1].text_input("Genre / arah musik", value="Pop Indonesia", key="music_idea_genre")
            idea_cols2 = st.columns(2)
            idea_mood = idea_cols2[0].text_input("Mood", value="Hangat dan penuh harap", key="music_idea_mood")
            idea_vocal = idea_cols2[1].selectbox("Karakter vokal", ["Otomatis", "Pria", "Wanita", "Duet"], key="music_idea_vocal")
            idea_instrumental = st.checkbox("Draf untuk instrumental (tanpa lirik)", key="music_idea_instrumental")
            draft_clicked = st.form_submit_button("✨ Buat draf dengan Ollama Cloud", disabled=not music_ollama_key)
        if not music_ollama_key:
            st.caption("Isi API key Ollama Cloud di atas atau Secrets `[ollama] api_key` untuk membuat draf. Isian lagu manual tetap bisa digunakan.")
        if draft_clicked:
            try:
                with st.spinner("Ollama Cloud sedang menyusun draf lagu..."):
                    song_draft = ollama_cloud.generate_song_draft(
                        music_ollama_key, music_ollama_model, music_idea, idea_language,
                        idea_genre, idea_mood,
                        {"Otomatis": "mixed", "Pria": "male", "Wanita": "female", "Duet": "duet"}.get(idea_vocal, "mixed"),
                        idea_instrumental,
                    )
                st.session_state["music_title"] = song_draft["title"]
                st.session_state["music_lyrics"] = song_draft["lyrics"]
                st.session_state["music_style"] = song_draft["style"]
                st.session_state["music_instrumental"] = idea_instrumental
                st.success("Draf siap dan bisa diedit sebelum dikirim ke Kie.ai.")
            except Exception as exc:  # noqa: BLE001
                st.error(f"Draf lagu gagal dibuat: {exc}")

    music_jobs_state = st.session_state.setdefault("music_studio_jobs", [])
    active_music_count = sum(job.get("status") in {"waiting", "generating", "paused"} for job in music_jobs_state)
    st.markdown("#### 🎼 Detail lagu")
    if active_music_count >= 5:
        st.warning("Ada lima tugas musik yang masih aktif/menunggu pemeriksaan. Selesaikan atau periksa dulu sebelum mengirim tugas baru.")
    with st.form("music_generate_form"):
        type_col, model_col2, vocal_col = st.columns(3)
        instrumental = type_col.checkbox("Instrumental (tanpa lirik/vokal)", key="music_instrumental")
        music_model = model_col2.selectbox("Model generator", ["V6", "V6_MINI", "V6_WILD"],
                                           format_func={"V6": "V6 · standar", "V6_MINI": "V6 Mini", "V6_WILD": "V6 Wild"}.get,
                                           key="music_model")
        vocal_gender_label = vocal_col.selectbox("Karakter vokal", ["Otomatis", "Pria", "Wanita"], disabled=instrumental,
                                                 key="music_vocal_gender")
        music_title = st.text_input("Judul lagu (maks. 80 karakter)", max_chars=80, key="music_title")
        music_lyrics = st.text_area("Lirik (maks. 5.000 karakter)", max_chars=5000, height=220,
                                    disabled=instrumental, key="music_lyrics",
                                    placeholder="Gunakan struktur seperti [Verse], [Pre-Chorus], [Chorus] dan [Bridge].")
        music_style = st.text_area("Style musik (maks. 1.000 karakter)", max_chars=1000, height=100,
                                   key="music_style", placeholder="Contoh: Indonesian pop ballad, warm piano, acoustic guitar, emotional, 80 BPM")
        with st.expander("Pengaturan tambahan"):
            music_negative = st.text_input("Hindari gaya atau suara (opsional)", max_chars=1000, key="music_negative")
            advanced_cols = st.columns(4)
            use_duration = advanced_cols[0].checkbox("Atur durasi", key="music_use_duration")
            music_duration = advanced_cols[0].number_input("Durasi, detik", min_value=10, max_value=360, value=180,
                                                           step=1, disabled=not use_duration, key="music_duration")
            use_variety = advanced_cols[1].checkbox("Atur variasi", key="music_use_variety")
            music_variety = advanced_cols[1].number_input("Variasi (0–4)", min_value=0, max_value=4, value=2,
                                                          step=1, disabled=not use_variety, key="music_variety")
            use_style_weight = advanced_cols[2].checkbox("Atur kekuatan style", key="music_use_style_weight")
            music_style_weight = advanced_cols[2].slider("Kekuatan style", 0.0, 1.0, 0.65, 0.01,
                                                          disabled=not use_style_weight, key="music_style_weight")
            use_weirdness = advanced_cols[3].checkbox("Atur eksperimen", key="music_use_weirdness")
            music_weirdness = advanced_cols[3].slider("Eksperimen", 0.0, 1.0, 0.65, 0.01,
                                                       disabled=not use_weirdness, key="music_weirdness")
        st.warning("Setiap permintaan memakai kredit Kie.ai sesuai tarif akun dan tugas yang diterima tetap berjalan meski tab ditutup. Periksa title/lirik/style sebelum generate.")
        music_submit = st.form_submit_button("🎵 Generate Musik", type="primary",
                                             disabled=not music_kie_key or st.session_state.get("music_create_uncertain", False) or active_music_count >= 5)

    if st.session_state.get("music_create_uncertain"):
        st.error("Status permintaan terakhir belum pasti. Periksa Task ID/akun Kie.ai sebelum mencoba lagi agar tidak terkena biaya ganda.")
        if st.button("Saya sudah memeriksa Kie.ai; izinkan permintaan baru", key="music_ack_uncertain"):
            st.session_state["music_create_uncertain"] = False
            st.rerun()
    elif not music_kie_key:
        st.info("Masukkan Kie.ai API key atau atur `[kie] api_key` di Secrets untuk mengaktifkan generator.")

    if music_submit:
        request_input = {"custom_mode": True, "instrumental": instrumental, "model": music_model,
                         "title": music_title, "style": music_style}
        if instrumental:
            request_input["prompt"] = music_style
        else:
            request_input["lyrics"] = music_lyrics
            if len(music_lyrics) <= 3000:
                request_input["prompt"] = music_lyrics
            if vocal_gender_label in {"Pria", "Wanita"}:
                request_input["vocal_gender"] = "m" if vocal_gender_label == "Pria" else "f"
        if music_negative.strip():
            request_input["negative_tags"] = music_negative.strip()
        if use_duration:
            request_input["duration"] = int(music_duration)
        if use_variety:
            request_input["variety"] = int(music_variety)
        if use_style_weight:
            request_input["style_weight"] = float(music_style_weight)
        if use_weirdness:
            request_input["weirdness_constraint"] = float(music_weirdness)
        try:
            request_body = musicapi.validate_request({"model": "ai-music-api/generate", "input": request_input})
            with st.spinner("Mengirim tugas ke Kie.ai..."):
                task_id, _provider_response = musicapi.create_task(music_kie_key, request_body)
            jobs_list = st.session_state.setdefault("music_studio_jobs", [])
            jobs_list[:] = [job for job in jobs_list if job.get("id") != task_id]
            jobs_list.insert(0, {"id": task_id, "title": music_title.strip(), "model": music_model,
                                 "instrumental": instrumental, "created_at": time.time(), "status": "waiting",
                                 "message": "Tugas diterima Kie.ai. Menunggu proses musik.", "progress": None,
                                 "tracks": [], "next_poll_at": 0, "poll_failures": 0})
            if len(jobs_list) > 40:
                active_jobs = [job for job in jobs_list if job.get("status") not in {"success", "failed"}]
                finished_jobs = [job for job in jobs_list if job.get("status") in {"success", "failed"}]
                jobs_list[:] = active_jobs + finished_jobs[:max(0, 40 - len(active_jobs))]
            st.success(f"Tugas musik dibuat · Task ID `{task_id}`. Status akan diperiksa otomatis.")
        except musicapi.KieMusicError as exc:
            if exc.uncertain:
                st.session_state["music_create_uncertain"] = True
            st.error(f"Kie.ai: {exc}")
        except ValueError as exc:
            st.error(f"Periksa isian musik: {exc}")

    with st.expander("Pulihkan / cek tugas dengan Task ID", expanded=False):
        with st.form("music_recover_form"):
            recovered_task_id = st.text_input("Task ID dari Kie.ai", key="music_recover_id")
            recover_clicked = st.form_submit_button("Cek Task ID")
        if recover_clicked:
            if not musicapi.TASK_ID_RE.fullmatch(recovered_task_id.strip()):
                st.error("Task ID tidak valid.")
            elif not music_kie_key:
                st.error("Masukkan API key Kie.ai yang sama dengan key pemilik tugas.")
            else:
                jobs_list = st.session_state.setdefault("music_studio_jobs", [])
                if not any(job.get("id") == recovered_task_id.strip() for job in jobs_list):
                    jobs_list.insert(0, {"id": recovered_task_id.strip(), "title": "Tugas " + recovered_task_id.strip()[:16],
                                         "model": "Kie.ai", "instrumental": None, "created_at": time.time(),
                                         "status": "waiting", "message": "Memulihkan status tugas.", "progress": None,
                                         "tracks": [], "next_poll_at": 0, "poll_failures": 0})
                st.success("Task ID ditambahkan ke pemantauan sesi ini.")

    def _merge_music_tracks(existing: list[dict], incoming: list[dict]) -> list[dict]:
        merged = list(existing)
        for track in incoming:
            match = next((item for item in merged if (track.get("id") and item.get("id") == track.get("id"))
                          or (track.get("audio") and track.get("audio") in {item.get("audio"), item.get("stream")})
                          or (track.get("stream") and track.get("stream") in {item.get("audio"), item.get("stream")})), None)
            if match is None:
                merged.append(dict(track))
            else:
                for field in ("id", "audio", "stream", "image", "title", "duration"):
                    if not match.get(field) and track.get(field):
                        match[field] = track[field]
        return merged[:20]

    def _poll_music_job(job: dict, api_key: str, force: bool = False) -> None:
        now = time.time()
        if not force and now < float(job.get("next_poll_at", 0)):
            return
        if not api_key:
            job.update(status="paused", message="Masukkan kembali API key Kie.ai yang sama untuk memeriksa tugas.")
            return
        job["next_poll_at"] = now + 12
        try:
            payload = musicapi.get_task(api_key, job["id"])
            result = musicapi.normalize_task(payload, api_key)
            job["tracks"] = _merge_music_tracks(job.get("tracks", []), result["tracks"])
            job["status"] = result["status"]
            job["message"] = result["message"]
            job["progress"] = result["progress"]
            job["poll_failures"] = 0
            if job["status"] == "success" and not job["tracks"]:
                job["status"] = "waiting"
                job["message"] = "Kie.ai menyatakan selesai, tautan audio belum tersedia. Status akan dicek lagi."
        except musicapi.KieMusicError as exc:
            failures = int(job.get("poll_failures", 0)) + 1
            job["poll_failures"] = failures
            if exc.status in {401, 402, 403, 404, 410, 422, 455} or failures >= 5:
                job.update(status="paused", message=str(exc))
            else:
                job.update(status="waiting", message=f"Pengecekan terganggu; akan dicoba lagi. {exc}")
                job["next_poll_at"] = now + min(60, 5 * (2 ** (failures - 1)))
        if now - float(job.get("created_at", now)) > 30 * 60 and job.get("status") in {"waiting", "generating"}:
            job.update(status="paused", message="Pemantauan otomatis dijeda setelah 30 menit. Tekan Cek status untuk melanjutkan.")

    @st.fragment(run_every=10)
    def music_jobs_panel():
        jobs_list = st.session_state.setdefault("music_studio_jobs", [])
        if not jobs_list:
            st.info("Belum ada lagu dibuat di sesi ini. Isian musik dapat dipakai manual atau dibuat dari draf Ollama Cloud.")
            return
        st.caption("Task ID dan tautan hasil hanya tersimpan di sesi browser Streamlit ini. Unduh hasil yang disukai; URL provider dapat kedaluwarsa.")
        for index, job in enumerate(list(jobs_list)):
            if job.get("status") in {"waiting", "generating"}:
                _poll_music_job(job, music_kie_key)
            with st.container(border=True):
                head = st.columns([3, 1])
                head[0].markdown(f"**{job.get('title') or 'Musik'}** · `{job.get('model') or 'Kie.ai'}`")
                head[1].markdown(f"**{job.get('status', 'waiting').upper()}**")
                st.caption(f"Task ID: `{job.get('id')}` · {time.strftime('%Y-%m-%d %H:%M', time.localtime(job.get('created_at', time.time())))}")
                st.write(job.get("message", ""))
                if job.get("progress") is not None:
                    st.progress(min(100, max(0, int(job["progress"]))) / 100)
                if st.button("🔄 Cek status", key=f"music_check_{index}_{job.get('id')}"):
                    _poll_music_job(job, music_kie_key, force=True)
                if job.get("tracks"):
                    for track_index, track in enumerate(job["tracks"]):
                        with st.expander(f"▶️ {track.get('title') or f'Variasi {track_index + 1}'}"):
                            audio_url = track.get("audio") or track.get("stream")
                            if audio_url:
                                st.audio(audio_url)
                                st.link_button("⬇️ Buka / unduh audio", audio_url)
                            if track.get("image"):
                                st.image(track["image"], width=160)
                            if track.get("duration"):
                                st.caption(f"Durasi: {track['duration']:.0f} detik")

    music_jobs_panel()

# ───────────── JOB ─────────────
with tab_jobs:
    b1, b2, b3 = st.columns(3)
    if b1.button("🔁 Coba ulang yang gagal", key="pj_retry"):
        st.toast(f"{jobs.retry_failed() + dl_jobs.retry_failed()} job diulang")
    if b2.button("🚫 Batalkan yang antre", key="pj_cancel"):
        st.toast(f"{jobs.cancel_queued() + dl_jobs.cancel_queued()} job dibatalkan")
    if b3.button("🧽 Bersihkan riwayat", key="pj_clear"):
        jobs.clear_finished()
        dl_jobs.clear_finished()
        st.rerun()

    @st.fragment(run_every=3)
    def jobs_panel():
        rm = jobs.snapshot()
        if rm:                                   # panel Render/Merge dihapus; job lama tetap terlihat di sini
            st.markdown("**Normalisasi, Render & Merge**")
            jobs_table(rm)
        st.markdown("**Impor / Unduhan**")
        jobs_table(dl_jobs.snapshot())

    jobs_panel()
    st.caption("Job yang sedang berjalan tidak bisa dibatalkan. Hasil unduhan muncul di Library.")
