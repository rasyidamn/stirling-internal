# Uji Login Active Directory (lab dan go-live)

Checklist untuk menyambungkan pdftools ke Active Directory dan memastikan perilakunya benar. Dipakai di lab (VM DC + VM Windows 10 join domain + VM Ubuntu) dan diulang saat go-live. Isi kolom **Hasil** setiap kali diuji.

Contoh nilai di bawah: domain `ad-lab.local`, DC `AD-SVR.ad-lab.local`, grup `GRP-PDF-Tools`. Ganti sesuai lingkungan Anda.

**Server PDF (Ubuntu) tidak perlu join domain.** Authelia hanya perlu: me-resolve dan menjangkau DC, akun service read-only, dan mempercayai CA yang menerbitkan sertifikat DC.

## 0. Jaringan

Dari server PDF:

```bash
ip -4 addr                      # IP server (kartu ens33/ens160, bukan docker0/br-*)
ping -c 2 <IP-DC>
nc -zv <IP-DC> 53               # DNS
nc -zv <IP-DC> 636              # LDAPS (setelah langkah 3)
```

Dari PC Windows 10: `ping <IP-server-PDF>`. Ketiga VM harus di jaringan VMware yang sama (mis. semuanya NAT/VMnet8).

## 1. Objek di AD (PowerShell admin di DC)

```powershell
Import-Module ActiveDirectory
$pw = Read-Host -AsSecureString "Password"

# akun service read-only: user biasa, password tidak kedaluwarsa
New-ADUser svc-pdftools -AccountPassword $pw -Enabled $true -PasswordNeverExpires $true
(Get-ADUser svc-pdftools).DistinguishedName        # -> AD_BIND_USER

# grup akses + grup bersarang
New-ADGroup GRP-PDF-Tools -GroupScope Global
New-ADGroup GRP-Nested    -GroupScope Global
Add-ADGroupMember GRP-PDF-Tools -Members GRP-Nested

# user uji
foreach ($u in "pdf.anggota","pdf.bersarang","pdf.luar","pdf.nonaktif") {
  New-ADUser $u -AccountPassword $pw -Enabled $true
}
Add-ADGroupMember GRP-PDF-Tools -Members pdf.anggota, pdf.nonaktif
Add-ADGroupMember GRP-Nested    -Members pdf.bersarang

# ambang lockout AD: harus LEBIH BESAR dari 5 (max_retries Authelia)
Get-ADDefaultDomainPasswordPolicy | Select LockoutThreshold
```

`LockoutThreshold` = `0` berarti lockout AD **mati**: akun tidak pernah terkunci, dan perlindungan tebak password hanya dari Authelia. Di lab, samakan dengan kebijakan produksi agar skenario 6 bermakna, misalnya:

```powershell
Set-ADDefaultDomainPasswordPolicy -Identity ad-lab.local -LockoutThreshold 10 `
  -LockoutDuration 00:15:00 -LockoutObservationWindow 00:15:00
```

Password akun service **tidak boleh mengandung tanda petik tunggal `'`**: nilainya disisipkan ke YAML ber-kutip tunggal di `authelia/backend-ldap.yml`.

## 2. DNS

```powershell
Add-DnsServerResourceRecordA -ZoneName ad-lab.local -Name pdftools -IPv4Address <IP-server-PDF>
```

Cek dari Windows 10: `nslookup pdftools.ad-lab.local` harus menjawab IP server PDF. Hapus entri `pdftools` lama di file hosts PC bila ada.

## 3. LDAPS di DC

1. Server Manager → Add Roles → **Active Directory Certificate Services** → Certification Authority → **Enterprise CA, Root CA**.
2. DC otomatis meminta sertifikat *Domain Controller*. Percepat dengan `certutil -pulse` atau restart DC.
3. Cek: `Test-NetConnection AD-SVR -Port 636` → `TcpTestSucceeded : True`.
4. Ekspor root CA dalam format PEM:
   ```powershell
   certutil -ca.cert C:\ad-root-ca.cer
   certutil -encode C:\ad-root-ca.cer C:\ad-root-ca.crt
   ```
5. Salin isi `C:\ad-root-ca.crt` (teks `-----BEGIN CERTIFICATE-----` …) ke server PDF sebagai `authelia/certs/ad-root-ca.crt`.

Cek dari server PDF:

```bash
openssl s_client -connect AD-SVR.ad-lab.local:636 -CAfile authelia/certs/ad-root-ca.crt </dev/null 2>/dev/null \
  | grep "Verify return code"          # harus: 0 (ok)
```

## 4. DNS di server PDF

Server PDF harus memakai DC sebagai DNS. Contoh netplan (`/etc/netplan/50-cloud-init.yaml`, nama kartu sesuaikan):

```yaml
network:
  version: 2
  ethernets:
    ens33:
      dhcp4: true
      dhcp4-overrides:
        use-dns: false
      nameservers:
        addresses: [<IP-DC>]
        search: [ad-lab.local]
```

```bash
sudo netplan apply
resolvectl query AD-SVR.ad-lab.local
# dari jaringan Docker yang dipakai Authelia:
docker run --rm --network stirling-audit_frontend alpine nslookup AD-SVR.ad-lab.local
```

DC harus meneruskan (forwarder) DNS internet, agar server tetap bisa menarik image Docker. Cek di DC: `Get-DnsServerForwarder`. Bila kosong, tambahkan gateway jaringan, mis. VMware NAT: `Add-DnsServerForwarder -IPAddress 192.168.126.2`. Uji dari server PDF: `resolvectl query registry-1.docker.io`.

## 5. Konfigurasi `.env`

```bash
PDF_HOSTNAME=pdftools.ad-lab.local
PDF_GROUP=GRP-PDF-Tools
AUTH_BACKEND=ldap
AD_LDAP_URL=ldaps://AD-SVR.ad-lab.local:636
AD_TLS_SERVER_NAME=AD-SVR.ad-lab.local
AD_BASE_DN=DC=ad-lab,DC=local
AD_BIND_USER=CN=svc-pdftools,CN=Users,DC=ad-lab,DC=local
AD_BIND_PASSWORD=<password svc-pdftools>
```

```bash
docker compose up -d --wait
docker compose logs authelia | tail -30     # tidak boleh ada error LDAP / TLS / x509
curl -sk https://pdftools.ad-lab.local/healthz
```

Bila Authelia gagal karena TLS, uji sementara `AD_LDAP_URL=ldap://AD-SVR.ad-lab.local:389` untuk memisahkan masalah sertifikat dari masalah akun/DN. Setelah ketemu, **kembali ke `ldaps://`**.

## 6. Root CA Caddy ke PC domain (GPO)

```bash
docker compose cp caddy:/data/caddy/pki/authorities/local/root.crt ./caddy-root.crt
```

Impor ke GPO *Computer Configuration → Policies → Windows Settings → Security Settings → Public Key Policies → Trusted Root Certification Authorities* (lihat `docs/operasional.md` §2). Di Windows 10: `gpupdate /force`.

## 7. Skenario uji (dari Windows 10, login domain)

Buka `https://pdftools.ad-lab.local`: **tidak boleh ada peringatan sertifikat**. Login memakai nama user AD (sAMAccountName, mis. `pdf.anggota`) dan password AD. Ini login form, bukan SSO otomatis Windows.

| # | Skenario | Harapan | Hasil |
|---|---|---|---|
| 1 | `pdf.anggota` login, lalu merge 2 PDF | Masuk. Baris di `logs/audit.jsonl` berisi `user: pdf.anggota`, `groups` dari AD, `ip` = IP Windows 10 | |
| 2 | `pdf.luar` login | 403 | |
| 3 | `pdf.bersarang` login (grup bersarang) | 403 dengan filter bawaan. Aktifkan baris `groups_filter` di `authelia/backend-ldap.yml`, `docker compose up -d`, ulangi → masuk | |
| 4 | `pdf.nonaktif` login, lalu `Disable-ADAccount pdf.nonaktif` | Akses hilang paling lama 5 menit | |
| 5 | `Remove-ADGroupMember GRP-PDF-Tools pdf.anggota` saat masih login | 403 paling lama 5 menit | |
| 6 | 5× password salah untuk `pdf.anggota` | Authelia memblokir 15 menit. Akun AD **tidak** terkunci: `(Get-ADUser pdf.anggota -Properties LockedOut).LockedOut` = `False` | |
| 7 | Buka `https://pdftools.ad-lab.local/logout` | Kembali ke halaman login | |

Perintah cek log di server PDF:

```bash
jq -c '{ts, user, groups, ip, operation, result, status}' logs/audit.jsonl | tail
docker compose logs authelia | grep -iE "unsuccessful|banned|ldap"
```

Kembalikan keanggotaan/aktifkan akun setelah uji (`Add-ADGroupMember`, `Enable-ADAccount`).
