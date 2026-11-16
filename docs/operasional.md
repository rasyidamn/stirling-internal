# Panduan Operasional

Semua perintah dijalankan dari folder proyek (misalnya `/opt/stirling-audit`).

## 1. Perintah harian

```bash
docker compose ps                    # status semua container (harus Up/healthy)
docker compose logs -f audit-proxy   # log teknis audit-proxy
docker compose logs -f authelia      # login berhasil/gagal
docker compose restart caddy         # setelah mengubah Caddyfile
docker compose up -d                 # setelah mengubah .env / docker-compose.yml
curl -sk https://pdftools.local/healthz
```

## 2. Sertifikat HTTPS (CA internal Caddy)

Caddy membuat CA sendiri (`tls internal`). Root CA berlaku 10 tahun, sedangkan sertifikat server diperbarui otomatis. **Root CA harus dipercaya oleh semua PC staf.**

1. Ambil root CA:
   ```bash
   docker compose cp caddy:/data/caddy/pki/authorities/local/root.crt ./caddy-root.crt
   ```
2. Bagikan lewat **Group Policy** (di server AD):
   *Group Policy Management* → buat/edit GPO yang ditautkan ke OU komputer staf →
   *Computer Configuration → Policies → Windows Settings → Security Settings → Public Key Policies → Trusted Root Certification Authorities* → **Import** `caddy-root.crt`.
   Di PC staf jalankan `gpupdate /force` (atau tunggu ±90 menit).
3. **Firefox** memakai store sendiri. Aktifkan `security.enterprise_roots.enabled = true` lewat GPO/policies.json agar Firefox ikut mempercayai store Windows.
4. **Kaspersky**: bila tetap muncul halaman "untrusted certificate", minta admin Kaspersky Security Center menambahkan `pdftools.local` ke pengecualian *Encrypted connections scan*, atau memastikan root CA ada di store **Local Machine**.

**Penting:** volume `caddy-data` berisi **private key CA**. Kalau hilang, Caddy membuat CA baru dan GPO harus diulang. Volume ini termasuk dalam backup (bagian 5).

Bila nanti kantor punya CA sendiri (misalnya AD Certificate Services), terbitkan sertifikat untuk `pdftools.local`, taruh di `caddy/certs/`, mount ke container, lalu ganti `tls internal` di Caddyfile menjadi `tls /certs/pdftools.crt /certs/pdftools.key`.

## 3. DNS

Buat record **A** `pdftools` di zona DNS AD (`ad-lab.local` / zona kantor) atau zona `pdftools.local` yang mengarah ke IP server PDF.

Catatan `.local`: akhiran `.local` juga dipakai mDNS/Bonjour. PC Windows yang memakai DNS AD tidak bermasalah. Mac/Linux/HP bisa gagal me-resolve. Bila itu terjadi, pertimbangkan nama di bawah domain AD, misalnya `pdftools.ad-lab.local`, lalu ubah `PDF_HOSTNAME` di `.env`.

## 4. Log audit

- Lokasi: `logs/audit.jsonl` (hari ini) dan `logs/audit-YYYY-MM-DD.jsonl` (hari-hari sebelumnya).
- **Rotasi** dilakukan oleh audit-proxy sendiri saat event pertama setelah tengah malam WIB. Tidak perlu logrotate.
- **Retensi**: file lebih tua dari `LOG_RETENTION_DAYS` (default 365) dihapus otomatis.
- File log hanya bisa ditambah oleh audit-proxy. Agar tidak bisa dihapus diam-diam dari server ini, aktifkan **syslog remote** (`SYSLOG_HOST` di `.env`) ke server log lain, dan/atau backup harian ke lokasi lain.
- Alternatif bila ingin logrotate di host: set `LOG_RETENTION_DAYS=0` (audit-proxy tetap rotasi harian tetapi tidak menghapus), lalu buat `/etc/logrotate.d/pdf-audit` yang mengompres dan menghapus `audit-*.jsonl` lama:
  ```
  /opt/stirling-audit/logs/audit-*.jsonl {
      daily
      rotate 365
      compress
      delaycompress
      missingok
      nocreate
  }
  ```
  Jangan sertakan `audit.jsonl` (file aktif) di logrotate.

## 5. Backup

Yang perlu di-backup:

| Apa | Lokasi | Kenapa |
|---|---|---|
| Log audit | `logs/` | bukti audit |
| Konfigurasi | `.env`, `caddy/`, `authelia/`, `docker-compose.yml` | rahasia + konfigurasi |
| CA & sertifikat | volume `stirling-audit_caddy-data` | private key CA internal |
| Data Authelia | volume `stirling-audit_authelia-data` | SQLite (status ban, dll.), opsional |

Contoh backup harian (cron):

```bash
#!/usr/bin/env bash
set -e
cd /opt/stirling-audit
D=/backup/pdftools/$(date +%F); mkdir -p "$D"
tar czf "$D/config-logs.tgz" .env caddy authelia docker-compose.yml logs
docker run --rm -v stirling-audit_caddy-data:/v:ro -v "$D":/b alpine tar czf /b/caddy-data.tgz -C /v .
docker run --rm -v stirling-audit_authelia-data:/v:ro -v "$D":/b alpine tar czf /b/authelia-data.tgz -C /v .
find /backup/pdftools -maxdepth 1 -type d -mtime +30 -exec rm -rf {} +
```

File backup berisi `.env` (password akun service AD). Simpan di lokasi yang aksesnya terbatas.

## 6. Update versi

Lihat README bagian 5. Ringkasnya: ganti tag image (Stirling/Caddy/Authelia) **satu per satu**, jalankan `scripts/integration-test.sh`, lalu `docker compose up -d`, dan uji login + merge lewat browser. Jika gagal, kembalikan tag lama lalu `docker compose up -d`.

Setelah update Stirling, ulangi pengecekan singkat Fase 0: apakah ada tool baru yang diproses di browser atau endpoint admin baru (lihat `docs/investigasi.md`).

## 7. Troubleshooting

| Gejala | Cek | Solusi |
|---|---|---|
| Browser: "untrusted certificate" | Root CA terpasang? | Bagian 2 |
| Halaman login tidak muncul / redirect loop | `docker compose logs authelia` | `PDF_HOSTNAME` di `.env` harus sama persis dengan alamat yang dibuka staf |
| Login benar tetapi "403 Forbidden" | User anggota `PDF_GROUP`? | Tambahkan ke grup AD, tunggu ≤5 menit (refresh_interval) atau logout/login |
| Login AD selalu gagal | `docker compose logs authelia` (cari `LDAP`) | Cek `AD_LDAP_URL`, DN akun service, password, firewall port 636/389 dari server ke DC |
| "File terlalu besar" | `MAX_UPLOAD_MB` | Naikkan di `.env`, lalu `docker compose up -d` (Stirling ikut menyesuaikan) |
| Proses lama lalu error 504 | `STIRLING_TIMEOUT` | Naikkan (detik). Dokumen sangat besar butuh waktu. Naikkan juga `CADDY_UPSTREAM_TIMEOUT` (≥ `STIRLING_TIMEOUT` + 30 detik) |
| "Server sedang sibuk" (503) | jumlah baris `server busy` di log audit | Banyak upload bersamaan. Naikkan `MAX_CONCURRENT_UPLOADS` **bersama** `TMPFS_SIZE` (lihat komentar di `.env.example`) |
| "Ruang sementara server penuh" (507) | `TMPFS_SIZE`, RAM server | Naikkan `TMPFS_SIZE` atau turunkan `MAX_CONCURRENT_UPLOADS` |
| Satu tool di UI gagal dengan 403 "endpoint disabled by administrator" | `jq 'select(.status==403)' logs/audit.jsonl` → kolom `endpoint` | Prefix API tool itu belum ada di allowlist. Bila tool memang boleh dipakai, set `ALLOWED_API_PREFIXES` (daftar bawaan + prefix baru), lalu `docker compose up -d` |
| Operasi ditolak 503 "audit log unavailable" | `/healthz` | Hanya terjadi bila `AUDIT_FAIL_CLOSED=true`: perbaiki penulisan log (baris `/healthz` di atas) |
| `/healthz` 503, `log_ok:false` | `ls -ld logs`, `df -h` | Pemilik folder `logs` harus `AUDIT_UID:AUDIT_GID`, disk tidak penuh |
| audit-proxy "unhealthy" setelah ubah UID | `docker compose logs audit-proxy` | `sudo chown -R AUDIT_UID:AUDIT_GID logs` |
| Build image lambat/timeout | unduhan dari PyPI | Jaringan kantor lambat ke PyPI. Ulangi build, `UV_HTTP_TIMEOUT=300` sudah diset. Lihat catatan di bawah |
| Kolom `ip` di log selalu sama (172.x.x.x) | Docker Desktop/NAT | Normal di laptop uji. Di server Linux, IP asli staf tercatat |

**Catatan jaringan (ditemukan saat pengembangan):** dari jaringan pengembangan, unduhan ke PyPI dan MCR sangat lambat (~300 KB/s) atau putus, sedangkan Docker Hub dan Cloudflare normal. Ini bukan masalah MTU Docker. Bila build gagal karena timeout, cukup ulangi. Bila MTU memang bermasalah di jaringan lain (cek `ping -M do -s 1472 1.1.1.1` gagal tetapi `-s 1300` berhasil), set `"mtu": 1400` di `/etc/docker/daemon.json` lalu restart Docker.
