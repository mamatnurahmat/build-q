# PRD: `bq bin` — Tool CLI terpusat di PocketBase

> **Version:** 0.2 (revisi setelah review Security FAIL v1)
> **Last Updated:** 2026-10-26
> **Ticket:** HAG-63
> **Target build-q:** v0.2.6+
> **Status:** Dokumen saja. Belum ada kode, belum ada collection, belum ada merge. Menunggu review ulang Security.
> **Ganti dari:** v0.1 (commit `2b281bd`), review: `security-engineer-mvkyygr4/hag-63-review.md`
> **CLI Entry (usulan):** `bq bin ls` · `bq bin show <cmd>` · `bq bin <cmd> <args...>` · `bq bin --dry-run <cmd> <args...>` · `bq bin publish|release|rollback|yank|deprecate`

---

## 0. Ringkasan

Tujuan tidak berubah: tool CLI tim (bash dan python) disimpan terpusat di PocketBase, dijalankan lewat `bq bin`, dan dirawat dengan versi, review, dan rollback.

Review Security v1 memberi FAIL dengan dua KRITIS dan tiga TINGGI. v2 menutupnya dengan prinsip berikut:

1. **Tool tidak pernah melihat kredensial.** Tool berjalan di sandbox OS. `~/.build-q` dan `HOME` asli tidak terbaca. Tool tidak menerima token. Hanya nilai rahasia yang dideklarasikan dan disetujui yang masuk ke env.
2. **Identitas datang dari server, bukan dari klien.** `author` dan `releaser` diisi hook dari `e.auth`. Agent tidak pernah merilis.
3. **Manifest bertanda tangan mencakup semua yang berpengaruh pada eksekusi.** Runtime, tier, env yang dibutuhkan, requires, args, dan hash body ditandatangani bersama. Kunci publik dipin di `bq`. Tanpa tanda tangan valid, tool tidak jalan.
4. **Perubahan status hanya lewat hook.** Klien tidak bisa menulis langsung ke collection tool. Semua tulisan lewat route kustom yang memvalidasi akses dan mencatat audit.
5. **Tanpa biner.** Offline: 72 jam untuk tier `read`, 24 jam untuk tier `external`.
6. **Uji dulu di PocketBase lokal.** Staging tidak ada. Produksi hanya setelah izin tertulis dari manusia.

**Menunggu keputusan manusia** (ditandai di bagian 10): akun pribadi untuk perilis, peran `tool_releaser`, peran untuk akun agent yang hanya boleh membuat draft, pengelolaan kunci penandatanganan, dan sandbox network per tier.

Tabel temuan Security ke bagian yang memperbaikinya ada di bagian 12.

---

## 1. Latar dan prasyarat

### 1.1 Yang sudah ada di build-q

| Komponen | Lokasi | Catatan untuk v2 |
|---|---|---|
| Login SSO dan token user | `build_q/pb_auth.py`, `~/.build-q/auth.json` | Token ini tidak pernah masuk ke proses tool. Gate `requires_login()` di `cli.py:main()` juga mencakup `bq bin`. |
| Ambil record | `pb_api.fetch_collection_records()` | Dipakai ulang untuk membaca katalog. |
| Ambil secret per key | `pb_api.fetch_secrets()` | Dipakai ulang, tetapi hanya nilai yang dideklarasikan tool yang diambil, dan tidak disimpan di cache (lihat P0). |
| Cache | `pb_api._write_cache()` (`.pb-cache.json`, 0600) | **Menyimpan nilai rahasia dalam teks biasa** (`pb_api.py:266-271`). Ini harus diubah sebelum fitur ini dipakai (P0). |
| Fallback superuser | `pb_api.get_token()` membaca `PB_API_USER/PASS` dari `.env` (`pb_api.py:222`) | Jalur ini tidak boleh dipakai oleh `bq bin` (P0). |
| Kebijakan risiko | `build_q/tool_policy.py` | Tier `read`, `local`, `external` dipakai ulang. |

### 1.2 Prasyarat P0 (wajib sebelum fase 1)

Ini perbaikan di luar `bq bin`, tetapi tanpanya KRITIS 1 tidak tertutup:

- **P0-a:** cache `pb_api` tidak menyimpan nilai rahasia. Bila perlu cache, simpan di keychain OS atau jangan cache sama sekali untuk jalur `bin`.
- **P0-b:** `bq bin` tidak memakai fallback superuser dari `.env`. Hanya token SSO pribadi.
- **P0-c:** setiap orang dan setiap agent punya akun PocketBase sendiri. Token bersama tidak dipakai di jalur `bq bin`. (Menunggu keputusan manusia, bagian 10.)

### 1.3 Inventaris skrip (read-only, tidak berubah dari v1)

| Sumber | Jumlah | Rincian |
|---|---|---|
| `~/.claude/skills/devops-bin/bin/` | 66 berkas | 57 bash, 9 python. |
| `~/.build-q/bin/` | 1 berkas | `docker-buildx` (biner). Tetap lokal, di luar v1. |
| `~/.agents/bin/` | tidak ada | Skill `devops-bin` masih menyebut lokasi ini. |

Ketergantungan yang perlu dipindah: 9 skrip merujuk `~/.pi/`, 10 merujuk `~/.agents/`. Skrip saling memanggil. `k8s-dump` dan `k8s-secret` memakai `yq`. Sekitar 25 skrip menyebut kata seperti TOKEN, SECRET, atau PASSWORD, dan harus diaudit.

---

## 2. UX CLI

### 2.1 Perintah

```
bq bin ls [--json] [--runtime bash|python] [--tier read|local|external]
bq bin show <cmd> [--version <v>] [--body] [--json]
bq bin [--version <v>] <cmd> [args...]          # argumen diteruskan apa adanya
bq bin --dry-run <cmd> [args...]                # tampilkan rencana, tidak menjalankan
bq bin publish <file> --name <cmd> --version <semver> --manifest <file.json> --changelog "<teks>"
bq bin release <cmd> <version>                  # hanya perilis (akun pribadi manusia), menandatangani di laptop
bq bin rollback <cmd> <version>                 # hanya perilis
bq bin yank <cmd> <version> --reason "<teks>"   # hanya perilis
bq bin deprecate <cmd> [--replacement <cmd>]    # hanya perilis
```

Aturan:

- `publish` boleh dipakai akun agent, tetapi hanya membuat draft. Hook menolak `state` selain `draft` dari klien.
- `release`, `rollback`, `yank`, dan `deprecate` memerlukan sesi pribadi manusia. `bq` menolak bila sesi berasal dari token bersama atau akun agent.
- Flag `--version` dan `--dry-run` harus muncul sebelum `<cmd>`. Semua token setelah `<cmd>` diteruskan apa adanya, tanpa diparse `bq`.
- Exit code tool diteruskan apa adanya. Galat `bq` memakai rentang 120–125: 120 tool tidak ditemukan, 121 ditarik (yanked), 122 integritas atau tanda tangan gagal, 123 offline dan cache tidak layak, 124 env wajib tidak tersedia, 125 runtime atau sandbox tidak tersedia. Galat `bq` dicetak dengan prefiks `[bq-bin]` di stderr.

### 2.2 `--dry-run`

Menampilkan, tanpa menjalankan:

- nama, versi, status, tier, runtime, dan `signer_key_id`;
- hasil verifikasi tanda tangan (OK atau gagal, dengan alasan);
- sumber: `pb`, `cache`, atau `stale-cache`;
- interpreter dan profil sandbox yang akan dipakai;
- **nama** env yang akan diinjeksi dan statusnya (tersedia atau tidak). Nilai tidak pernah ditampilkan;
- argumen sebagai literal, dan pemeriksaan pola token pada argumen (lihat 4.7);
- argv yang akan dieksekusi.

### 2.3 Contoh

```
$ bq bin --dry-run gitops-match-tag plus-be-merchantappauth-module
name: gitops-match-tag  version: 0.4.1  tier: read  runtime: python
signature: OK (key kunci-perilis-2026a)  source: pb (diperbarui 09:12)
sandbox: profil read, HOME kosong, network ditolak
env: GITHUB_TOKEN (tersedia)
argv: python3 -I <tmp>/run.py 'plus-be-merchantappauth-module'
```

---

## 3. Skema PocketBase

Nama collection diusulkan. Belum dibuat. Pembuatan hanya di PocketBase lokal (lihat bagian 8).

### 3.1 `cli_tools` (satu record per tool)

| Field | Tipe | Keterangan |
|---|---|---|
| `name` | text | Unik. Pola `^[a-z][a-z0-9-]{1,60}$`. Nama bentrok dengan perintah bq atau berkas di `~/.build-q/bin` ditolak. |
| `description` | text | Satu kalimat. |
| `owner` | relation → `devops_users` | Pemilik yang bertanggung jawab. |
| `current_version` | relation → `cli_tool_versions` | Versi aktif. Diubah hanya oleh hook. |
| `status` | select | `active`, `deprecated`, `yanked`. Diubah hanya oleh hook. |
| `replacement` | text | Nama tool pengganti bila `deprecated`. |
| `active` | bool | `false` menyembunyikan tool dari `ls`. |

Field `runtime`, `tier`, `env_required`, `requires`, dan `args_spec` **tidak ada di sini**. Semuanya ada di `cli_tool_versions` dan masuk manifest bertanda tangan (temuan TINGGI 3).

### 3.2 `cli_tool_versions` (satu record per versi, immutable setelah `released`)

| Field | Tipe | Keterangan |
|---|---|---|
| `tool` | relation → `cli_tools` | |
| `version` | text | SemVer. Unik per `tool`. |
| `state` | select | `draft`, `released`, `yanked`. Hanya hook yang mengubah. |
| `runtime` | select | `bash`, `python`. |
| `tier` | select | `read`, `local`, `external`. |
| `env_required` | json | Daftar nama env yang boleh diinjeksi. Hanya nama. |
| `requires` | json | Biner pendukung, mis. `["yq"]`. |
| `args_spec` | json | Dokumentasi argumen (dan pemeriksaan pola token, lihat 4.7). |
| `body` | file (maks 512 KB) | Isi script. |
| `sha256` | text | Hash `body`, dihitung server. |
| `manifest_json` | json | Manifest kanonik yang ditandatangani (3.3). |
| `signature` | text | Ed25519, base64. Wajib untuk `released`. |
| `signer_key_id` | text | Id kunci publik yang dipakai, lihat 4.5. |
| `changelog` | text | Wajib. |
| `author` | relation → `devops_users` | **Diisi hook dari `e.auth.id`.** Nilai dari body diabaikan. |
| `releaser` | relation → `devops_users` | **Diisi hook dari `e.auth.id`.** Nilai dari body diabaikan. |
| `released_at` | date | Diisi hook. |
| `yanked_reason` | text | Wajib bila `yanked`. |

Aturan immutable: setelah `released`, field `body`, `sha256`, `manifest_json`, `signature`, `signer_key_id`, `runtime`, `tier`, `env_required`, `requires`, `args_spec`, dan `version` tidak bisa diubah. Hanya `state` (ke `yanked`) dan `yanked_reason` yang boleh diubah, dan hanya oleh hook rilis. Hapus record tidak diizinkan.

### 3.3 Manifest bertanda tangan

`manifest_json` adalah JSON kanonik (kunci urut, tanpa spasi, UTF-8). Yang ditandatangani adalah seluruh manifest:

```json
{
  "schema": 1,
  "name": "gitops-match-tag",
  "version": "0.4.1",
  "runtime": "python",
  "tier": "read",
  "env_required": ["GITHUB_TOKEN"],
  "requires": [],
  "args_spec": {"usage": "gitops-match-tag <repo> [--json]", "max_args": 4},
  "sha256": "9f2c...",
  "released_at": "2026-10-26T10:00:00Z",
  "releaser": "<id PB perilis>",
  "author": "<id PB penulis>"
}
```

Verifikasi tanda tangan dilakukan dua kali: di `bq` (sebelum menjalankan, dengan kunci publik yang dipin di `bq`) dan di hook rilis (dengan kunci publik perilis yang terdaftar). Keduanya memakai byte manifest yang sama.

### 3.4 Akses per peran

Peran yang ada di `devops_users.role` (sesuai schema produksi): `view`, `admin`, `incident_support`, `incident_owner`. Peran untuk tool belum ada.

| Aksi | `view` | `incident_*` | `admin` (sesi pribadi) | `tool_releaser` (**menunggu keputusan manusia**) | Akun agent (**menunggu keputusan manusia**) |
|---|---|---|---|---|---|
| `ls`, `show`, jalankan `released` dan `active` | ya | ya | ya | ya | ya |
| Baca `body` draft | tidak | tidak | ya | ya (bukan penulis) | penulisnya saja |
| Buat draft | tidak | tidak | ya | ya | ya, **hanya draft** |
| `release`, `rollback`, `yank`, `deprecate` | tidak | tidak | ya, bukan penulis draft | ya, bukan penulis draft | tidak |
| Ubah `state` langsung lewat API | tidak | tidak | tidak | tidak | tidak |

Catatan:
- Sampai `tool_releaser` ada dan ada akun pribadi, rilis dinonaktifkan. Tidak ada jalur rilis lewat token bersama.
- `admin` pada tabel ini berarti akun admin pribadi milik manusia. Token admin bersama tidak masuk tabel.
- Pemeriksaan peran dilakukan di hook dengan `e.auth.role`, bukan hanya di rule.

### 3.5 Klien tidak menulis langsung

- `createRule`, `updateRule`, dan `deleteRule` untuk `cli_tools`, `cli_tool_versions`, dan `cli_tool_audit` adalah kosong (tidak bisa diakses klien).
- Penulisan lewat route kustom (`/api/bq/bin/publish`, `/api/bq/bin/release`, `/api/bq/bin/rollback`, `/api/bq/bin/yank`, `/api/bq/bin/deprecate`). Setiap route:
  1. memeriksa `e.auth` dan peran;
  2. mengabaikan `author`, `releaser`, `state`, `released_at` dari body;
  3. memvalidasi manifest, sha256, ukuran, dan (untuk rilis) tanda tangan;
  4. menulis dengan `$app` dan menambah baris `cli_tool_audit`.
- `listRule`: `cli_tools` `active = true` untuk pengguna terautentikasi. `cli_tool_versions` `state = "released"`, atau draft untuk penulis dan perilis. Body draft tidak bisa dibaca oleh peran lain.

### 3.6 `cli_tool_audit` (ditulis hook saja)

Setiap publish, release, rollback, yank, deprecate, dan penolakan tercatat: `action`, `tool`, `version`, `actor` (dari `e.auth.id`), `actor_role`, `result` (ok, atau alasan tolak), `ip`, `at`. Tidak bisa diubah dan tidak bisa dihapus.

---

## 4. Eksekusi

### 4.1 Alur `bq bin <cmd> args...`

1. Cek login pribadi (`requires_login`). Token bersama atau fallback superuser ditolak (P0-b).
2. Muat indeks lokal `~/.build-q/bin/tools/index.json`. Indeks hanya berisi nama, versi aktif, sha256, id kunci, dan waktu ambil. Tidak ada nilai rahasia.
3. Ambil katalog dari PB bila indeks lebih tua dari TTL katalog (1 jam). Gagal ambil: lanjut ke 4.6.
4. Tentukan versi dan cek status: `yanked` ditolak (121), `deprecated` jalan dengan peringatan.
5. Ambil `manifest.json` dan `body` dari cache atau PB.
6. **Verifikasi:** hitung sha256 body dan bandingkan dengan manifest. Verifikasi tanda tangan manifest dengan kunci publik yang dipin di `bq` (4.5). Gagal di salah satunya: exit 122, cache entri dihapus. Verifikasi ulang dilakukan setiap jalan, termasuk dari cache.
7. Tentukan env: hanya nama dalam `env_required` dari manifest yang sudah diverifikasi. Nilai diambil oleh `bq` dari PB (P0-a, tanpa cache), hanya untuk nama itu. Nama yang tidak ada: exit 124, nama ditampilkan, nilai tidak.
8. Siapkan sandbox (4.2): direktori kerja sementara `~/.build-q/run/<random>` mode 700, `HOME` baru dan kosong di dalamnya.
9. Tulis body ke `run.<ext>` mode 700 dari byte yang sudah diverifikasi di memori, lalu jalankan di bawah sandbox.
10. Hapus direktori kerja di `finally`, termasuk saat Ctrl-C (signal diteruskan ke proses anak).
11. Exit dengan exit code anak.

### 4.2 Sandbox OS untuk proses tool (KRITIS 1)

Setiap proses tool dijalankan dengan `sandbox-exec` (macOS) memakai profil berikut:

- `(deny default)`. Yang diizinkan hanya:
  - baca dan eksekusi interpreter (`/bin/bash`, `python3` dari path sistem) dan pustaka sistem yang diperlukan;
  - baca dan tulis di direktori kerja sementara;
  - baca dan tulis di pipe stdin, stdout, stderr.
- **Tidak boleh dibaca:** `~/.build-q`, `~/.claude`, `~/.ssh`, `~/.kube`, `~/.config`, `~/.agents`, `~/.pi`, dan seluruh `HOME` asli. `HOME` yang dilihat tool adalah direktori kosong sementara.
- **Eksekusi:** hanya interpreter yang sudah ditentukan. `exec` proses lain ditolak, sehingga tool tidak bisa memanggil `bq` lagi (rekursi juga ditolak lewat `BQ_BIN_DEPTH`).
- **Network:** tier `read` ditolak seluruhnya. Tier `external` ditolak kecuali ke host yang didaftarkan di manifest (keputusan manusia, bagian 10, butir 5).
- Bila `sandbox-exec` tidak ada atau gagal dimuat, `bq` menolak menjalankan tool (exit 125). Tidak ada jalur tanpa sandbox.
- Catatan: `sandbox-exec` sudah ditandai usang oleh Apple, tetapi masih ada di macOS saat ini. Rencana jangka panjang: pindah ke mekanisme yang didukung, dan Linux memakai `bwrap`. Dicatat sebagai risiko residual (bagian 9).

Tes wajib (bagian 7): tool gema yang mencoba membaca `~/.build-q/auth.json` harus gagal. Tool yang mencoba `curl` ke host di luar daftar harus gagal.

### 4.3 Tool tidak menerima token

- Env proses anak dibangun dari kosong. Yang masuk: `PATH` minimal (`/usr/bin:/bin:/usr/sbin:/sbin`), `LANG`, `HOME` sementara, `BQ_BIN_DEPTH=1`, dan nilai env yang dideklarasikan.
- Tidak ada `GITHUB_TOKEN`, `PB_*`, `auth.json`, atau token apa pun yang tidak dideklarasikan.
- Tool tidak bisa memanggil PB. Tool yang butuh data dari PB harus melewati argumen atau env yang sudah disiapkan `bq`.
- Tool yang menerima rahasia tetap bisa membocorkannya lewat network yang diizinkan. Risiko ini dicatat di bagian 9 dan dibatasi oleh tinjauan isi saat rilis (env yang dideklarasikan ada di manifest bertanda tangan) dan oleh aturan network per tier.

### 4.4 Cache tanpa nilai rahasia (KRITIS 1)

- `~/.build-q/bin/tools/index.json`: nama, versi, sha256, `signer_key_id`, `fetched_at`.
- `~/.build-q/bin/tools/<name>/<version>/manifest.json` dan `body`, mode 600 di dalam direktori mode 700.
- Tidak ada nilai rahasia di cache, termasuk di `.pb-cache.json` (P0-a). Nilai rahasia hanya di memori, selama satu proses.
- Cache hanya berisi versi `released`. Versi `yanked` dihapus dari cache saat terdeteksi.

### 4.5 Kunci penandatanganan (TINGGI 4)

- **Kunci privat:** satu pasang Ed25519 per perilis manusia. Kunci privat disimpan di keychain OS (macOS Keychain) atau perangkat keras. Tidak pernah di PB, tidak di repo, tidak di `~/.build-q` sebagai berkas teks.
- **Kunci publik:** daftar kunci publik perilis dipin di `bq` (berkas `build_q/keys/bin-release-keys.json` dengan `key_id`, nama perilis, kunci publik, dan `valid_from`/`revoked_at`). Mengubah daftar ini berarti rilis `bq`.
- **Penandatanganan:** hanya di laptop perilis, saat `bq bin release`. Server memverifikasi ulang dengan kunci publik yang terdaftar di hook, dan menolak bila tidak cocok.
- **Tanpa tanda tangan valid, tool tidak jalan.** Tidak ada mode opsional.
- **Rotasi dan pencabutan:** `revoked_at` mencabut kunci. Versi yang ditandatangani kunci yang dicabut berubah ke `yanked` lewat hook. Prosedur rotasi perlu keputusan manusia (bagian 10).

### 4.6 Perilaku offline (SEDANG 7)

| Kondisi | Tier `read` | Tier `external` |
|---|---|---|
| PB terjangkau | ambil katalog, jalan | ambil katalog, jalan |
| PB tidak terjangkau, cache < 72 jam (read) atau < 24 jam (external) | jalan dari cache, peringatan `stale-cache` | sama |
| PB tidak terjangkau, cache lebih tua | exit 123 | exit 123 |
| PB tidak terjangkau, tanpa cache | exit 123 | exit 123 |

Tier diambil dari manifest yang sudah diverifikasi, bukan dari indeks. Tier `external` tidak punya jendela offline lebih dari 24 jam, karena yank tidak bisa diketahui selama offline.

### 4.7 Pemeriksaan argumen dan token (SEDANG 6)

- `--dry-run` dan eksekusi menolak argumen yang cocok pola token umum (mis. `ghp_`, `xox`, `AKIA`, `eyJ` yang panjang, atau nama flag `--token`, `--password`, `--secret`). Pola daftar di `bq`, bukan di PB.
- Penolakan ini sekadar pagar. Pengguna tetap harus memberi rahasia lewat env. Argumen tetap terlihat di `ps` bila lolos pemeriksaan.

### 4.8 Verifikasi integritas dan file sementara

- Byte body diverifikasi di memori, lalu ditulis ke file baru (`O_EXCL`, mode 700) dalam direktori mode 700. Tidak ada jendela baca ulang.
- Sandbox melindungi dari proses lain di akun yang sama yang mencoba membaca direktori `bq`. Proses lain yang bisa menulis ke `run/` di dalam sandbox tidak bisa memengaruhi isi yang sudah diverifikasi di memori.

### 4.9 Kenapa tidak `eval`

Body tidak pernah dikonkatenasi ke shell. Tool dijalankan sebagai berkas dengan interpreter tetap. Argumen masuk sebagai argv. Tes wajib membuktikan `$(...)`, backtick, dan `;` tampil literal.

---

## 5. Alur maintenance

### 5.1 Publish (draft)

1. Penulis (akun pribadi atau akun agent, bila disetujui) memanggil `bq bin publish`. Route membuat `cli_tools` (jika baru) dan `cli_tool_versions` dengan `state = draft`. Hook mengisi `author` dari `e.auth.id`.
2. Draft tidak bisa dijalankan oleh siapa pun kecuali penulis untuk uji lokal (`bq bin --draft`, lihat 5.5).
3. Penulis membuka tiket atau PR untuk review isi.

### 5.2 Release (hanya perilis manusia)

1. Perilis dengan sesi pribadi menjalankan `bq bin release <cmd> <versi>`.
2. `bq` mengambil draft dan menampilkan manifest yang akan ditandatangani, termasuk diff body dan daftar env dan network.
3. Perilis mengonfirmasi. `bq` menandatangani manifest dengan kunci keychain perilis.
4. `bq` mengirim tanda tangan ke route release.
5. Hook memeriksa: `e.auth.role` adalah `tool_releaser` (atau `admin` pribadi, selama keputusan belum ada), `e.auth.id != author`, sha256 cocok, tanda tangan valid dengan kunci terdaftar milik `e.auth.id`, dan state adalah `draft`.
6. Hook menulis `state = released`, `releaser = e.auth.id`, dan `released_at`, lalu menulis audit.

Hanya hook yang mengubah state ke `released` (TINGGI 5).

### 5.3 Update = versi baru

Versi lama tetap `released` dan bisa dijalankan dengan `--version`.

### 5.4 Rollback dan yank

- `rollback`: target harus `released` dan tidak `yanked`. Hook memindahkan `current_version`. Tanda tangan target sudah ada, jadi tidak perlu tanda tangan baru.
- `yank`: hook mengubah `state` ke `yanked`, mewajibkan alasan, dan memindahkan `current_version` ke versi `released` terbaru yang tidak di-yank. Bila tidak ada, `status` tool menjadi `deprecated` tanpa versi aktif.
- Yank berlaku pada klien saat indeks diperbarui. Pada tier `external`, jendela 24 jam menjadi batas paparan.

### 5.5 Uji draft lokal

`bq bin --draft <cmd>` menjalankan draft milik penulis yang sedang login, di sandbox yang sama. Tidak bisa dipakai untuk versi `released`. Peringatan selalu tercetak. Draft tidak masuk cache.

### 5.6 Deprecate

Perilis mengubah `status` ke `deprecated` dengan `replacement`. Tool tetap jalan dengan peringatan.

---

## 6. Inventaris dan migrasi

| Kelompok | Contoh | Risiko | Fase |
|---|---|---|---|
| A. Baca saja, tanpa rahasia | `k8s-latest-tag`, `check-pod-ip`, `svc-endpoints-audit` (mode read) | read | Fase 3 |
| B. Baca dengan token (GitHub, Docker) | `gh-latest-tag`, `gh-check-lang`, `tag-match`, `gitops-match-gh-latest-tag`, `image-ready` | read, open-world | Fase 3, setelah P0 dan network per tier |
| C. Ubah cluster atau GitOps | `set-image`, `k8s-scale-active-deploys`, `gitops-audit --apply-*`, `gh-k8s-dump`, `apply-file-config` | external | Fase 4, review per tool |
| D. Rahasia atau SOPS | `k8s-secret`, `sops-*`, `source_auth.py`, `gh-fetch-secret`, `get-file-secret`, `argocd-app token-set` | sensitif | Tetap lokal. Tidak dimigrasi di v2 |
| E. Bootstrap dan init | `cicd-init*`, `k8s-init-*`, `init-master`, `bootstrap-app` | external | Fase 4, bila ada kebutuhan |
| F. Biner | `docker-buildx` | — | Tetap lokal |

Langkah migrasi per skrip: hapus path mesin (`~/.pi`, `~/.agents`), ganti rahasia dari file dengan env yang dideklarasikan, ganti panggilan ke skrip lain dengan `bq bin`, isi `args_spec`, `env_required`, `requires`, `runtime`, `tier` di manifest, jalankan `shellcheck` atau `ruff`, dan publish sebagai draft. Salinan di `devops-bin` dihapus hanya setelah dua siklus rilis berjalan tanpa masalah.

---

## 7. Rencana tes

Uji di **PocketBase lokal** (bukan staging, karena staging belum ada; instance lokal dari DevOps di port 8193 ke atas, bukan 8192 kecuali DevOps menyetujui). Data uji berawalan `TEST-` atau `UJI-BIN-` dan dihapus oleh DevOps. Tidak ada uji di produksi.

Keamanan (wajib lulus sebelum fase 2):

- Tool gema yang membaca `~/.build-q/auth.json` gagal di sandbox.
- Tool gema yang membaca `~/.build-q/.pb-cache.json` atau `.env` gagal.
- Env anak tidak berisi `GITHUB_TOKEN`, `PB_*`, atau nilai apa pun yang tidak dideklarasikan (diperiksa dengan `env` di dalam tool).
- `curl` ke host di luar daftar gagal (tier `read` semua gagal).
- Cache tidak memuat nilai rahasia (`grep` nilai uji di seluruh `~/.build-q` setelah jalan: nol).
- Manifest diubah (mis. `env_required` ditambah `SECRET_X`) tanpa tanda tangan baru: ditolak, exit 122.
- Body diubah tanpa mengubah sha256: ditolak.
- Tanda tangan dengan kunci yang dicabut: ditolak.
- `state` diubah ke `released` lewat API langsung oleh penulis: 403.
- Penulis merilis draft miliknya sendiri, atau mengirim `releaser` atau `author` di body: ditolak atau diabaikan (nilai dari `e.auth`).
- Token bersama di jalur `release`: ditolak oleh `bq`.
- Akun agent mencoba `release`: ditolak.
- `--dry-run` tidak menjalankan tool (tool menulis penanda, penanda tidak ada) dan tidak menampilkan nilai env.
- Argumen `$(...)`, backtick, `;`, dan token uji (`ghp_...`) diperlakukan literal, dan token ditolak oleh pemeriksaan pola.
- Rekursi: tool yang memanggil `bq bin` ditolak.
- Offline: tier `read` jalan sampai 72 jam, `external` sampai 24 jam, lebih dari itu exit 123.
- Biner tidak bisa dipublish (ditolak oleh route).

Fungsional:

- Publish → draft → release (perilis berbeda) → jalankan → rollback → yank.
- Exit code tool `0`, `1`, `42`, `130` diteruskan.
- Mode file dan direktori 700/600.
- File sementara terhapus saat sukses, gagal, dan Ctrl-C.

---

## 8. Fase rollout

| Fase | Isi | Gerbang keluar |
|---|---|---|
| 0. P0 dan prasyarat | P0-a, P0-b, P0-c (akun pribadi dan akun agent), peran (`tool_releaser`, peran agent), kunci perilis di keychain. Semua menunggu keputusan manusia bila belum disetujui | P0 selesai, Security setuju |
| 1. PocketBase lokal | Schema, hook, route, `cli_tool_audit`, uji keamanan bagian 7 | Semua uji keamanan lulus, Security review ulang |
| 2. CLI baca dan draft | `ls`, `show`, `--dry-run`, eksekusi `released` di sandbox, `publish`, `--draft` | Tes fungsional lulus |
| 3. Rilis | `release`, `rollback`, `yank`, `deprecate` dengan dua perilis berbeda di PB lokal. Migrasi A dan B | Dua orang sudah mencoba alur, tidak ada insiden |
| 4. Kelompok C dan E | Migrasi aksi yang mengubah cluster dan repo, dengan review per tool | Review DevOps dan Security per tool |
| 5. Produksi | Pembuatan collection dan route di produksi **hanya** setelah izin tertulis dari manusia, dan Security PASS | Izin tertulis |

---

## 9. Threat model (v2)

Aset: laptop perilis dan engineer, kredensial di `~/.build-q`, token PB, cluster dan repo yang bisa diubah tool.

| Ancaman | Kontrol v2 | Risiko residual |
|---|---|---|
| Tool jahat membaca kredensial dari `~/.build-q` | Sandbox OS, `HOME` kosong, tanpa token (4.2, 4.3) | Sandbox macOS sudah usang. Perlu rencana pindah ke mekanisme yang didukung |
| Tool jahat membawa rahasia yang dideklarasikan keluar lewat network | Network ditolak untuk `read`. `external` hanya host terdaftar (4.2) | Rahasia yang dideklarasikan tetap bisa bocor ke host terdaftar. Ditinjau saat rilis |
| Penulis atau pihak tak berhak mengubah `released` lewat API | Klien tidak bisa menulis langsung (3.5). State hanya via hook (5.2) | Akun admin yang dibajak tetap bisa merilis. Diatasi dengan kunci perilis dan audit |
| Penulis merilis sendiri | `e.auth.id != author` di hook (5.2) | Bergantung pada akun pribadi (P0-c). Token bersama tidak boleh dipakai |
| Identitas dipalsukan lewat body | `author`, `releaser` dari `e.auth` (3.2, 3.5) | Tidak ada |
| Manifest diubah setelah rilis (mis. env ditambah) | Manifest ditandatangani utuh (3.3), verifikasi di `bq` dan hook (4.1 langkah 6) | Tidak ada selama kunci aman |
| Body dan indeks diubah bersama di PB | Tanda tangan wajib dan dipin di `bq`, bukan dari PB (4.5) | Kunci perilis yang bocor. Dicabut dengan `revoked_at` |
| Kunci privat perilis dicuri | Keychain atau perangkat keras, pencabutan (4.5) | Sampai dicabut, perilis itu bisa merilis. Dicatat di audit |
| Injeksi shell lewat argumen | Tanpa `eval`, argv terpisah (4.9) | Tidak ada |
| Rahasia di cache, log, atau argumen | Cache tanpa nilai (4.4), dry-run hanya nama, pemeriksaan pola token (4.7) | Argumen tetap terlihat di `ps` bila lolos pemeriksaan |
| Tool yang sudah ditarik masih jalan offline | Batas 72 jam (read) dan 24 jam (external) (4.6) | Jendela 72 jam untuk read |
| Rekursi tool ke `bq bin` | `BQ_BIN_DEPTH` dan `exec` ditolak (4.2) | Tidak ada |
| Race pada file sementara | Byte diverifikasi di memori, `O_EXCL`, mode 700 (4.8) | Penyerang di dalam sandbox tidak bisa mengubah isi yang sudah diverifikasi |
| Biner tidak terverifikasi | Tidak ada biner di v2 | Tidak ada |
| Peningkatan hak lewat peran | Pemeriksaan di hook dengan `e.auth.role` (3.4) | Peran belum ada (menunggu keputusan manusia) |

---

## 10. Keputusan untuk manusia

Keputusan ini **menunggu keputusan manusia**. Masing-masing dengan rekomendasi. Usulan Boss dicatat sebagai pilihan yang sedang ditanyakan.

1. **Akun dan peran perilis.** Rekomendasi: setiap perilis punya akun PocketBase pribadi dengan peran baru `tool_releaser` (menambah nilai ke `devops_users.role`, perubahan skema produksi, butuh izin). Sampai itu ada, rilis dinonaktifkan. Alternatif: admin pribadi saja, lebih cepat tetapi lebih lemah.
2. **Akun agent.** Rekomendasi: agent punya akun sendiri dengan peran draft-only (`tool_author`, perubahan skema juga). Agent tidak pernah merilis. Alternatif: agent tidak membuat draft sama sekali, dan manusia yang mempublish.
3. **Kunci penandatanganan.** Rekomendasi: satu pasang Ed25519 per perilis di keychain atau perangkat keras. Kunci publik dipin di `bq` (`build_q/keys/bin-release-keys.json`), dan rotasi atau pencabutan lewat rilis `bq`. Perlu prosedur tertulis untuk rotasi dan pencabutan.
4. **Batas offline dan network.** Rekomendasi: offline 72 jam untuk `read`, 24 jam untuk `external`. Network: `read` ditolak seluruhnya, `external` hanya host terdaftar di manifest. Alternatif: tidak ada network sama sekali untuk `external`, sehingga tool yang butuh API eksternal tidak bisa jalan lewat `bq bin`.
5. **Sandbox dan izin produksi.** Rekomendasi: pakai `sandbox-exec` sekarang sebagai jalan awal, dengan rencana tertulis pindah ke mekanisme yang didukung. Gagal menutup sandbox berarti tool tidak jalan (exit 125), tidak ada jalur tanpa sandbox. Produksi (collection dan route) hanya setelah izin tertulis dan Security PASS.

---

## 11. Pertanyaan terbuka

- Peran dan field di `devops_users` yang menentukan siapa perilis. Akses RnD hanya bisa membaca record (schema 403), jadi konfirmasi DevOps diperlukan.
- Apakah `secrets` bisa dibaca oleh akun non-admin. Berpengaruh pada P0-b.
- Format dan lokasi pemasangan kunci publik di `bq` untuk rilis awal.
- Apakah tool yang jalan di CI perlu jalur yang sama. Di luar cakupan.
- Revisi besar lanjutan menunggu review ulang Security.

## 12. Temuan Security → perbaikan

| Temuan (review v1) | Perbaikan di v2 |
|---|---|
| KRITIS 1: tool membaca `~/.build-q` (auth, cache rahasia, `.env`) | Sandbox OS dan `HOME` kosong (4.2); tool tidak menerima token (4.3); cache tanpa nilai rahasia (4.4); P0-a, P0-b |
| KRITIS 2: pemisahan penulis dan perilis bisa dilewati; token admin bersama | Identitas dari `e.auth` di hook (3.2, 3.5, 5.2); klien tidak menulis langsung (3.5); rilis hanya akun pribadi manusia, token bersama ditolak (2.1, 3.4, P0-c); peran `tool_releaser` menunggu keputusan (10.1) |
| TINGGI 3: `env_required`, `tier`, `requires`, `runtime` di `cli_tools` tanpa perlindungan | Dipindah ke `cli_tool_versions` (3.1, 3.2); masuk manifest bertanda tangan (3.3); verifikasi ulang tiap jalan (4.1 langkah 6) |
| TINGGI 4: sha256 dan indeks dari sumber yang sama | Tanda tangan Ed25519 wajib atas manifest, kunci publik dipin di `bq`, kunci privat di keychain (4.5); tanpa tanda tangan valid, tool tidak jalan |
| TINGGI 5: penulis bisa mengubah `state` ke `released` lewat API | Klien tidak bisa menulis (3.5); state hanya lewat route dan hook (5.2, 5.4) |
| SEDANG 6: rahasia di argumen dan `ps` | Pemeriksaan pola token di `--dry-run` dan eksekusi (4.7); dicatat sebagai risiko residual (9) |
| SEDANG 7: offline 72 jam untuk tier external | `read` 72 jam, `external` 24 jam (4.6); keputusan 4 di bagian 10 |
| RENDAH 8: kunci privat di laptop, draft bisa dibaca, batas ukuran | Keychain atau perangkat keras dan pencabutan (4.5, 9); `listRule` draft hanya penulis dan perilis (3.5); batas 512 KB (3.2) |
| Pendapat atas keputusan 1 sampai 5 | Keputusan 1 (tanda tangan wajib), 2 (tanpa biner), 3 (dua orang dengan akun pribadi), 4 (offline dengan syarat), 5 (staging dulu, izin tertulis) dimasukkan di bagian 10 |
| Pertanyaan (a) sampai (c) | (a) tanda tangan dan sandbox (4.2, 4.5); (b) audit di collection terpisah yang ditulis hook (3.6); (c) 72 jam hanya untuk `read` (4.6) |

Semoga dimudahkan, aamiin.
