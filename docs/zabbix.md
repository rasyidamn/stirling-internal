# Integrasi Zabbix

Pemantauan memakai **Zabbix agent 2** yang dipasang di server PDF (host Docker). Semua item di bawah dibuat pada host tersebut di Zabbix frontend.

## 1. Persiapan agent

Agent harus bisa membaca folder log audit (`<folder-proyek>/logs`). Pemilik folder adalah uid `AUDIT_UID` (default 1000). Cara paling sederhana: tambahkan user `zabbix` ke grup pemilik folder.

```bash
sudo chgrp -R 1000 /opt/stirling-audit/logs      # sesuaikan path & gid
sudo chmod 750 /opt/stirling-audit/logs
sudo usermod -aG $(stat -c %G /opt/stirling-audit/logs) zabbix
sudo systemctl restart zabbix-agent2
```

Item log memerlukan **active check**. Isi `ServerActive=` di `/etc/zabbix/zabbix_agent2.conf` dan pastikan `Hostname=` sama dengan nama host di Zabbix.

## 2. Memantau log audit

File aktif selalu `logs/audit.jsonl`. Setiap tengah malam WIB, file itu diganti nama menjadi `audit-YYYY-MM-DD.jsonl` dan file baru dibuat. Karena itu gunakan **`logrt[]`** (log yang dirotasi), bukan `log[]`.

| Nama item | Key | Tipe | Keterangan |
|---|---|---|---|
| Audit: semua event | `logrt["/opt/stirling-audit/logs/^audit.*\.jsonl$",,,,skip]` | Zabbix agent (active), Log | Mengirim setiap baris ke Zabbix (bisa dicari di *Latest data*) |
| Audit: jumlah error | `log.count["/opt/stirling-audit/logs/audit.jsonl","\"result\":\"error\"",,,skip]` | Zabbix agent (active), Numeric | Jumlah baris error sejak cek terakhir |
| Audit: jumlah operasi | `log.count["/opt/stirling-audit/logs/audit.jsonl","\"result\":",,,skip]` | Zabbix agent (active), Numeric | Jumlah total event |
| Audit: ditolak | `log.count["/opt/stirling-audit/logs/audit.jsonl","\"result\":\"rejected\"",,,skip]` | Zabbix agent (active), Numeric | Akses tanpa login, endpoint terblokir, file terlalu besar |

Pakai interval update **1m**. Parameter `skip` membuat agent tidak membaca ulang isi lama saat pertama kali dipasang.

### Operasi per user (untuk deteksi lonjakan)

Buat **satu item per user dengan Low-Level Discovery** bisa jadi berlebihan untuk kantor kecil. Cara yang lebih sederhana adalah item dependent dari item "Audit: semua event" dengan preprocessing **JSONPath** `$.user`, lalu trigger memakai fungsi `count()` dengan pola.

Contoh item dependent: `audit.user`

- Master item: `Audit: semua event`
- Preprocessing: `JSONPath` → `$.user`
- Tipe informasi: Character

## 3. Contoh trigger

| Nama | Ekspresi | Severity |
|---|---|---|
| Lonjakan operasi oleh satu user (>50/jam) | `count(/PDF-Server/audit.user,1h,"budi","eq")>50` (buat satu trigger per user yang dicurigai, atau pakai LLD) | Warning |
| Lonjakan operasi total (>300/jam) | `sum(/PDF-Server/log.count["/opt/stirling-audit/logs/audit.jsonl","\"result\":",,,skip],1h)>300` | Warning |
| Rasio error tinggi (>20% dalam 1 jam, minimal 10 operasi) | `sum(/PDF-Server/log.count[...error...],1h) / sum(/PDF-Server/log.count[...result...],1h) > 0.2 and sum(/PDF-Server/log.count[...result...],1h) >= 10` | Average |
| Banyak akses ditolak (>20/jam) | `sum(/PDF-Server/log.count[...rejected...],1h)>20` | Warning |
| audit-proxy tidak sehat / log gagal ditulis | lihat bagian 4 | High |

Ganti `[...error...]` dan seterusnya dengan key lengkap dari tabel di bagian 2.

**Catatan lonjakan per user:** karena nama user berbeda-beda, trigger statis per nama tidak praktis. Alternatifnya: jalankan skrip harian (cron) yang menghitung operasi per user per jam dengan `jq` (lihat README bagian 4), lalu kirim hasil di atas ambang ke Zabbix memakai `zabbix_sender`.

```bash
# /etc/cron.hourly/pdf-audit-burst  (contoh)
LOG=/opt/stirling-audit/logs/audit.jsonl
SINCE=$(TZ=Asia/Jakarta date -d '1 hour ago' +%Y-%m-%dT%H:%M)   # ts di log memakai WIB
jq -r --arg s "$SINCE" 'select(.ts >= $s and .result=="success") | .user' "$LOG" \
  | sort | uniq -c | awk '$1>50 {print $2" "$1}' \
  | while read u n; do zabbix_sender -z ZABBIX_SERVER -s "PDF-Server" -k pdf.audit.burst -o "$u: $n operasi/jam"; done
```

Buat item **Zabbix trapper** `pdf.audit.burst` (Text), lalu trigger `nodata(/PDF-Server/pdf.audit.burst,10m)=0`, yang aktif setiap ada kiriman.

## 4. Health audit-proxy

`https://pdftools.local/healthz` bisa diakses tanpa login dan tidak dicatat di audit:

- HTTP **200** + `"log_ok": true`: normal.
- HTTP **503** + `"log_ok": false`: audit-proxy **tidak bisa menulis log** (disk penuh, izin folder salah). Dengan `AUDIT_FAIL_CLOSED=false` (default) staf tetap dilayani, tetapi aktivitasnya **tidak tercatat**. Dengan `true`, operasi PDF ditolak sampai log pulih. Segera tangani.

Lewat HTTPS hanya `status` dan `log_ok` yang ditampilkan. Pesan error lengkapnya ada di log container: `docker compose logs audit-proxy | grep "AUDIT LOG WRITE FAILED"`.

Item (di server PDF sendiri, sertifikat internal diabaikan):

| Nama | Key | Tipe |
|---|---|---|
| audit-proxy healthz | `web.page.get["https://pdftools.local/healthz"]` | Zabbix agent |

Atau, lebih bersih, buat **Web scenario** di host: URL `https://pdftools.local/healthz`, required status code `200`, string yang dicari `"log_ok":true`, centang *Verify host* = off (karena CA internal), atau pasang root CA Caddy di server Zabbix.

Trigger:

- `last(/PDF-Server/web.test.fail[healthz])<>0` → **High**: "audit-proxy tidak sehat / log audit gagal ditulis"

## 5. Container tetap hidup

Zabbix agent 2 punya plugin **Docker**. Tambahkan user `zabbix` ke grup `docker`:

```bash
sudo usermod -aG docker zabbix && sudo systemctl restart zabbix-agent2
```

Lalu tautkan template bawaan **"Docker by Zabbix agent 2"** ke host. Template ini otomatis menemukan semua container (LLD) dan punya trigger untuk container yang berhenti atau restart berulang.

Container yang harus selalu `running` dan (kecuali Caddy) `healthy`:

| Container | Healthcheck |
|---|---|
| `stirling-audit-stirling-pdf-1` | bawaan image |
| `stirling-audit-audit-proxy-1` | `/healthz` (juga 503 bila log gagal) |
| `stirling-audit-authelia-1` | bawaan image |
| `stirling-audit-caddy-1` | tidak ada, gunakan web scenario bagian 4 |

Item tambahan bila ingin eksplisit:

- `docker.container_info["stirling-audit-audit-proxy-1",full]` + preprocessing JSONPath `$.State.Health.Status`. Trigger bila nilainya bukan `healthy`.

## 6. Login gagal (log Authelia)

Percobaan login (berhasil/gagal) **tidak** ada di `audit.jsonl`, tetapi di log container Authelia. Agar bisa dipantau, tulis log Authelia ke file:

```yaml
# authelia/configuration.yml
log:
  level: info
  file_path: /data/authelia.log
```

Isi volume `authelia-data` ada di `/var/lib/docker/volumes/stirling-audit_authelia-data/_data/authelia.log`. Lalu buat item:

| Nama | Key |
|---|---|
| Login gagal | `log.count["/var/lib/docker/volumes/stirling-audit_authelia-data/_data/authelia.log","Unsuccessful 1FA authentication",,,skip]` |
| User diblokir (regulation) | `log.count[".../authelia.log","banned",,,skip]` |

Trigger contoh: `sum(/PDF-Server/log.count[...Unsuccessful...],10m)>10` → Warning "Banyak login gagal (kemungkinan tebak password)".
