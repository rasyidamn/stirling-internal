# pdftools: Layanan PDF Internal dengan Login AD dan Audit Log

Layanan PDF (merge, split, compress, convert, dll.) untuk staf kantor yang **berjalan di jaringan lokal**. Memakai **Stirling-PDF resmi (tanpa modifikasi)**, login memakai **akun Active Directory**, dan **setiap pemrosesan file dicatat**: siapa, kapan, file apa (hash SHA-256), dan hasilnya. Isi dokumen tidak pernah disimpan.

## 1. Arsitektur

```
Browser staf
   │ https://pdftools.local
   ▼
Caddy  (HTTPS, CA internal)            port 80/443 - satu-satunya yang terbuka ke LAN
   │── forward_auth ──► Authelia ──► Active Directory (LDAP)
   │   (Remote-User, Remote-Groups, Remote-Email, Remote-Name; header kiriman browser dibuang)
   ▼
audit-proxy (Python/FastAPI)  ──► logs/audit.jsonl  (+ salinan syslog opsional)
   ▼   network "backend" internal: tanpa akses LAN/Internet
Stirling-PDF 3.3.0 (image resmi, login bawaannya dimatikan)
```

| Komponen | Image (dipin) | Fungsi |
|---|---|---|
| Stirling-PDF | `stirlingtools/stirling-pdf:3.3.0` | Mesin dan UI PDF |
| audit-proxy | dibangun dari `audit-proxy/` (`python:3.14.10-slim-trixie`, `astral/uv:0.13.8`) | Meneruskan request dan menulis audit |
| Authelia | `authelia/authelia:4.39.28` | Halaman login, cek AD dan grup |
| Caddy | `caddy:2.11.9-alpine` | HTTPS, reverse proxy |

Versi diperiksa pada 13 Nov 2026. Detail perilaku Stirling: `docs/investigasi.md`.

## 2. Mode uji (tanpa AD), ≤5 langkah

Butuh Docker + Docker Compose v2.

```bash
# 1. buat .env + rahasia acak (AUTH_BACKEND=file = mode uji)
scripts/generate-secrets.sh
# 2. folder log milik uid di .env (AUDIT_UID, default 1000)
mkdir -p logs && sudo chown 1000:1000 logs
# 3. jalankan
docker compose up -d --wait
```

4. Di PC yang dipakai menguji, arahkan nama ke server. Contoh di Windows (Notepad sebagai Administrator) `C:\Windows\System32\drivers\etc\hosts`: `127.0.0.1 pdftools.local` (atau IP server).
5. Buka **https://pdftools.local** dan login dengan akun dummy:

| User | Password | Hasil |
|---|---|---|
| `staf.uji` | `Uji-PDF-staf-2026` | masuk (grup `GG-PDF-Users`) |
| `orang.luar` | `Uji-PDF-luar-2026` | ditolak 403 |

Akun dummy hanya untuk mode uji. Peringatan sertifikat: lihat `docs/operasional.md` bagian 2.

## 3. Menghubungkan ke Active Directory

**Data yang diminta ke admin AD:**

| Data | Contoh | Variabel `.env` |
|---|---|---|
| Nama/IP domain controller | `AD-SVR.ad-lab.local` | `AD_LDAP_URL`, `AD_TLS_SERVER_NAME` |
| Apakah LDAPS (636) aktif + CA penerbit sertifikat DC | ya/tidak | menentukan `ldaps://` atau `ldap://` |
| Base DN domain | `DC=ad-lab,DC=local` | `AD_BASE_DN` |
| Akun service read-only (user biasa, tanpa hak admin, password tidak kedaluwarsa) | `CN=svc-pdftools,CN=Users,DC=ad-lab,DC=local` | `AD_BIND_USER`, `AD_BIND_PASSWORD` |
| Grup AD yang boleh memakai layanan | `GRP-PDF-Tools` | `PDF_GROUP` |
| Ambang *account lockout* AD | mis. 10 kali | Authelia memblokir setelah 5 kali agar akun AD tidak ikut terkunci |
| Firewall: server PDF → DC port 636 (atau 389) | dibuka | |

Langkah:

1. Untuk LDAPS: simpan root CA yang menerbitkan sertifikat DC (format PEM) di `authelia/certs/`, mis. `authelia/certs/ad-root-ca.crt`.
2. Isi nilai di atas pada `.env`, lalu ubah `AUTH_BACKEND=ldap`. Password akun service tidak boleh mengandung `'`.
3. `docker compose up -d` (Authelia dibuat ulang).
4. Login dengan akun AD anggota grup. Cek `docker compose logs authelia` bila gagal.

Langkah lengkap (objek AD, DNS, LDAPS, GPO) dan skenario uji: **`docs/uji-ad.md`**.

Logout: buka `https://<PDF_HOSTNAME>/logout`. UI Stirling tidak punya tombol logout karena login bawaannya dimatikan.

Catatan keamanan: tanpa LDAPS, password staf dikirim ke DC **tanpa enkripsi** (ldap://389). Gunakan hanya untuk lab. Untuk produksi, minta admin AD mengaktifkan LDAPS.

User yang di-disable atau dikeluarkan dari grup di AD kehilangan akses paling lama 5 menit kemudian. Untuk grup bersarang, lihat komentar di `authelia/backend-ldap.yml`.

## 4. Membaca dan mencari log audit

Satu baris JSON per kejadian di `logs/audit.jsonl` (hari ini) dan `logs/audit-YYYY-MM-DD.jsonl` (hari sebelumnya). Field penting: `ts`, `user`, `name`, `groups`, `ip`, `operation`, `inputs[]` (nama file, ukuran, `sha256`, halaman), `output` (nama, `sha256`), `result` (`success`/`error`/`rejected`), `duration_ms`, `error`. Untuk request API yang bukan multipart (tanpa file), ada `request_body` (jenis, ukuran, `sha256`; isinya tidak disimpan).

Yang dicatat: semua `POST/PUT/PATCH/DELETE /api/v1/**` (kecuali `automation/meter` milik UI) dan semua request yang ditolak audit-proxy, termasuk upload yang gagal di tengah jalan (`result: error`, status 499/507).

Pasang `jq` (`sudo apt install jq`), lalu:

```bash
cd logs

# Semua aktivitas user budi hari ini (ringkas)
jq -c 'select(.user=="budi") | {ts, operation, file: [.inputs[].filename], result}' audit.jsonl

# Siapa yang pernah memproses file dengan hash X (sebagai input atau output), semua hari
H=ac03e2df556d706b53a8dd1acd36eee2c74100b2d67998d742553d3f579ae281
jq -c --arg h "$H" 'select(any(.inputs[]?; .sha256==$h) or .output.sha256==$h)
       | {ts, user, operation, in: [.inputs[].filename], out: .output.filename}' audit*.jsonl

# Hash sebuah file yang ditemukan (mis. dokumen bocor), lalu cari dengan perintah di atas
sha256sum dokumen.pdf

# Jumlah operasi per user hari ini
jq -r 'select(.result=="success") | .user' audit.jsonl | sort | uniq -c | sort -rn

# Semua yang gagal / ditolak
jq -c 'select(.result!="success") | {ts, user, ip, endpoint, status, error}' audit.jsonl

# Aktivitas pada rentang tanggal (contoh 1-15 November)
jq -c 'select(.ts >= "2026-11-01" and .ts < "2026-11-16")' audit-2026-11-*.jsonl audit.jsonl
```

Karena hash output satu operasi menjadi hash input operasi berikutnya, riwayat sebuah dokumen bisa ditelusuri walaupun namanya berganti.

## 5. Update versi dengan aman

1. Baca catatan rilis versi baru (terutama Stirling-PDF).
2. Ganti tag **satu image** di `docker-compose.yml` (atau `audit-proxy/Dockerfile`).
3. Jalankan test:
   ```bash
   scripts/integration-test.sh            # Stirling + audit-proxy: merge/split/compress/rotate/convert + hash
   docker compose up -d --wait
   cd audit-proxy && uv run pytest -m e2e # jalur lengkap Caddy+Authelia (hanya mode uji)
   ```
4. Uji manual sebentar di browser (login, merge), lalu periksa baris baru di `logs/audit.jsonl`.
5. Jika ada masalah: kembalikan tag lama, lalu `docker compose up -d`.

Untuk update Stirling, ulangi juga pemeriksaan `docs/investigasi.md`: tool baru yang diproses di browser tidak akan teraudit.

## 6. Batasan yang diketahui

**Tidak teraudit** karena diproses sepenuhnya di browser (keputusan 13 Nov 2026):

- Page editor / workbench (menyusun, memutar, menghapus halaman, menggabung lalu export)
- PDF Text Editor / editor PDF
- Sign (tanda tangan gambar), anotasi di viewer
- Remove Annotations, Adjust Contrast, Form Fill

**Dimatikan** di Stirling (`docker-compose.yml`): grup `Automation`, `DeveloperTools`, `DeveloperDocs`, `DocParse`; endpoint `auto-rotate-pdf` (hasil akhirnya dibuat di browser sehingga hash tidak cocok) dan `url-to-pdf`; analytics, cek update, upload dari HP.

**Diblokir di audit-proxy (403, tercatat)**:

- **Allowlist API**: hanya `/api/v1/{general, misc, security, convert, filter, analysis, config, info, notifications, policies, automation/meter, proprietary/ui-data}` yang diteruskan. API lain, termasuk API baru di versi Stirling berikutnya, otomatis ditolak. Atur dengan `ALLOWED_API_PREFIXES` di `.env`.
- **Blocklist** (lapis kedua): API `admin`, `database`, `storage`, `ai`, `integration(s)`, `workflow`, `pipeline`, `docparse`, `user`, `team`, `invite`, `audit`, `usage`, `ownership`, `processing-folders`, serta Swagger/OpenAPI.
- **Job async** (`?async=true` atau field `async`, dan `/api/v1/general/job/**`). UI tidak memakainya, dan hasil job bisa diambil user lain yang tahu ID-nya.
- Path dinormalisasi dulu (`;param`, `//` dibuang), dan path berisi `.`/`..`/`%` ditolak (400). Stirling menerima path persis seperti yang dicek.

**Batas beban**: maksimal `MAX_CONCURRENT_UPLOADS` (default 3) upload diproses bersamaan; sisanya menunggu `UPLOAD_QUEUE_TIMEOUT` detik lalu mendapat "Server sedang sibuk" (503). Jumlah halaman hanya dihitung untuk file ≤ `PAGE_COUNT_MAX_MB` (default 50).

Lain-lain:

- Percobaan login (berhasil/gagal) tercatat di log Authelia, bukan di `audit.jsonl` (lihat `docs/zabbix.md` bagian 6).
- Log di server bisa dihapus oleh admin server. Aktifkan syslog remote untuk salinan yang tidak bisa dihapus dari server ini.
- Session login disimpan di memori: setelah Authelia restart, staf login ulang.

## Struktur repo

```
docker-compose.yml        semua layanan
compose.test.yml          override test (expose audit-proxy ke localhost) - jangan di produksi
.env.example              contoh konfigurasi (tanpa rahasia)
caddy/Caddyfile
authelia/configuration.yml, backend-file.yml, backend-ldap.yml, users_database.yml (uji)
audit-proxy/              kode, Dockerfile, test (pytest)
scripts/                  generate-secrets.sh, integration-test.sh
docs/                     investigasi.md, operasional.md, zabbix.md, uji-ad.md
```

## Pengembangan audit-proxy

```bash
cd audit-proxy
uv sync
uv run pytest                    # unit test (tanpa Docker)
../scripts/integration-test.sh   # integration (Docker)
uv run pytest -m e2e             # full stack mode uji harus sudah jalan
```
