# Investigasi Fase 0 — Perilaku Stirling-PDF v3.3.0

Tanggal: 13 Nov 2026. Metode: (1) membaca source berlisensi MIT pada tag `v3.3.0`, (2) uji API langsung dengan `curl`, (3) merekam lalu lintas UI asli lewat proxy pencatat (browser staf → proxy → Stirling), (4) OpenAPI `/v1/api-docs`.

## 1. Versi dan image

| Komponen | Tag dipin | Catatan |
|---|---|---|
| Stirling-PDF | `stirlingtools/stirling-pdf:3.3.0` | Rilis stabil terbaru (5 Nov 2026). Image ±3,5 GB. |

**Keputusan:** image standar `3.3.0`, bukan ultra-lite.
- Image ini dibangun dengan `STIRLING_FLAVOR=proprietary`, jadi modul berbayar ikut terpasang tetapi tidak aktif tanpa lisensi. Kita tidak memakai, mengaktifkan, atau menyalinnya.
- UI tetap memanggil `/api/v1/proprietary/ui-data/tool-recommendations`, yang menjawab **501** tanpa efek apa pun.

Folder berlisensi proprietary yang tidak dibaca: `app/proprietary`, `app/saas`, `engine`, `frontend/src/editor/{proprietary,desktop,saas,cloud}`, `frontend/src/processor`.

## 2. Ringkasan jawaban pertanyaan Fase 0

| Pertanyaan | Jawaban | Bukti |
|---|---|---|
| Operasi dikirim sebagai `POST /api/...` multipart? | **Ya**, untuk tool server-side. | Rekaman UI: `POST /api/v1/general/merge-pdfs`, `/general/split-by-size-or-count`, `/misc/compress-pdf`, semuanya `multipart/form-data`. |
| Hasil langsung atau lewat job async? | **Langsung (sinkron).** Body response berisi file. Async hanya aktif dengan `?async=true`, dan UI tidak memakainya. | Rekaman UI tanpa `async`. `curl ...merge-pdfs?async=true` → `{"async":true,"jobId":...}`. |
| WebSocket/SSE? | **Tidak ada** selama pemakaian UI. | Rekaman: tidak ada header `Upgrade` maupun `text/event-stream`. UI melakukan polling `GET /api/v1/notifications` tiap 30 detik. |
| Login bawaan bisa dimatikan? | Ya, `SECURITY_ENABLELOGIN=false` (default v3.3.0 = **true**). | Teruji: UI dan API terbuka tanpa login. |
| Fitur bisa disembunyikan/dimatikan? | Ya, `ENDPOINTS_TOREMOVE` / `ENDPOINTS_GROUPSTOREMOVE` (daftar dipisah koma). Endpoint yang dimatikan → **403**, dan UI menerima `enabled:false, reason:CONFIG`. | Teruji di container kedua. |

## 3. Tabel fitur → jenis request → bisa diaudit?

| Fitur | Endpoint / mekanisme | Jenis | Diaudit? | Bukti |
|---|---|---|---|---|
| Merge | `POST /api/v1/general/merge-pdfs` (`fileInput`×N, `sortType`, `removeCertSign`, `generateToc`, `clientFileIds`) | Server, sync | ✅ | Rekaman UI + `curl` (2 + 3 hlm → 5 hlm) |
| Split | `/api/v1/general/split-pages`, `split-by-size-or-count`, `split-pdf-by-sections`, `split-pdf-by-chapters`, `split-for-poster-print`, `/api/v1/misc/auto-split-pdf` | Server, sync, hasil **ZIP** (`application/octet-stream`) | ✅ | Rekaman UI |
| Compress | `POST /api/v1/misc/compress-pdf` | Server, sync | ✅ | Rekaman UI |
| Rotate (tool Rotate) | `POST /api/v1/general/rotate-pdf` | Server, sync | ✅ (source) | `hooks/tools/rotate`. Tidak muncul di rekaman, lihat §6 |
| Convert ke/dari PDF | `/api/v1/convert/*` (26 endpoint) | Server, sync | ✅ (source) | `hooks/tools/convert/useConvertOperation.ts`. Tidak muncul di rekaman, lihat §6 |
| **Page editor** (putar/hapus/susun halaman, export) | pdfium WASM di browser | **Browser** | ❌ | Rekaman: tidak ada POST, tetapi file berikutnya bernama `merged_a_edited…` |
| Sign | `endpoint: null`, pdfium | **Browser** | ❌ | `hooks/tools/sign/useSignOperation.ts`. Tidak ada POST di rekaman |
| Remove Annotations, Adjust Contrast, PDF Text Editor, Form Fill, anotasi viewer | pdfium WASM | **Browser** | ❌ | source |
| Auto Rotate | server menganalisis, rotasi di browser | Campuran | ⚠️ hash output ≠ file akhir | source |
| Validate Signature | validasi di server, laporan PDF dibuat di browser | Campuran | ⚠️ | source |
| Add/Remove Password | `/api/v1/security/add-password`, `remove-password` | Server | ✅, password **wajib disensor** | source |

## 4. Request non-operasi dari UI (diteruskan, tidak diaudit)

`GET /`, aset statis, `/api/v1/config/{app-config, endpoints-availability, endpoint-enabled, group-enabled, login-disclaimer}`, `/api/v1/info/{status, wau}`, `/api/v1/notifications` (polling 30 detik), `/api/v1/policies`, `/api/v1/policies/runs`, `POST /api/v1/automation/meter` (JSON kecil, 202), `/api/v1/proprietary/ui-data/*` (501).

## 5. Permukaan API yang lebih luas (OpenAPI)

Ada **128 endpoint POST multipart**. Selain tool PDF, ada grup yang tidak dibutuhkan staf dan berisiko: `/api/v1/admin`, `/database` (import DB), `/storage`, `/ai`, `/integration`, `/workflow`, `/pipeline`, `/docparse`. Juga ada endpoint non-multipart: `audit`, `user`, `team`, `invite`, dan lain-lain.

## 6. Keterbatasan investigasi

- Rotate dan convert tidak muncul di rekaman UI. Kemungkinan belum sempat dicoba lewat tool-nya, atau rotasi dilakukan lewat page editor (yang memang di browser). Source memastikan keduanya memanggil server. Akan diverifikasi ulang di integration test Fase 1.
- Unduhan dari PyPI/MCR di jaringan ini sangat lambat (~300 KB/s), sedangkan Cloudflare normal. Ini bukan masalah MTU Docker. Akan dicatat di `docs/operasional.md`.

## 7. Rekomendasi

**Konfigurasi Stirling** (env di compose):
```
SECURITY_ENABLELOGIN=false
SYSTEM_ENABLEANALYTICS=false
SYSTEM_ENABLEPOSTHOG=false
SYSTEM_ENABLESCARF=false
SYSTEM_SHOWUPDATE=false
SYSTEM_SHOWSETTINGSWHENNOLOGIN=false
SYSTEM_ENABLEMOBILESCANNER=false
SYSTEM_ENABLEMOBILESIGNATURE=false
SYSTEM_CORSALLOWEDORIGINS=https://pdf.<domain>
ENDPOINTS_GROUPSTOREMOVE=Automation,DeveloperTools,DeveloperDocs,DocParse
ENDPOINTS_TOREMOVE=auto-rotate-pdf,url-to-pdf
UI_DEFAULTHIDEUNAVAILABLETOOLS=true
```

**Desain audit-proxy:**
- Audit: semua `POST /api/v1/**` berbody `multipart/form-data`. Respons sinkron, hash output dihitung bertahap sambil streaming. Nama file diambil dari `Content-Disposition` dengan format tak lazim `form-data; name="attachment"; filename="..."`.
- ~~`job_id`: dukung secara defensif bila ada `async=true`~~ **Diganti 16 Nov 2026:** job async diblokir di audit-proxy (UI tidak memakainya; hasil job bisa diambil user lain yang tahu ID-nya).
- **Blokir (403) di proxy** prefix `/api/v1/{admin,database,storage,ai,integration,workflow,pipeline,docparse,user,team,invite,audit}` dan `/swagger-ui`, `/v1/api-docs`. Staf tidak membutuhkannya, dan memblokirnya mengecilkan permukaan serangan.
- Tidak perlu proxy WebSocket/SSE.

**Batasan yang diterima (keputusan 13 Nov 2026):** page editor, Sign, Remove Annotations, Adjust Contrast, PDF Text Editor, Form Fill, dan anotasi viewer diproses di browser sehingga **tidak teraudit**. Hal ini dicatat di README.
