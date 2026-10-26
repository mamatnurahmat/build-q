# PRD: `bq bin`, tool CLI terpusat di PocketBase

> **Version:** 0.3 (plan final sebelum fase 1; review ulang Security: PASS BERSYARAT untuk fase 1)
> **Last Updated:** 2026-10-26
> **Ticket:** HAG-63
> **Target build-q:** v0.2.6+
> **Status:** Dokumen saja. Belum ada kode, collection, atau merge. Tidak ada ronde review dokumen lagi; Security mereview kode fase 1.
> **Riwayat:** v0.1 (`2b281bd`), v0.2 (`48d8919`, review v1 FAIL), v0.3 (review v2 PASS BERSYARAT fase 1 PB lokal, tier `external` belum lulus)
> **Sumber review:** `security-engineer-mvkyygr4/hag-63-review.md` (review v1 dan review ulang)
> **CLI (usulan):** `bq bin ls` · `bq bin show <cmd>` · `bq bin [--version <v>] <cmd> <args...>` · `bq bin --dry-run <cmd> <args...>` · `bq bin publish|release|rollback|yank|deprecate`

---

## 0. Ringkasan

Tujuan tidak berubah: tool CLI tim (bash dan python) disimpan terpusat di PocketBase, dijalankan lewat `bq bin`, dan dirawat dengan versi, review, dan rollback.

**Lingkup fase 1 (dikunci di v0.3):**

- Hanya PocketBase lokal (bukan produksi, bukan staging).
- Hanya tier `read`, tanpa jaringan sama sekali.
- Tier `external` **dinonaktifkan**. Tidak ada tool external yang bisa jalan lewat `bq bin` sampai proxy egress localhost dipasang dan lulus review (lihat 13.1).
- Tidak ada biner. Tidak ada exec `yq`, `curl`, `gh`, `kubectl`, `git`, atau `docker`.
- Rilis dinonaktifkan sampai dua akun manusia dengan peran `tool_releaser` ada (syarat 5).

**Prinsip yang tetap berlaku:**

1. Tool tidak melihat kredensial. Sandbox OS, `HOME` kosong, tanpa token.
2. Identitas datang dari server (`e.auth`), bukan dari body.
3. Manifest bertanda tangan adalah satu-satunya sumber kebenaran untuk eksekusi.
4. State hanya berubah lewat hook. Klien tidak menulis langsung.

**Enam syarat sebelum fase 1** (status ada di bagian 8 dan 12):

| No | Syarat | Bagian |
|---|---|---|
| 1 | Tier `external` dinonaktifkan; tier `read` tanpa jaringan | 4.2, 0 |
| 2 | Profil sandbox lengkap (keychain `~/Library`, mach `securityd`, `~/.build-q`) dan ujinya | 4.2, Lampiran A, 7.1 |
| 3 | Jalur draft didefinisikan (N4) | 5.5 |
| 4 | `bq` hanya membaca `manifest_json` (N7) | 3.2, 4.1 |
| 5 | Deteksi token bersama didefinisikan (N6); fallback admin pribadi dihapus | 3.4, 4.1 |
| 6 | Uji keamanan bagian 7 jadi gerbang fase 2 | 7, 8 |

---

## 1. Latar dan prasyarat

### 1.1 Yang sudah ada di build-q

| Komponen | Lokasi | Catatan |
|---|---|---|
| Login SSO dan token user | `build_q/pb_auth.py`, `~/.build-q/auth.json` | Token tidak pernah masuk proses tool. Gate `requires_login()` di `cli.py:main()` mencakup `bq bin`. |
| Ambil record | `pb_api.fetch_collection_records()` | Dipakai untuk katalog. |
| Ambil secret per key | `pb_api.fetch_secrets()` | Hanya nilai yang dideklarasikan tool, tanpa cache (P0-a). |
| Cache rahasia | `pb_api._write_cache()`, `.pb-cache.json` | **Menyimpan nilai rahasia teks biasa** (`pb_api.py:266-271`). Harus diubah (P0-a). |
| Fallback superuser | `pb_api.get_token()` membaca `PB_API_USER/PASS` dari `.env` (`pb_api.py:222`) | Tidak dipakai `bq bin` (P0-b), dan dihapus dari mesin perilis (N10). |

### 1.2 Prasyarat P0 (wajib sebelum fase 1)

- **P0-a:** cache `pb_api` tidak menyimpan nilai rahasia. Nilai rahasia hanya di memori satu proses.
- **P0-b:** `bq bin` tidak memakai fallback superuser. Hanya token SSO pribadi.
- **P0-c:** setiap orang dan setiap agent punya akun PocketBase sendiri. Token bersama tidak dipakai di jalur `bq bin`. (Menunggu keputusan manusia, bagian 10.)
- **P0-d:** field `kind` (`human` atau `agent`) di `devops_users` untuk deteksi token bersama (N6). Perubahan skema produksi, menunggu keputusan manusia.

### 1.3 Inventaris skrip (read-only)

| Sumber | Jumlah | Rincian |
|---|---|---|
| `~/.claude/skills/devops-bin/bin/` | 66 berkas | 57 bash, 9 python |
| `~/.build-q/bin/` | 1 berkas | `docker-buildx` (biner), tetap lokal |
| `~/.agents/bin/` | tidak ada | Skill `devops-bin` masih menyebut lokasi ini |

Temuan audit yang wajib ditangani sebelum migrasi:

- **Kredensial default tertanam di skrip.** Pemindaian pola menemukan nilai default untuk kata seperti PASS, TOKEN, atau SECRET di 7 berkas: `bootstrap-app`, `get-file-secret`, `cicd-webhook`, `gh-fetch-secret`, `image-ready`, `jx-ci-trigger-setup`, `trigger-ci-init`. `pb-webhook-batch` juga punya default password PocketBase (ditemukan lewat pemeriksaan manual). Nilai tidak dicetak di dokumen ini. Tindakan: skrip-skrip itu tidak dimigrasi, DevOps diminta menilai apakah kredensial tersebut perlu dirotasi.
- Ketergantungan path: 9 skrip merujuk `~/.pi/`, 10 merujuk `~/.agents/`.
- `k8s-dump` dan `k8s-secret` memakai `yq`. Skrip saling memanggil.

---

## 2. UX CLI

### 2.1 Perintah

```
bq bin ls [--json] [--runtime bash|python] [--tier read|external]
bq bin show <cmd> [--version <v>] [--body] [--json]
bq bin [--version <v>] <cmd> [args...]          # argumen diteruskan apa adanya
bq bin --dry-run <cmd> [args...]                # rencana, tidak menjalankan
bq bin --local <file> [args...]                 # jalur draft, lihat 5.5
bq bin publish <file> --name <cmd> --version <semver> --manifest <file.json> --changelog "<teks>"
bq bin release <cmd> <version>                  # perilis manusia (bagian 3.4), tidak tersedia sampai syarat 5
bq bin rollback <cmd> <version>                 # perilis
bq bin yank <cmd> <version> --reason "<teks>"   # perilis
bq bin deprecate <cmd> [--replacement <cmd>]    # perilis
```

Aturan:

- `publish` boleh dipakai akun agent (bila P0-c dan peran agent disetujui), hanya membuat draft. Hook menolak `state` selain `draft` dari klien.
- `release`, `rollback`, `yank`, dan `deprecate` menolak sesi yang bukan `kind = human`, dan menolak token bersama.
- `--version` dan `--dry-run` dan `--local` harus muncul sebelum `<cmd>`. Token setelah `<cmd>` diteruskan apa adanya, tanpa diparse `bq`.
- Exit code tool diteruskan. Galat `bq` memakai 120–125 (120 tool tidak ada, 121 ditarik, 122 manifest atau tanda tangan atau sha256 gagal, 123 offline dan cache tidak layak, 124 env wajib tidak ada, 125 sandbox tidak tersedia atau tier tidak diizinkan di fase ini). Galat `bq` dicetak dengan prefiks `[bq-bin]` di stderr.

### 2.2 `--dry-run`

Menampilkan: nama, versi, status, tier, runtime, `signer_key_id`, hasil verifikasi manifest dan tanda tangan, sumber (`pb`, `cache`, `stale-cache`), profil sandbox, nama env dan statusnya (nilai tidak pernah ditampilkan), hasil pemeriksaan pola token pada argumen, dan argv sebagai literal. Tidak menjalankan tool.

---

## 3. Skema PocketBase

Nama collection diusulkan. Belum dibuat. Pembuatan hanya di PocketBase lokal (bagian 8).

### 3.1 `cli_tools`

| Field | Tipe | Keterangan |
|---|---|---|
| `name` | text | Unik. Pola `^[a-z][a-z0-9-]{1,60}$`. Nama bentrok dengan perintah bq atau berkas di `~/.build-q/bin` ditolak. |
| `description` | text | |
| `owner` | relation → `devops_users` | |
| `current_version` | relation → `cli_tool_versions` | Diubah hanya oleh hook. |
| `status` | select | `active`, `deprecated`, `yanked`. Diubah hanya oleh hook. |
| `replacement` | text | |
| `active` | bool | |

Tidak ada `runtime`, `tier`, `env_required`, `requires`, atau `args_spec` di sini.

### 3.2 `cli_tool_versions`

| Field | Tipe | Keterangan |
|---|---|---|
| `tool` | relation → `cli_tools` | |
| `version` | text | SemVer, unik per `tool`. |
| `state` | select | `draft`, `released`, `yanked`. Hanya hook. |
| `manifest_json` | json | **Satu-satunya sumber kebenaran untuk eksekusi** (N7). Isinya di 3.3. Ditandatangani. |
| `runtime`, `tier`, `env_required`, `requires`, `args_spec` | kolom | Salinan untuk query dan tampilan saja. `bq` tidak membacanya. Hook menolak rilis bila salinan tidak sama dengan `manifest_json`. |
| `body` | file, maks 512 KB | Isi script. |
| `sha256` | text | Hash `body`, dihitung server, dicocokkan dengan manifest. |
| `signature` | text | Ed25519, base64. Wajib untuk `released`. |
| `signer_key_id` | text | Lihat 4.5. |
| `changelog` | text | Wajib. |
| `author` | relation → `devops_users` | **Diisi hook dari `e.auth.id`.** Nilai di body diabaikan. |
| `releaser` | relation → `devops_users` | **Diisi hook dari `e.auth.id`.** Nilai di body diabaikan. |
| `released_at` | date | Diisi hook. |
| `yanked_reason` | text | Wajib bila `yanked`. |

Immutable setelah `released`: `body`, `sha256`, `manifest_json`, `signature`, `signer_key_id`, `version`, dan semua kolom salinan. Hanya `state` (ke `yanked`) dan `yanked_reason` yang boleh berubah, dan hanya oleh hook. Hapus record tidak diizinkan.

### 3.3 Manifest bertanda tangan

JSON kanonik (kunci urut, tanpa spasi, UTF-8). Seluruh manifest ditandatangani:

```json
{
  "schema": 1,
  "name": "kustomize-init",
  "version": "0.1.0",
  "runtime": "bash",
  "tier": "read",
  "env_required": [],
  "requires": [],
  "args_spec": {"usage": "kustomize-init [dir] [--ns <namespace>] [--out <file>]", "max_args": 5},
  "sha256": "<hash body>",
  "author": "<id PB penulis>",
  "releaser": "<id PB perilis>",
  "released_at": "<UTC>"
}
```

Field `tier`, `runtime`, `env_required`, dan `requires` berada di manifest, bukan di kolom. Verifikasi tanda tangan dilakukan di `bq` dengan kunci yang dipin, dan di hook rilis dengan kunci terdaftar.

### 3.4 Akses per peran

Peran yang ada di `devops_users.role` (schema produksi): `view`, `admin`, `incident_support`, `incident_owner`.

Peran yang belum ada: `tool_releaser` (perilis) dan `tool_author` (agent yang hanya membuat draft). Keduanya menunggu keputusan manusia dan perubahan skema.

| Aksi | `view` | `incident_*` | `tool_releaser` (manusia) | `tool_author` (agent) |
|---|---|---|---|---|
| `ls`, `show`, jalankan `released` tier `read` | ya | ya | ya | ya |
| Baca `body` draft | tidak | tidak | ya, bukan penulis | penulisnya saja |
| Buat draft | tidak | tidak | ya | ya |
| `release`, `rollback`, `yank`, `deprecate` | tidak | tidak | ya, bukan penulis draft | tidak |

**Fallback admin pribadi dihapus (syarat 5).** Admin tidak merilis sampai ada dua akun `tool_releaser` manusia. Sampai itu, rilis dinonaktifkan di semua mesin.

**Deteksi token bersama (N6):** hook menolak `publish` dari `tool_author`, dan menolak semua aksi rilis, bila `e.auth.kind != "human"`. `bq` melakukan pemeriksaan yang sama lebih dulu: mengambil record `e.auth` dan menolak bila `kind` bukan `human` untuk aksi rilis. Field `kind` belum ada (P0-d). Tanpa field itu, rilis tidak bisa dibuka, dan itu aman.

### 3.5 Klien tidak menulis langsung

- `createRule`, `updateRule`, dan `deleteRule` untuk `cli_tools`, `cli_tool_versions`, `cli_tool_audit`, dan `cli_signing_keys` kosong (tidak bisa diakses klien).
- Penulisan lewat route kustom: `/api/bq/bin/publish`, `/release`, `/rollback`, `/yank`, `/deprecate`. Setiap route memeriksa `e.auth`, mengabaikan `author`, `releaser`, `state`, dan `released_at` dari body, memvalidasi manifest dan sha256, dan menulis dengan `$app`.
- `listRule`: `cli_tools` untuk pengguna terautentikasi dengan `active = true`. `cli_tool_versions` untuk `released`, dan draft hanya untuk penulis dan perilis.

### 3.6 `cli_tool_audit` dan `cli_signing_keys` (ditulis hook saja)

- `cli_tool_audit`: setiap publish, release, rollback, yank, deprecate, dan penolakan. Kolom: `action`, `tool`, `version`, `actor`, `actor_kind`, `result`, `reason`, `at`. Tidak bisa diubah atau dihapus.
- `cli_signing_keys` (N5): `key_id`, `owner` (manusia), `public_key`, `valid_from`, `revoked_at`. Hanya bisa diubah lewat route pencabutan, yang butuh dua perilis manusia berbeda. Hook memakainya untuk verifikasi kedua. `bq` tetap memakai daftar yang dipin (4.5).

---

## 4. Eksekusi

### 4.1 Alur `bq bin <cmd> args...` (fase 1)

1. Cek login pribadi (`requires_login`). Token bersama atau fallback superuser ditolak (P0-b, P0-c).
2. Muat indeks lokal `~/.build-q/bin/tools/index.json` (nama, versi aktif, sha256, `signer_key_id`, `fetched_at`). Tidak ada nilai rahasia.
3. Ambil katalog dari PB bila indeks lebih tua dari 1 jam. Gagal: lanjut ke 4.6.
4. Tentukan versi, cek status: `yanked` ditolak (121), `deprecated` jalan dengan peringatan.
5. Ambil `body` dan `manifest.json` dari cache atau PB. Cache hanya berisi `released`.
6. **Verifikasi manifest (syarat 4):**
   - `bq` membaca `manifest_json` dari record dan **hanya** nilai di dalamnya (runtime, tier, env, requires, args). Kolom salinan diabaikan.
   - Tanda tangan manifest diverifikasi dengan kunci publik yang dipin di `bq`. Gagal: 122, cache entri dihapus.
   - sha256 `body` dihitung dan dicocokkan dengan `sha256` di manifest. Gagal: 122.
   - Verifikasi diulang setiap jalan, termasuk dari cache.
7. **Gerbang tier (syarat 1):** fase 1 hanya menerima `tier = read` dan `requires = []` dan `env_required = []` atau env yang disetujui. Tier `external` atau `requires` tidak kosong ditolak dengan 125.
8. Tentukan env: hanya nama dalam `env_required` dari manifest. Nilai diambil di memori (P0-a). Nama tidak ada: 124.
9. Siapkan direktori kerja `~/.build-q/run/<random>` mode 700. `HOME` yang dilihat tool adalah direktori kosong di dalamnya.
10. Tulis `body` ke `run.<ext>` mode 700 dari byte yang sudah diverifikasi di memori.
11. Jalankan di bawah sandbox (4.2) dengan argv: `/bin/bash <file> <args>` atau `/usr/bin/python3 -I <file> <args>` (path interpreter tetap). Stdin, stdout, dan stderr diteruskan.
12. Hapus direktori kerja di `finally`, termasuk saat Ctrl-C.
13. Exit dengan exit code anak.

### 4.2 Sandbox OS (syarat 1 dan 2)

- Mekanisme: `sandbox-exec -D HOME=<dir kosong> -f <profil> <interpreter> ...`. Profil ditulis sebagai berkas di `build_q/sandbox/`, belum dibuat. Draf lengkap ada di Lampiran A.
- **Fase 1: tier `read` tanpa jaringan sama sekali.** Profil memakai `(deny network*)`. Tidak ada daftar host.
- **Tier `external` dinonaktifkan** di fase 1. Alasan: `sandbox-exec` hanya bisa mengizinkan `*` atau `localhost` (review N1). Jalur yang dipertimbangkan: proxy egress di localhost yang memegang daftar host per manifest. Dicatat sebagai prasyarat fase external (13.1), tidak dikerjakan sekarang.
- Deny baca (wajib, diuji di 7.1): `~/Library` (termasuk keychain), `~/.build-q`, `~/.claude`, `~/.ssh`, `~/.kube`, `~/.config`, `~/.agents`, `~/.pi`, dan seluruh `HOME` asli.
- Deny mach: `mach-lookup` ditolak untuk `securityd` dan layanan keychain lainnya. Hanya layanan yang dibutuhkan interpreter yang boleh.
- Exec: hanya interpreter yang ditentukan. `process-exec` lain ditolak, sehingga tool tidak bisa memanggil `bq` atau biner lain (rekursi juga ditolak lewat `BQ_BIN_DEPTH`).
- Gagal tertutup: bila `sandbox-exec` tidak ada, gagal dimuat, atau profil tidak cocok dengan versi macOS, `bq` menolak dengan 125. Tidak ada jalur tanpa sandbox.
- Versi macOS: matriks uji dijalankan di setiap versi macOS yang dipakai engineer (N9). Hasilnya dicatat sebelum fase 2.

### 4.3 Tool tidak menerima token

- Env anak dibangun dari kosong: `PATH=/usr/bin:/bin:/usr/sbin:/sbin`, `LANG`, `HOME` kosong sementara, `BQ_BIN_DEPTH=1`, dan nilai env yang dideklarasikan.
- Tidak ada `PB_*`, token, atau `auth.json` di env atau di sandbox.
- Tier `read` tanpa jaringan berarti tidak ada rahasia yang bisa keluar dari sandbox. Ini membuat risiko N1 tidak berlaku di fase 1.

### 4.4 Cache tanpa nilai rahasia (syarat 4, KRITIS 1)

- `index.json`, `manifest.json`, dan `body` mode 600 di direktori mode 700. Tidak ada nilai rahasia di cache.
- `.pb-cache.json` tidak menyimpan nilai (P0-a).
- Cache hanya `released`. Versi yang ditarik dihapus dari cache.

### 4.5 Kunci penandatanganan dan pencabutan (N5)

- Kunci privat: satu pasang Ed25519 per perilis manusia, di keychain macOS dengan kontrol akses user-presence (konfirmasi sentuh atau kata sandi per tanda tangan). Tidak di PB, tidak di repo, tidak di berkas teks.
- Kunci publik: dipin di `build_q/keys/bin-release-keys.json` (`key_id`, pemilik, kunci publik, `valid_from`, `revoked_at`). Mengubah daftar berarti rilis `bq`.
- Registri `cli_signing_keys` di PB hanya lapisan kedua. Hook memeriksa tanda tangan terhadap registri juga.
- Pencabutan: `revoked_at` di registri, dan hook mengubah versi yang ditandatangani kunci itu ke `yanked`. Klien menerima daftar cabut lewat indeks yang ditandatangani. Jeda antara pencabutan dan penolakan di klien: dibatasi oleh TTL katalog (1 jam) untuk klien online; untuk klien offline, batas offline tier `read` (72 jam). Keputusan manusia (10.3).

### 4.6 Perilaku offline

| Kondisi | Tier `read` |
|---|---|
| PB terjangkau | ambil katalog, jalan |
| PB tidak terjangkau, cache < 72 jam | jalan, peringatan `stale-cache` |
| PB tidak terjangkau, cache lebih tua atau tidak ada | exit 123 |

Tier `external` punya batas 24 jam, tetapi tier itu dinonaktifkan di fase 1.

### 4.7 Pemeriksaan argumen dan token (SEDANG 6)

Argumen yang cocok pola token umum (`ghp_`, `xox`, `AKIA`, JWT, atau flag `--token`, `--password`, `--secret`) ditolak di `--dry-run` dan eksekusi. Daftar pola ada di `bq`. Ini pagar, bukan jaminan. Argumen tetap terlihat di `ps` bila lolos.

### 4.8 Integritas dan file sementara

- Byte `body` diverifikasi di memori, lalu ditulis ke file baru (`O_EXCL`, mode 700) di direktori mode 700. Tidak ada jendela baca ulang.
- Proses lain di akun yang sama tidak bisa mengubah isi yang sudah diverifikasi, dan tidak bisa membaca direktori `bq` karena sandbox.

### 4.9 Tanpa `eval`

Body tidak pernah dikonkatenasi ke shell. Argumen masuk sebagai argv. Tes wajib (7) membuktikan `$(...)`, backtick, dan `;` tampil literal.

---

## 5. Alur maintenance

### 5.1 Publish (draft)

1. Penulis memanggil `bq bin publish`. Route membuat `cli_tools` (bila baru) dan `cli_tool_versions` dengan `state = draft`. `author` dari `e.auth.id`.
2. Penulis membuka review isi (tiket atau PR).

### 5.2 Release (perilis manusia, syarat 5)

Tersedia hanya setelah dua akun `tool_releaser` manusia dan `kind` ada. Sampai itu, rilis dinonaktifkan.

1. Perilis dengan sesi pribadi menjalankan `bq bin release <cmd> <versi>`.
2. `bq` mengecek `kind = human` dan `e.auth.id != author`.
3. `bq` menampilkan **seluruh body**, `sha256`, daftar env, dan tier. Tidak hanya diff (N8).
4. Perilis mengetik ulang 12 karakter pertama sha256. Tanpa itu, tanda tangan tidak dibuat.
5. Tanda tangan dibuat dengan keychain (4.5), dengan konfirmasi user-presence.
6. Route release memeriksa: role `tool_releaser`, `kind = human`, `e.auth.id != author`, sha256 cocok, tanda tangan valid dengan kunci terdaftar milik `e.auth.id`, kolom salinan sama dengan manifest (N7), state `draft`.
7. Hook menulis `state = released`, `releaser = e.auth.id`, `released_at`, dan audit.

### 5.3 Update = versi baru

Versi lama tetap `released`. Dijalankan dengan `--version`.

### 5.4 Rollback dan yank

- `rollback`: target `released` dan tidak `yanked`. Hook memindahkan `current_version`. Tanda tangan target sudah ada.
- `yank`: hook mengubah `state` ke `yanked`, mewajibkan alasan, dan memindahkan `current_version`. Tool tanpa versi tersisa menjadi `deprecated`.

### 5.5 Jalur draft (N4)

- Draft **tidak pernah** dijalankan dari PB atau cache.
- `bq bin --local <file>` menjalankan berkas lokal yang ditulis penulis, dengan sandbox yang sama dan aturan yang sama: tier `read`, tanpa jaringan, tanpa env, tanpa `requires`.
- Keluaran diberi tanda `UNSIGNED` di stderr pada setiap jalan. Tidak ada sha256 yang dicek terhadap PB.
- `--local` tidak menulis ke cache, tidak membaca PB, dan tidak bisa menjalankan record yang sudah `released`.
- Jalur ini tidak mengizinkan tanda tangan palsu, karena tidak ada tanda tangan yang diperiksa. Hasilnya tidak boleh dianggap sebagai rilis.

### 5.6 Deprecate

Perilis mengubah `status` ke `deprecated` dengan `replacement`.

---

## 6. Inventaris dan migrasi

| Kelompok | Contoh | Fase |
|---|---|---|
| A. Baca lokal, tanpa jaringan, tanpa rahasia | `kustomize-init`, `apigw-docs` (lihat Lampiran B) | fase 1 |
| B. Baca dengan token atau jaringan | `gh-*`, `tag-match`, `image-ready`, `gitops-match-gh-latest-tag`, `svc-endpoints-audit`, `k8s-latest-tag` | setelah proxy egress (13.1). Tier `external` |
| C. Ubah cluster atau GitOps | `set-image`, `k8s-scale-active-deploys`, `gitops-audit`, `gh-k8s-dump`, `apply-file-config` | fase 4, setelah external |
| D. Rahasia atau SOPS | `k8s-secret`, `sops-*`, `source-auth`, `source_auth.py`, `gh-fetch-secret`, `get-file-secret`, `argocd-app token-set` | tetap lokal. Tidak dimigrasi di v0.3 |
| E. Bootstrap dan init | `cicd-init*`, `k8s-init-*`, `init-master`, `bootstrap-app` | fase 4 |
| F. Biner | `docker-buildx` | tetap lokal |

Skrip dengan kredensial default tertanam (1.3) tidak dimigrasi sampai DevOps memutuskan rotasi.

---

## 7. Rencana tes

Lingkungan: PocketBase lokal dari DevOps (port 8193 ke atas; 8192 hanya bila DevOps setuju). Data uji berawalan `TEST-` atau `UJI-BIN-`, dihapus DevOps. Tidak ada uji di produksi.

### 7.1 Sandbox (syarat 2, gerbang fase 2)

Tool uji (gema) dijalankan di bawah profil Lampiran A, dan setiap kasus harus gagal kecuali yang diberi tanda "harus berhasil":

- baca `~/Library/Keychains` gagal (tidak hanya rc, pesan `Operation not permitted`);
- `security find-generic-password` atau panggilan mach ke `securityd` gagal;
- baca `~/.build-q/auth.json`, `~/.build-q/.pb-cache.json`, dan `~/.build-q/.env` gagal;
- baca `~/.kube`, `~/.ssh`, `~/.config`, `~/.claude`, `~/.agents`, `~/.pi` gagal;
- `HOME` di dalam tool adalah direktori kosong; `env` tidak berisi token;
- koneksi TCP apa pun gagal (`curl` ke `127.0.0.1` dan `example.com` keduanya gagal);
- `exec` binari lain (mis. `/bin/ls` dari luar daftar) gagal;
- tool yang menulis ke direktori kerjanya sendiri **harus berhasil** (kontrol positif).

### 7.2 Manifest dan rilis

- Kolom salinan diubah tanpa mengubah `manifest_json`: `bq` tetap memakai manifest, dan hook menolak rilis.
- `manifest_json` diubah (mis. `env_required` ditambah) tanpa tanda tangan baru: ditolak, 122.
- `body` diubah tanpa mengubah sha256: ditolak, 122.
- Kunci yang dicabut: ditolak.
- `state` diubah ke `released` lewat API langsung: 403.
- Penulis merilis draft miliknya: ditolak.
- `author` atau `releaser` di body: diabaikan, nilai dari `e.auth`.
- Sesi `kind != human` mencoba rilis: ditolak. Token bersama mencoba rilis: ditolak di `bq` dan hook.
- `tool_author` mencoba rilis: ditolak.
- Rilis tanpa konfirmasi sha256: tidak ada tanda tangan.

### 7.3 Eksekusi

- Argumen `$(...)`, backtick, `;`, dan token uji (`ghp_...`) literal, dan token ditolak pemeriksaan pola.
- Rekursi: tool yang memanggil `bq bin` ditolak.
- Tier `external` dan `requires` tidak kosong: ditolak 125.
- `--dry-run` tidak menjalankan tool (tool menulis penanda, penanda tidak ada) dan tidak menampilkan nilai env.
- `--local` menandai `UNSIGNED`, tidak menulis cache, dan tidak membaca PB.
- Exit code `0`, `1`, `42`, `130` diteruskan.
- Offline: `read` jalan sampai 72 jam, lebih lama exit 123.
- File sementara terhapus saat sukses, gagal, dan Ctrl-C.
- Mode 700 dan 600 terpenuhi.

### 7.4 Fungsional

Publish → draft → release (dua perilis berbeda, di PB lokal setelah syarat 5) → jalankan → rollback → yank.

---

## 8. Fase rollout

| Fase | Isi | Gerbang keluar |
|---|---|---|
| 0. Prasyarat | P0-a sampai P0-d. Keputusan manusia (10). Matriks uji macOS (N9) | P0 selesai |
| 1. PB lokal, baca saja | Schema, route, hook, `cli_tool_audit`, `cli_signing_keys`. `ls`, `show`, `--dry-run`, eksekusi tier `read` tanpa jaringan. Lingkup tool: Lampiran B. Rilis **nonaktif** sampai syarat 5 | Lulus 7.1 dan 7.2 (sebagian), Security mereview kode |
| 2. Draft dan rilis | `publish`, `--local`, `release` dengan dua perilis manusia di PB lokal | **Lulus seluruh bagian 7**, termasuk 7.3 dan 7.4 (syarat 6) |
| 3. Rilis lanjutan | `rollback`, `yank`, `deprecate`. Matriks macOS dan paritas `bwrap` (N9) direncanakan | Dua siklus rilis tanpa insiden |
| 4. Tier external | Proxy egress localhost (13.1), lalu migrasi grup B dan C | Review DevOps dan Security per tool |
| 5. Produksi | Collection dan route di produksi **hanya** setelah izin tertulis dari manusia dan Security PASS | Izin tertulis |

Tidak ada kode fase 1 yang dibuat sebelum Boss menerima dokumen ini.

---

## 9. Threat model (v0.3, ringkas)

| Ancaman | Kontrol | Risiko residual |
|---|---|---|
| Tool membaca kredensial | Sandbox, HOME kosong, tanpa token (4.2, 4.3), tier `read` tanpa jaringan | `sandbox-exec` usang (N9) |
| Tool membawa rahasia keluar | Fase 1: tidak ada jaringan, tidak ada env rahasia di luar deklarasi | Tier external belum aman (13.1) |
| Penulis atau akun tak berhak mengubah `released` | Klien tidak menulis langsung (3.5) | Akun admin yang dibajak tanpa `kind` tidak bisa merilis (P0-d) |
| Penulis merilis sendiri | `e.auth.id != author` (5.2) | Bergantung pada dua akun manusia (syarat 5) |
| Identitas lewat body | `author`, `releaser` dari `e.auth` (3.2) | Tidak ada |
| Manifest diubah | Manifest ditandatangani, `bq` memakai hanya manifest (3.3, 4.1) | Tidak ada selama kunci aman |
| Kolom salinan berbeda dengan manifest | Hook menolak rilis (3.2, N7) | Tidak ada |
| Kunci privat dicuri | Keychain dengan user-presence, pencabutan (4.5) | Sampai dicabut, perilis itu bisa merilis (N5, 13.2) |
| Konfirmasi rilis jadi stempel karet | Body penuh, sha256 diketik ulang, user-presence (5.2, N8) | Perilis yang lalai tetap bisa menyetujui |
| Draft dijalankan sebagai rilis | Draft tidak pernah dari PB atau cache, `--local` ditandai UNSIGNED (5.5, N4) | Pengguna bisa salah menganggap hasil `UNSIGNED` sebagai rilis |
| Rahasia default tertanam di skrip | Skrip tidak dimigrasi (1.3, 6) | Kredensial lama tetap aktif sampai DevOps merotasi (13.4) |
| Fallback superuser di disk | Dihapus dari mesin perilis (N10, 13.4) | Sampai dihapus, berkas masih ada di mesin itu |
| Injeksi shell | Tanpa `eval`, argv terpisah (4.9) | Tidak ada |
| Tool yang sudah ditarik jalan offline | Batas 72 jam untuk `read` (4.6) | Jendela 72 jam |
| Rekursi tool | `BQ_BIN_DEPTH`, exec dibatasi (4.2) | Tidak ada |
| Race pada file sementara | Verifikasi di memori, `O_EXCL`, mode 700 (4.8) | Tidak ada |

---

## 10. Keputusan untuk manusia

Semua menunggu keputusan manusia. Maksimal lima, masing-masing dengan rekomendasi.

1. **Peran perilis dan akun pribadi.** Rekomendasi: tambah nilai `tool_releaser` ke `devops_users.role` dan minimal dua akun manusia dengan peran itu. Rilis nonaktif sampai ada. Alternatif: admin pribadi, ditolak karena melemahkan pemisahan (N6, syarat 5).
2. **Peran agent dan field `kind`.** Rekomendasi: tambah `tool_author` untuk agent, dan field `kind` (`human` atau `agent`) di `devops_users` (P0-d). Agent tidak pernah merilis. Alternatif: agent tidak membuat draft sama sekali.
3. **Kunci dan rotasi.** Rekomendasi: Ed25519 per perilis di keychain dengan user-presence, daftar pin di `bq`, pencabutan lewat rilis `bq`, dan prosedur tertulis. Jeda pencabutan untuk klien offline ikut batas 72 jam (`read`). Alternatif: tidak ada tier external sampai proxy siap.
4. **Batas offline.** Rekomendasi: 72 jam untuk `read` (fase 1). 24 jam untuk `external` ditetapkan sekarang, berlaku saat tier itu dibuka. Alternatif: 24 jam untuk `read` juga.
5. **Izin produksi dan urutan fase.** Rekomendasi: uji di PB lokal (fase 1 dan 2). Produksi hanya setelah izin tertulis dari manusia dan Security PASS. Alternatif: staging dibuat DevOps, tetapi tidak dipakai sebelum izin produksi.

---

## 11. Pertanyaan terbuka

- Konfirmasi DevOps: peran dan field `devops_users` yang ada, dan apakah `secrets` bisa dibaca akun non-admin (berpengaruh pada P0-b).
- Konfirmasi DevOps: rotasi kredensial default di 8 skrip (1.3).
- Matriks macOS yang dipakai engineer (N9).

---

## 12. Status temuan Security dan perbaikan di v0.3

| Temuan | Status | Perbaikan di v0.3 |
|---|---|---|
| KRITIS 1 (kredensial di `~/.build-q`) | Sebagian → **ditutup untuk fase 1** | Deny baca, HOME kosong, tanpa token, tanpa jaringan (4.2, 4.3, 4.4, Lampiran A). Uji 7.1 wajib |
| KRITIS 2 (penulis dan perilis) | Sebagian → **ditutup dengan syarat 5** | Fallback admin dihapus, deteksi `kind` (3.4, N6), rilis nonaktif sampai ada dua manusia (P0-c, P0-d) |
| TINGGI 3 (`cli_tools` tanpa perlindungan) | Tertutup | Manifest bertanda tangan (3.3) |
| TINGGI 4 (sha256 dan indeks satu sumber) | Tertutup di klien | Tanda tangan wajib, kunci dipin (4.5) |
| TINGGI 5 (`state` via API) | Tertutup | Klien tidak menulis langsung (3.5) |
| SEDANG 6 (rahasia di argumen) | Sebagian | Pemeriksaan pola (4.7), risiko residual (9) |
| SEDANG 7 (offline) | Tertutup | `read` 72 jam, `external` 24 jam (4.6) |
| RENDAH 8 | Sebagian | Keychain user-presence (4.5), `listRule` draft (3.5), batas 512 KB (3.2) |
| **N1** (filter host tidak bisa dipasang) | **Ditutup untuk fase 1**. Tier external dinonaktifkan (syarat 1) | Prasyarat proxy (13.1) |
| **N2** (profil sandbox belum ada) | **Ditutup di dokumen**, uji menjadi gerbang fase 2 | Lampiran A, 7.1 (syarat 2) |
| **N3** (kontradiksi tier dan kebutuhan tool) | Ditutup untuk fase 1 | Grup B dipindah ke external (bagian 6). `requires` wajib kosong di fase 1 (4.1 langkah 7) |
| **N4** (draft tanpa tanda tangan) | Ditutup | Jalur `--local` didefinisikan, tidak dari PB atau cache (5.5). Syarat 3 |
| **N5** (registri dan pencabutan kunci) | Sebagian, rencana dicatat | Registri `cli_signing_keys` dengan rule ketat (3.6). Jeda pencabutan (13.2) |
| **N6** (deteksi token bersama) | Ditutup | Field `kind` dan pemeriksaan di `bq` dan hook (3.4). Syarat 5 |
| **N7** (dua sumber manifest) | Ditutup | `bq` hanya membaca `manifest_json`. Hook menolak kolom yang tidak sama (3.2, 4.1). Syarat 4 |
| **N8** (konfirmasi rilis) | Ditutup di desain | Body penuh, sha256 diketik, user-presence (5.2) |
| **N9** (`sandbox-exec` usang) | Diterima untuk fase 1 dengan matriks | Matriks dan rencana `bwrap` (13.3) |
| **N10** (superuser dan `.env` di disk) | Rencana dicatat | Hapus dari mesin perilis (13.4) |

---

## 13. Catatan untuk N1, N3, N5, N8, N9, N10

### 13.1 Prasyarat fase external (N1, N3): **tidak dikerjakan sekarang**

- Tier `external` dinonaktifkan. Filter host di `sandbox-exec` tidak bisa dipakai.
- Rancangan yang dicatat: proxy egress di `127.0.0.1` yang dijalankan `bq`. Sandbox hanya mengizinkan koneksi ke port proxy itu. Proxy membaca daftar host dari manifest yang sudah diverifikasi dan menolak host lain. Rahasia yang diizinkan hanya dipasang di proxy, bukan di tool, jika memungkinkan.
- Syarat sebelum dibuka: proxy lulus uji (koneksi langsung gagal, host di luar daftar gagal, host dalam daftar berhasil), dan Security mereview.
- Grup B (`gh-*` dan sejenisnya) dan `requires` seperti `yq` tidak masuk sampai ini selesai. Eksekusi binari di `requires` perlu allowlist tersendiri.

### 13.2 Pencabutan kunci (N5)

- Rencana: `cli_signing_keys` ditulis hook dan route pencabutan dengan dua perilis. Pencabutan memindahkan versi yang ditandatangani kunci itu ke `yanked`.
- Jeda di klien: klien online melihat pencabutan dalam 1 jam (TTL katalog). Klien offline dibatasi oleh batas offline tier `read` (72 jam). Keputusan manusia (10.3).

### 13.3 `sandbox-exec` usang (N9)

- Fase 1 memakai `sandbox-exec` dengan matriks uji per versi macOS yang dipakai engineer. Hasil matriks dicatat sebelum fase 2.
- Sebelum fase 3: rencana paritas `bwrap` untuk Linux, dan evaluasi mekanisme pengganti di macOS.

### 13.4 Superuser, `.env`, dan kredensial default (N10, 1.3)

- Rencana: hapus `PB_API_USER`, `PB_API_PASS`, dan token superuser dari `~/.build-q/.env` di setiap mesin perilis. Bukan hanya berhenti memakainya.
- Kredensial default di 8 skrip: DevOps diminta menilai rotasi. `bq bin` tidak memigrasi skrip itu.

---

## Lampiran A. Draf profil sandbox (belum diuji; diuji di 7.1)

```scheme
(version 1)
(deny default)

;; Interpreter dan pustaka sistem yang dibutuhkan untuk menjalankan script
(allow process-exec
  (literal "/bin/bash")
  (literal "/usr/bin/python3"))
(allow file-read*
  (subpath "/usr/lib")
  (subpath "/System/Library")
  (subpath "/Library/Frameworks/Python.framework")
  (subpath "/usr/share")
  (subpath "/private/var/db/timezone"))
(allow sysctl-read)
(allow mach-lookup
  (global-name "com.apple.system.opendirectoryd.libinfo"))

;; Direktori kerja sementara: satu-satunya tempat tulis
(allow file-read* file-write*
  (subpath (param "WORKDIR")))
(allow file-read* (subpath (param "HOME")))  ; HOME = direktori kosong sementara

;; Deny eksplisit (dicek ulang di 7.1). Tidak boleh terbaca:
(deny file-read*
  (subpath (string-append (param "REALHOME") "/Library"))
  (subpath (string-append (param "REALHOME") "/.build-q"))
  (subpath (string-append (param "REALHOME") "/.claude"))
  (subpath (string-append (param "REALHOME") "/.ssh"))
  (subpath (string-append (param "REALHOME") "/.kube"))
  (subpath (string-append (param "REALHOME") "/.config"))
  (subpath (string-append (param "REALHOME") "/.agents"))
  (subpath (string-append (param "REALHOME") "/.pi")))

;; Keychain dan layanan keamanan: ditolak
(deny mach-lookup
  (global-name "com.apple.SecurityServer")
  (global-name "com.apple.securityd"))

;; Jaringan: ditolak seluruhnya di fase 1 (tier read)
(deny network*)
```

Catatan: `(param ...)` dan `string-append` perlu diuji di matriks macOS (N9). Bila tidak didukung, path ditulis langsung ke profil sebelum dijalankan. Hanya satu aturan `process-exec` (interpreter); tidak ada aturan exec lain. Perilaku `deny` terhadap `allow` yang lebih umum diuji di 7.1.

## Lampiran B. Tool `devops-bin` yang bisa jalan di fase 1

Kriteria: tidak memakai jaringan, tidak mengeksekusi `yq`, `curl`, `gh`, `kubectl`, `git`, `docker`, `ssh`, `argocd`, `helm`, `sops`, `age`, `openssl`, atau skrip lain yang memanggilnya, dan tidak membaca kredensial.

**Hasil: 2 dari 66 skrip.**

| No | Nama | Runtime | Catatan |
|---|---|---|---|
| 1 | `apigw-docs` | python | Hanya membaca partial lokal dan menulis `docs/`. Teks `--server` adalah data, bukan koneksi. |
| 2 | `kustomize-init` | bash | Hanya membaca manifest lokal dan menulis `kustomization.yaml`. Tanpa pemanggilan eksternal. |

Dikecualikan (dengan alasan):

- `apigw-check`, `apigw-live`, `image-ready`, `production-ngenwal-sls-refresh-argcd`: `curl`.
- `apigw-match`, `apigw-match-partial`, `cicd-init-web`, `devops-ci*`, `trigger-*`: `docker`, `git`, atau `ssh`.
- `pi-cost`: menghubungi API kurs eksternal dan membaca `~/.pi`.
- `source-auth`, `source_auth.py`: membaca berkas kredensial.
- `ovpn`: fungsi VPN, tidak lolos kriteria jaringan (tidak diperiksa baris demi baris).
- Semua `gh-*`, `k8s-*`, `kubectl`-based, `argocd-*`, `sops-*`, `pb-*`, `jx-*`, `cicd-*`, `ns`, `set-image`, `tag-match`, `tagging`, `svc-endpoints-audit`, `is-match*`, `check-*`, `patch-*`, `apply-file-config`, `generate-file-config-secrets`, `get-file-secret`, `match-secret`, `gitops-*`, `k8s-dump`, `k8s-secret`, dan sisanya: memakai jaringan atau exec yang dilarang.

Catatan: untuk tool `kustomize-init` dan `apigw-docs`, pemeriksaan dilakukan dengan pencarian pola. Keduanya perlu dibaca lengkap sebelum dimigrasi (uji 7.3).

Semoga dimudahkan, aamiin.
