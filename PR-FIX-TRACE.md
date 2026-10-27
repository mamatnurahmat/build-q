# `bq --pr-fix` — jejak run & klasifikasi penyebab (Jev)

Dokumen ini menjelaskan apa yang dicatat setiap `bq --pr-fix <repo> <ref>`, collection PocketBase yang dipakai, dan peran Jev (TypeSafe System One).

## Pemakaian

```bash
# standar (tanpa Jev)
bq --pr-fix Qoin-Digital-Indonesia/<repo> <tag>

# dengan klasifikasi penyebab dari log step pipeline yang gagal
bq --pr-fix <repo> <tag> --failure-log /path/ke/release.log

# simulasi tanpa push/PR
bq --pr-fix <repo> <tag> --dry-run --failure-log /path/ke/release.log
```

Setiap run menulis jejak ke `~/.build-q/pr-fix-runs/<run_id>.jsonl` dan (bila PocketBase bisa diakses) ke collection di bawah.

## Collection PocketBase

Schema lengkap ada di `build_q/pr_fix_trace.py` (`PR_FIX_COLLECTIONS`).

### `pr_fix_runs` — satu record per run

| Field | Tipe | Keterangan |
|---|---|---|
| `run_id` | text, unik | `<repo>-<ref>-<timestamp>` |
| `repo`, `ref` | text | repo dan tag/branch sumber |
| `status` | select | `running` · `ok` · `no_change` · `dry_run` · `failed` |
| `head_sha`, `branch`, `pr_url` | text / url | hasil clone, branch fix, dan PR |
| `cause`, `cause_source`, `cause_confidence` | text / number | penyebab dari klasifikasi (`jev` atau `rule`) |
| `started_at`, `finished_at` | date | waktu run |

### `pr_fix_steps` — satu record per langkah

| Field | Tipe | Keterangan |
|---|---|---|
| `run_id` | text | relasi ke `pr_fix_runs.run_id` |
| `seq` | number | urutan langkah dalam run |
| `step` | text | lihat daftar langkah di bawah |
| `status` | select | `ok` · `skip` · `fail` · `info` |
| `detail` | json | data langkah (file, branch, error, verdict Jev, ...) |
| `at` | date | waktu langkah |

## Daftar langkah dan saran perbaikannya

| Langkah | Status yang mungkin | Saran bila gagal / perlu perhatian |
|---|---|---|
| `preflight` | ok / fail | Lengkapi `GITHUB_TOKEN` dan `WEBHOOK_TRIGGER_TOKEN` di `~/.build-q/.env`. |
| `fetch_cicd` | ok | Pastikan `cicd/cicd.json` ada di ref. |
| `clone` | ok | Catat HEAD sebagai dasar perubahan. |
| `branch` | ok | Branch `fix/jx-init-<timestamp>` dibuat dari ref. |
| `detect_registry_placeholder` | ok / info | Bila `registry.example.com` ditemukan di Makefile atau compose.yaml, artefak di-render ulang dengan `resolve_registry()` (→ `loyaltolpi`). |
| `classify_cause` | info | Hanya bila `--failure-log` diberikan. Lihat bagian Jev. |
| `render:<file>` | ok | Setiap artefak yang berubah dicatat dengan jumlah baris (+/-). |
| `render_artifacts` | skip | Semua artefak sudah up-to-date, run berakhir `no_change`. |
| `commit` | ok | Daftar file yang di-commit dan file legacy yang dihapus. |
| `push` | ok / fail | Bila gagal karena scope `workflow`, perbarui token GitHub (lihat pesan di CLI). |
| `open_pr` | ok / skip / fail | `skip` bila PR sudah terbuka untuk branch yang sama. |
| `unhandled` | fail | Exception tak terduga; run ditandai `failed`. |

## Peran Jev (TypeSafe System One)

Prinsip: aturan yang bisa dihitung tetap di code. Jev hanya dipakai untuk penilaian semantik pada log yang tidak cocok dengan aturan.

Alur `build_q/jev.py:classify()`:

1. Aturan regex (`rule_classify`) dijalankan lebih dulu. Bila cocok dengan kategori spesifik, hasilnya dipakai (`source=rule`).
2. Bila aturan menghasilkan `other` dan `TYPESAFE_API_KEY` tersedia, log dikirim ke Jev sebagai pertanyaan `choice` dengan kategori:
   `registry_placeholder`, `missing_secret`, `dockerfile_base_image`, `go_version`, `build_failure`, `other`.
3. Jawaban Jev dipakai hanya bila `confidence >= 0.7` (`source=jev`). Di bawah itu, hasil aturan dipakai dan jawaban Jev dicatat sebagai `jev_cause`.
4. Error jaringan atau API tidak menggagalkan `--pr-fix`; alasannya dicatat di field `note`.

Model dan threshold bisa diatur lewat env `JEV_MODEL` dan konstanta `DEFAULT_THRESHOLD`.

## Catatan

- Jejak lokal selalu ditulis. Sinkronisasi ke PocketBase bersifat best-effort: bila collection belum ada atau token tidak berizin, sinkronisasi dimatikan untuk sisa run dan alur tetap berjalan.
- Membuat collection membutuhkan akun superuser PocketBase. Schema di atas bisa dipakai sebagai acuan.
