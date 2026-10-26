# PRD: `bq bin` — Tool CLI terpusat di PocketBase

> **Version:** 0.1 (draft untuk review)
> **Last Updated:** 2026-10-26
> **Ticket:** HAG-63
> **Target build-q:** v0.2.6+
> **Status:** Plan saja. Belum ada kode implementasi, belum ada collection di produksi.
> **CLI Entry (usulan):** `bq bin ls` · `bq bin show <cmd>` · `bq bin <cmd> <args...>` · `bq bin <cmd> --dry-run` · `bq bin publish|release|rollback|deprecate`

---

## 0. Ringkasan dan keputusan kunci

Tujuan: semua tool CLI tim (bash, python, dan kelak biner) disimpan terpusat di PocketBase, dijalankan lewat `bq bin <cmd> <args...>`, dan dirawat dengan versi, review, dan rollback.

Keputusan kunci (detail di bagian 9):

1. **Eksekusi kode dari database adalah risiko terbesar.** Siapa pun yang bisa menulis versi tool yang dirilis bisa menjalankan kode di laptop semua engineer. Karena itu rilis wajib dua orang (penulis ≠ perilis), dan isi tool wajib ditandatangani di luar PocketBase (rekomendasi, lihat keputusan 1).
2. **Isi tool tidak pernah dievaluasi shell.** Tool dijalankan sebagai file sementara dengan interpreter tetap (`/bin/bash` atau `python3 -I`), argumen diteruskan sebagai daftar argv, bukan string.
3. **Rahasia tidak pernah disimpan di collection tool.** Tool hanya mendeklarasikan nama env yang dibutuhkan; nilainya diambil dari collection `secrets` yang sudah ada, lewat `pb_api`, dan hanya dimasukkan ke env proses anak.
4. **Versi bersifat immutable setelah rilis.** Perubahan isi berarti versi baru. Rollback berarti memindahkan penunjuk versi aktif ke versi rilis sebelumnya.
5. **Versi biner tidak masuk v1.** Biner `docker-buildx` (65 MB) yang sudah ada di `~/.build-q/bin` tetap lokal.
6. **Tidak ada perubahan skema produksi tanpa izin manusia.** Collection hanya dibuat di staging oleh DevOps lebih dulu.

---

## 1. Latar dan kondisi saat ini

### 1.1 Yang sudah ada di build-q

| Komponen | Lokasi | Catatan untuk fitur ini |
|---|---|---|
| Login SSO dan token user | `build_q/pb_auth.py`, `~/.build-q/auth.json` | Dipakai ulang lewat `pb_api.get_token()`. Gate login `requires_login()` di `cli.py:main()` harus juga mencakup `bq bin`. |
| Ambil data collection umum | `build_q/pb_api.py: fetch_collection_records()` | Dipakai ulang untuk `cli_tools`. Sudah menangani auth dan HTTP error (`PBAPIError`). |
| Ambil secret per key | `pb_api.fetch_secrets()`, `pb_api.hydrate_env()` | Dipakai ulang untuk env tool. Hanya key yang dideklarasikan tool yang diinjeksi. |
| Cache dengan TTL | `pb_api._write_cache()` (0600), `tui_catalog.py` (TTL 1 jam) | Pola cache dipakai ulang. Cache tool memakai direktori sendiri (lihat bagian 4). |
| Katalog TUI | collection `build_q_tools`, `build_q_providers`, `build_q_patterns` | Ini katalog perintah bq untuk TUI, bukan isi script. Tidak diubah. Collection baru `cli_tools` dipisah. |
| Kebijakan risiko | `build_q/tool_policy.py` (tier read/local/external) | Tool `bin` memakai tier yang sama: setiap tool punya field `tier`. |

Catatan: `bq` wajib login (`requires_login`). Akses per peran di bagian 3.4 harus dikonfirmasi DevOps, karena akun RnD tidak bisa membaca schema (`GET /api/collections/<nama>` mengembalikan 403). Yang terbaca hanya record; untuk katalog, hanya nama field yang diperiksa (isi tidak dicatat).

### 1.2 Inventaris skrip yang ada (read-only)

| Sumber | Jumlah | Rincian |
|---|---|---|
| `~/.claude/skills/devops-bin/bin/` | 66 berkas | 57 bash, 9 python. Ada `source_auth.py`, `k8s-dump`, `k8s-secret`, `pb-seed*`, `cicd-*`, `argocd-app`, `gitops-audit`, dan lainnya. |
| `~/.build-q/bin/` | 1 berkas | `docker-buildx` (biner, 65 MB). Tetap lokal. |
| `~/.agents/bin/` | tidak ada di mesin ini | Skill `devops-bin` masih menyebut lokasi ini, jadi katalog skill sudah tidak sesuai dengan kondisi mesin. |

Temuan yang memengaruhi migrasi:

- **Ketergantungan path mesin:** 9 skrip merujuk `~/.pi/` dan 10 merujuk `~/.agents/`. Path ini harus diganti dengan mekanisme `bq auth get` atau env dari `secrets`.
- **Ketergantungan antar-skrip:** skrip saling memanggil (mis. `gitops-match-gh-latest-tag` memanggil `gh-latest-tag`). Migrasi harus mengikuti urutan dependensi, dan pemanggilan ke skrip lain diganti menjadi `bq bin <cmd>`.
- **Pemakaian `yq`:** `k8s-dump` dan `k8s-secret` memakai `yq eval`. Tool harus mendeklarasikan `requires` (biner pendukung) dan `bq` memeriksanya saat `--dry-run`.
- **Skrip yang menyentuh rahasia:** `k8s-secret`, `sops-*`, `source_auth.py`, `gh-fetch-secret`. Kategori ini tidak dimigrasi pada fase awal (lihat bagian 8).

---

## 2. UX CLI

### 2.1 Perintah

```
bq bin ls [--json] [--runtime bash|python] [--tier read|local|external]
bq bin show <cmd> [--version <v>] [--body] [--json]
bq bin <cmd> [--version <v>] [args...]       # argumen diteruskan apa adanya; --version hanya bila sebelum args
bq bin --dry-run <cmd> [args...]             # tampilkan rencana, tidak menjalankan
bq bin publish <file> --name <cmd> --runtime bash|python --version <semver> --changelog "<teks>" [--args-spec <file.json>] [--env NAME ...] [--tier ...] [--description "<teks>"]
bq bin release <cmd> <version>               # draft → released (perlu peran perilis, bukan penulis)
bq bin rollback <cmd> <version>              # pindahkan penunjuk aktif ke versi released sebelumnya
bq bin deprecate <cmd> [--replacement <cmd>] # status deprecated, tetap bisa dijalankan dengan peringatan
bq bin yank <cmd> <version>                  # tarik versi karena masalah keamanan; tidak bisa dijalankan
```

### 2.2 Aturan argumen

- Semua token setelah `bq bin <cmd>` diteruskan ke tool **tanpa diparse** oleh argparse `bq`. Penangkapan dilakukan di `main()` sebelum argparse, seperti `bq auth` (`cli.py:126`). Dengan begitu, `--dry-run` hanya dikenali bila muncul **sebelum** `<cmd>`, atau dengan tanda `--bq-dry-run`. Usulan: flag `bq` selalu didahulukan, contoh `bq bin --dry-run <cmd> ...`. Ini mencegah bentrok dengan flag milik tool (mis. tool yang punya `--dry-run` sendiri).
- Exit code tool diteruskan apa adanya sebagai exit code `bq`.
- Galat dari `bq` sendiri memakai rentang exit yang tidak dipakai tool: `120` tool tidak ditemukan, `121` tool ditarik (yanked), `122` integritas gagal, `123` PB tidak terjangkau dan cache tidak layak pakai, `124` env wajib tidak tersedia, `125` runtime tidak didukung. Kode 126 dan 127 mengikuti shell.
- Keterbatasan: tool yang sendiri mengembalikan 120–125 akan tampak sama. Opsi mitigasi: `bq bin` mencetak prefiks `[bq-bin]` di stderr setiap kali exit code berasal dari `bq`. Ini diputuskan saat implementasi (lihat keputusan 5 pada bagian 10).

### 2.3 `--dry-run`

Menampilkan tanpa menjalankan:

- nama, versi, status, sha256 (16 karakter pertama dan panjang penuh dengan `--json`);
- sumber: `pb` (baru diambil), `cache`, atau `stale-cache` (offline);
- runtime dan interpreter yang akan dipakai (`/bin/bash` atau `python3 -I`), termasuk versi yang terdeteksi;
- daftar **nama** env yang akan diinjeksi (`GITHUB_TOKEN` dst.), status tersedia atau tidak. Nilainya **tidak** pernah ditampilkan;
- argv yang akan dieksekusi, dengan argumen ditampilkan sebagai literal dalam tanda kutip.

### 2.4 Contoh

```
$ bq bin ls
NAME                  VERSI    RUNTIME  TIER      STATUS      SUMBER
k8s-scale-active      1.2.0    bash     external  active      cache
gitops-match-tag      0.4.1    python   read      active      pb
svc-endpoints-audit   2.0.0    bash     read      deprecated  pb  → pengganti: <nama>  (dari field replacement)

$ bq bin --dry-run gitops-match-tag plus-be-merchantappauth-module --json
{"name":"gitops-match-tag","version":"0.4.1","sha256":"9f2c…","source":"pb",
 "runtime":"python","interpreter":"python3 -I","env_needed":["GITHUB_TOKEN"],
 "env_missing":[], "argv":["python3","-I","<tmp>","plus-be-merchantappauth-module"]}

$ bq bin gitops-match-tag plus-be-merchantappauth-module
...output tool...
$ echo $?   # exit code tool
```

---

## 3. Skema PocketBase

Nama collection diusulkan, belum dibuat. Pembuatan hanya di staging, oleh DevOps. Produksi butuh izin manusia.

### 3.1 `cli_tools` (satu record per tool)

| Field | Tipe | Wajib | Keterangan |
|---|---|---|---|
| `name` | text | ya | Unik, pola `^[a-z][a-z0-9-]{1,60}$`. Nama yang bentrok dengan perintah bawaan bq atau berkas di `~/.build-q/bin` ditolak. |
| `description` | text | ya | Satu kalimat. |
| `runtime` | select | ya | `bash`, `python`. (`binary` ditunda, lihat keputusan 2.) |
| `tier` | select | ya | `read`, `local`, `external`, sama dengan `tool_policy.py`. |
| `current_version` | relation → `cli_tool_versions` | tidak | Versi aktif. Kosong berarti tool belum rilis. |
| `status` | select | ya | `active`, `deprecated`, `yanked`. |
| `replacement` | text | tidak | Nama tool pengganti bila `deprecated`. |
| `owner` | relation → `devops_users` | ya | Pemilik yang bertanggung jawab. |
| `env_required` | json | tidak | Daftar nama env, mis. `["GITHUB_TOKEN"]`. Hanya nama, tidak pernah nilai. |
| `requires` | json | tidak | Biner pendukung, mis. `["yq", "jq"]`. |
| `args_spec` | json | tidak | Dokumentasi argumen (bagian 3.3). Dipakai `show` dan `--help`. |
| `active` | bool | ya | `false` menyembunyikan tool dari `ls`. |
| `created` / `updated` | autodate | — | Pola standar PocketBase. |

### 3.2 `cli_tool_versions` (satu record per versi, immutable setelah rilis)

| Field | Tipe | Wajib | Keterangan |
|---|---|---|---|
| `tool` | relation → `cli_tools` | ya | |
| `version` | text | ya | SemVer. Unik per `tool`. |
| `state` | select | ya | `draft`, `released`, `yanked`. |
| `body` | file (maks 512 KB) | ya | Isi script. Batas 512 KB untuk bash dan python; di atas itu ditolak. |
| `sha256` | text | ya | Hash isi `body`, dihitung saat publish. |
| `signature` | text | tidak | Tanda tangan Ed25519 base64 (bila keputusan 1 disetujui). |
| `changelog` | text | ya | Wajib diisi. |
| `args_spec` | json | tidak | Salinan spesifikasi argumen saat rilis. |
| `author` | relation → `devops_users` | ya | Penulis draft. |
| `releaser` | relation → `devops_users` | tidak | Perilis. Wajib berbeda dari `author` (dijaga di hook/rule). |
| `released_at` | date | tidak | |
| `yanked_reason` | text | tidak | Wajib bila `state = yanked`. |

Aturan tambahan:
- Setelah `state = released`, field `body`, `sha256`, `signature`, dan `version` tidak bisa diubah. Hanya `state` (ke `yanked`) dan `yanked_reason` yang bisa.
- Batas ukuran `body` dan validasi `sha256` dijalankan ulang di server (hook) saat publish.
- Hapus record tidak diizinkan (`deleteRule` none). Yank adalah jalur penarikan.

### 3.3 Format `args_spec`

```json
{
  "usage": "gitops-match-tag <repo> [--json]",
  "positional": [{"name": "repo", "required": true, "pattern": "^[a-z0-9-]+$"}],
  "flags": [{"name": "--json", "description": "Keluaran JSON"}],
  "max_args": 4
}
```

Di v1, `args_spec` hanya dokumentasi. `bq` menampilkannya di `show` dan `--help`. Validasi pola argumen dilakukan oleh tool sendiri. Validasi dari `bq` ditunda (lihat bagian 10, keputusan 5 bila relevan).

### 3.4 Akses per peran (usulan, perlu dikonfirmasi DevOps)

Peran di `devops_users` belum diketahui persis. Yang terbaca: rule pembuatan mensyaratkan `role=view` untuk pengguna baru. Peran di bawah adalah konsep, nama field-nya perlu dicocokkan dengan schema sebenarnya.

| Aksi | viewer | maintainer | perilis (release/rollback/yank) | admin (skema) |
|---|---|---|---|---|
| `ls`, `show`, jalankan tool `released` dan `active` | ya | ya | ya | ya |
| Lihat `body` versi `draft` | tidak | penulisnya saja | ya | ya |
| Buat tool dan draft versi | tidak | ya | ya | ya |
| Ubah `draft` milik sendiri | tidak | penulisnya saja | ya | ya |
| `release` / `rollback` | tidak | tidak | ya, bukan penulis draft | ya |
| `yank` | tidak | tidak | ya | ya |
| Hapus record | tidak | tidak | tidak | tidak (tidak ada delete rule) |
| Ubah skema | tidak | tidak | tidak | izin manusia |

Rule PB (usulan bentuk, belum final):

- `listRule` `cli_tools`: `active = true && (@request.auth.id != "")`
- `listRule` `cli_tool_versions`: `state = "released" || author = @request.auth.id || @request.auth.role = "admin"`
- `updateRule` `cli_tool_versions`: `state = "draft" && author = @request.auth.id`
- `deleteRule`: kosong (none).
- Pemeriksaan `releaser != author` dijalankan di hook server (`onRecordUpdate`), bukan hanya di rule, karena rule PB tidak membandingkan dua field relasi yang berbeda pada satu update.

---

## 4. Eksekusi

### 4.1 Alur `bq bin <cmd> args...`

1. Cek login (`requires_login`). Tanpa login, berhenti dengan pesan `bq auth login`.
2. Muat indeks lokal `~/.build-q/bin/tools/index.json` (nama → versi aktif, sha256, status, `fetched_at`).
3. Ambil katalog dari PB bila `fetched_at` lebih tua dari TTL katalog (default 1 jam). Gagal ambil PB: lanjut ke langkah 4.
4. Tentukan versi yang akan dipakai, lalu cek status:
   - `yanked` → tolak (exit 121), walau ada di cache.
   - `deprecated` → jalankan, cetak peringatan dengan `replacement`.
   - tidak ditemukan → exit 120.
5. Cek cache: `~/.build-q/bin/tools/<name>/<version>/body`. Hitung sha256 dan bandingkan dengan `sha256` (dari indeks, yang diambil dari PB). Tidak cocok → hapus entri cache, ambil ulang dari PB. Tetap tidak cocok → exit 122.
6. Verifikasi tanda tangan (bila keputusan 1 disetujui). Gagal → exit 122.
7. Kumpulkan env: hanya nama dalam `env_required`. Nilai diambil dari `pb_api.fetch_secrets` (sudah tersedia di cache `.pb-cache.json`). Nama yang tidak ada → exit 124 dan tampilkan nama saja.
8. Buat direktori kerja sementara `~/.build-q/run/<random>` dengan mode `700`. Tulis `body` ke `run.<ext>` dengan mode `700`, bukan `eval`.
9. Jalankan proses anak:
   - bash: `/bin/bash <file> <args...>`
   - python: `python3 -I <file> <args...>`
   Argumen dikirim sebagai daftar argv (`subprocess.run([...], shell=False)`). Environment dimulai dari kosong, hanya ditambah `PATH` minimal (`/usr/bin:/bin:/usr/sbin:/sbin`), `HOME`, `LANG`, dan env yang dideklarasikan. Stdin, stdout, dan stderr diteruskan langsung (tanpa buffer), termasuk TTY.
10. Hapus direktori kerja di `finally`, termasuk saat proses dihentikan dengan Ctrl-C (signal ditangani dan diteruskan ke proses anak).
11. Exit dengan exit code anak.

### 4.2 Perilaku offline

| Kondisi | Perilaku |
|---|---|
| PB terjangkau | Ambil katalog, perbarui indeks, lalu jalankan. |
| PB tidak terjangkau, indeks ada dan `fetched_at` < `OFFLINE_MAX_AGE` (usulan 72 jam) | Jalankan dari cache. Cetak peringatan `sumber: stale-cache`. |
| PB tidak terjangkau, indeks lebih tua dari `OFFLINE_MAX_AGE` | Tolak (exit 123). Pesan menyebut usia cache. |
| PB tidak terjangkau, cache tool tidak ada | Tolak (exit 123). |

Dalam mode offline, tool yang sudah di-yank di PB tidak bisa diketahui. Batas 72 jam membatasi jendela itu. Ini diputuskan di bagian 10 (keputusan 4).

### 4.3 Verifikasi integritas dan file sementara

- Hash dihitung pada byte yang ditulis ke file sementara, bukan pada file yang sudah ada di disk. Dua langkah (hitung lalu baca ulang file) menyisakan jendela race. Dengan menulis byte yang sudah diverifikasi dari memori ke file yang baru dibuat (`O_EXCL`, mode `0700`), jendela itu hilang.
- Race dengan proses lain pada akun yang sama tetap ada. Ini risiko residual: pada mesin dengan akun yang sama, proses lain sudah bisa membaca `~/.build-q` (kredensial ada di sana juga). Dicatat di threat model (bagian 9).
- Cache tool: `~/.build-q/bin/tools/`, direktori `0700`, berkas `0600`. Cache tidak pernah menyimpan rahasia.
- Nama `docker-buildx` di `~/.build-q/bin/` tidak masuk `tools/`. Nama tool yang bentrok dengan berkas di `~/.build-q/bin/` ditolak saat publish.

### 4.4 Rahasia

- Rahasia tidak pernah disimpan di `cli_tools`, `cli_tool_versions`, argumen, atau cache.
- Nilai env hanya ada di memori `bq` dan env proses anak.
- `--dry-run`, `show`, dan log hanya menampilkan **nama** env.
- Peringatan: argumen yang berisi rahasia (mis. `--token xyz`) akan terlihat di `ps`. Dokumentasi pengguna meminta rahasia lewat env, bukan argumen. `bq` tidak mencoba mendeteksi ini.

### 4.5 Kenapa tidak `eval`

Isi tool tidak pernah dikonkatenasi ke shell `bq`. Tool dijalankan sebagai file dengan interpreter tetap, dan argumen masuk sebagai argv. Dengan begitu, argumen seperti `$(rm -rf ~)` atau `; curl ...` tampil sebagai teks literal dan tidak dieksekusi oleh `bq`. Tes wajib memverifikasi ini (bagian 7).

---

## 5. Alur maintenance

### 5.1 Terbitkan tool baru

1. Penulis: `bq bin publish ./tool.sh --name ... --version 0.1.0 --changelog "..."`. `bq` membuat `cli_tools` (jika belum ada) dan `cli_tool_versions` dengan `state = draft`, menghitung sha256, dan memeriksa ukuran dan nama.
2. Penulis mengajukan review (pada tiket atau PR), biasanya di repo `build-q` untuk tool yang disimpan juga di Git. Sumber kebenaran isi tool tetap di Git bila ada; PB adalah distribusi.
3. Perilis (bukan penulis) memeriksa diff isi dan argumen, lalu `bq bin release <cmd> 0.1.0`. `current_version` diisi.

### 5.2 Perbarui tool

Selalu versi baru: `publish` dengan versi yang lebih tinggi. Versi lama tetap `released` dan bisa dijalankan dengan `bq bin --version <v> <cmd> args...` (flag ini termasuk di bagian 2.1).

### 5.3 Rollback

`bq bin rollback <cmd> <versi-sebelumnya>` memindahkan `current_version`. Syarat: versi tujuan `released` dan tidak `yanked`. Rollback tidak mengubah isi versi apa pun.

### 5.4 Yank (darurat)

`bq bin yank <cmd> <versi>` mengubah `state` menjadi `yanked` dan mewajibkan alasan. Jika versi itu adalah `current_version`, pemilihan versi aktif mundur ke versi `released` terbaru yang tidak di-yank. Tool yang tidak punya versi tersisa berubah ke `status = deprecated`. Yank berlaku di semua klien setelah indeks diperbarui (maks. TTL katalog, atau langsung bila klien online).

### 5.5 Deprecate

`status = deprecated` dengan `replacement`. Tool tetap berjalan dengan peringatan. Penghapusan total tidak dilakukan lewat `bq`; hanya `active = false` yang menyembunyikan tool dari `ls`.

---

## 6. Inventaris dan migrasi

### 6.1 Klasifikasi (usulan, per skrip)

| Kelompok | Contoh | Risiko | Fase migrasi |
|---|---|---|---|
| A. Baca saja, tanpa rahasia, tanpa path mesin | `k8s-latest-tag`, `check-pod-ip`, `svc-endpoints-audit` (mode read) | read | Fase 3 |
| B. Baca saja dengan token GitHub/Docker | `gh-latest-tag`, `gh-check-lang`, `tag-match`, `gitops-match-gh-latest-tag`, `image-ready` | read, open-world | Fase 3 |
| C. Ubah cluster atau GitOps | `set-image`, `k8s-scale-active-deploys`, `gitops-audit --apply-*`, `gh-k8s-dump`, `apply-file-config` | external | Fase 4, dengan review dan release dua orang |
| D. Menyentuh rahasia atau SOPS | `k8s-secret`, `sops-*`, `source_auth.py`, `gh-fetch-secret`, `get-file-secret`, `argocd-app token-set` | local/external, sensitif | Tetap lokal. Tidak dimigrasi di v1 |
| E. Bootstrap dan init repo | `cicd-init*`, `k8s-init-*`, `init-master`, `bootstrap-app` | external | Fase 4, bila sudah ada kebutuhan |
| F. Biner | `docker-buildx` | — | Tetap lokal (keputusan 2) |

Menurut inventaris, sekitar 25 dari 66 skrip menyebut kata seperti TOKEN, SECRET, atau PASSWORD. Kebanyakan adalah nama env, bukan nilai, tetapi wajib diaudit sebelum migrasi, termasuk memastikan tidak ada nilai yang tertanam.

### 6.2 Langkah migrasi per skrip

1. Hapus path mesin (`~/.pi/`, `~/.agents/`), ganti dengan env yang dideklarasikan.
2. Ganti pemanggilan ke skrip lain dengan `bq bin <cmd>` (atau dengan fungsi internal bila ringan).
3. Isi `args_spec`, `env_required`, `requires`.
4. Jalankan `shellcheck` (bash) atau `ruff` (python) dan tes lokal dengan `--dry-run`.
5. Publish sebagai draft, release oleh perilis, lalu hapus salinan di skill `devops-bin` dalam satu PR terpisah (mengikuti pola skill-convert bila perlu).

### 6.3 Kompatibilitas

Selama masa transisi, skill `devops-bin` tetap memanggil path lama. Skrip lama tidak dihapus sampai `bq bin` versi yang sama terbukti di dua siklus rilis. Bila ada masalah, skrip lama masih bisa dipakai.

---

## 7. Rencana tes

Unit (offline, tanpa PB):

- Hash mismatch di cache → cache dihapus dan diambil ulang; hash tetap tidak cocok → exit 122.
- Versi `yanked` → exit 121, walau ada di cache.
- `deprecated` → jalan, ada peringatan di stderr.
- Env wajib tidak ada → exit 124, nama disebut, nilai tidak pernah tercetak.
- Argumen berisi `$(...)`, backtick, `;`, dan spasi → diteruskan literal (uji dengan tool gema yang menulis argv).
- Exit code anak `0`, `1`, `42`, `130` diteruskan tanpa berubah.
- Mode file dan direktori (`700`/`600`) sesuai.
- File sementara terhapus saat sukses, gagal, dan Ctrl-C.
- `--dry-run` tidak menjalankan tool (dibuktikan dengan tool yang menulis file penanda) dan tidak mencetak nilai env.
- Offline: di dalam batas 72 jam → jalan; di luar → exit 123.

Integrasi (PB staging, bukan produksi):

- Alur publish → draft → release oleh akun berbeda → jalankan → rollback → yank.
- Penulis tidak bisa release draft miliknya sendiri (dijaga di hook dan dicek lewat API).
- Versi `released` tidak bisa di-update isinya (403 dari PB).
- Akun `viewer` tidak bisa melihat `draft`.

Keamanan (diminta Boss setelah PRD, lihat bagian 9):

- Body yang diubah tanpa mengubah sha256 → ditolak.
- Tanda tangan salah atau kunci lain → ditolak (bila keputusan 1 disetujui).
- Percobaan injeksi lewat nama tool, argumen, dan changelog.

Catatan data uji: data uji diberi awalan `TEST-` atau `UJI-BIN-`, dibuat di PB staging atau instance lokal yang disediakan DevOps (port 8193 ke atas), dan dihapus oleh DevOps. Tidak ada record uji di produksi.

---

## 8. Fase rollout

| Fase | Isi | Gerbang keluar |
|---|---|---|
| 0. PRD | Dokumen ini, review, keputusan manusia | Keputusan 1–5 disetujui |
| 1. Staging | DevOps membuat `cli_tools`, `cli_tool_versions` di staging. Hook dan rule diuji | Tes integrasi lulus |
| 2. CLI baca | `bq bin ls`, `show`, `--dry-run`, eksekusi tool `released` di staging | Tes unit dan keamanan lulus, Security review |
| 3. Publish dan rilis | `publish`, `release`, `rollback`, `yank`. Migrasi kelompok A dan B | Dua orang sudah mencoba alur rilis, tidak ada insiden |
| 4. Kelompok C dan E | Migrasi aksi yang mengubah cluster dan repo, dengan review wajib | Review per tool oleh DevOps dan Security |
| 5. Produksi | Pembuatan collection di produksi **hanya** setelah izin manusia. Lalu hapus salinan di `devops-bin` | Izin manusia tertulis |

Kelompok D tidak masuk fase mana pun sampai ada desain terpisah untuk rahasia.

---

## 9. Threat model (ringkas, untuk review Security)

Aset: laptop engineer yang menjalankan `bq bin`, kredensial di `~/.build-q` dan `secrets`, serta cluster dan repo yang bisa diubah tool.

| Ancaman | Dampak | Kontrol |
|---|---|---|
| Penulis jahat atau akun penulis yang diambil alih menerbitkan tool berbahaya | Eksekusi kode di semua laptop | Rilis dua orang (penulis ≠ perilis). Tanda tangan di luar PB (keputusan 1). Kebijakan rilis dan audit log. Yank cepat. |
| Akses tulis langsung ke record PB lewat API (lewati hook) | Sama seperti di atas | Rule `updateRule` hanya untuk draft. Rilis lewat hook yang memeriksa sha256 dan tanda tangan. Tanda tangan dicek di klien, jadi perubahan DB tanpa kunci tidak lolos. |
| Pengubahan isi tool setelah rilis | Tool berubah diam-diam | Versi `released` immutable. Cek sha256 setiap jalan. |
| Injeksi shell lewat argumen | Eksekusi perintah tak terduga | Tanpa `eval`. Argv terpisah. Interpreter tetap. |
| Kebocoran rahasia lewat cache, log, atau argumen | Kredensial bocor | Rahasia tidak pernah di cache atau katalog. Dry-run dan log hanya nama. Dokumentasi: rahasia lewat env. |
| Race pada file sementara | Tool diganti di antara tulis dan eksekusi | Byte ditulis dari memori, file baru `O_EXCL`, mode `0700`, direktori `0700`. Risiko residual dicatat. |
| Cache tampil setelah yank (offline) | Tool yang sudah ditarik masih jalan | Batas offline 72 jam (keputusan 4). Yank yang darurat perlu komunikasi di luar bq. |
| Penyerang lokal pada akun yang sama | Sudah di luar jangkauan bq | Di luar cakupan. Dicatat. |
| Peningkatan hak lewat peran | Viewer bisa merilis | Pemeriksaan peran di rule dan hook. Peta peran perlu dikonfirmasi DevOps. |

Pertanyaan untuk Security: (a) apakah Ed25519 dengan kunci di luar PB cukup, atau perlu alur kunci yang lebih ketat; (b) apakah audit log per rilis harus disimpan di collection terpisah; (c) apakah batas 72 jam offline dapat diterima.

---

## 10. Keputusan untuk manusia

Maksimal lima. Masing-masing dengan rekomendasi.

1. **Tanda tangan tool.** Rekomendasi: Ed25519, kunci privat dipegang perilis (bukan di PB), kunci publik dibundel di rilis `bq`. `bq` menolak tool tanpa tanda tangan valid. Alternatif: sha256 saja, lebih sederhana tetapi tidak melindungi bila DB ditulis oleh pihak yang tidak berhak.
2. **Biner di v1.** Rekomendasi: tidak. v1 hanya bash dan python. Biner (mis. `docker-buildx`, 65 MB) tetap lokal dan dikelola lewat jalur lain. Alternatif: menyimpan biner di file PB dengan batas ukuran (perlu cek batas upload PB dan kuota).
3. **Aturan rilis.** Rekomendasi: dua orang wajib (penulis ≠ perilis), dijaga di hook. Alternatif: satu admin boleh rilis, lebih cepat tetapi lebih lemah.
4. **Batas offline.** Rekomendasi: 72 jam sejak sinkronisasi terakhir, dengan peringatan tampil di setiap jalan. Alternatif: 24 jam (lebih ketat, lebih sering gagal di laptop yang sering offline).
5. **Pembuatan collection dan kode galat.** Rekomendasi: DevOps membuat collection di staging dulu (fase 1), produksi menunggu izin manusia tertulis setelah Security review. Untuk kode galat, rekomendasi: prefiks `[bq-bin]` di stderr setiap kali exit code berasal dari `bq`, supaya tidak tertukar dengan tool.

---

## 11. Yang belum dijawab

- Peran pasti di `devops_users` dan nama field-nya (akses RnD hanya bisa membaca record, schema 403).
- Apakah `secrets` boleh dibaca oleh peran `viewer`. Saat ini `bq` membacanya dengan akun pengguna, jadi perilaku yang sudah ada tetap dipakai.
- Apakah tool yang berjalan di mesin lain (CI) perlu jalur yang sama. Di luar cakupan PRD ini.

Semoga dimudahkan, aamiin.
