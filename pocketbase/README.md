# PocketBase: collection `bq_bin` (PRD-bin fase 1)

Isi folder ini dipakai oleh `bq bin` (lihat `build_q/bin.py`):

- `pb_migrations/1793000000_bq_bin.js`: membuat collection `bq_bin`. Baca untuk pengguna login. Tulis hanya superuser (rule `null`), sehingga token admin bersama (`devops_users`) tidak bisa menulis.
- `pb_migrations/1793000001_bq_bin_protected.js`: field `file` collection `bq_bin` jadi `protected`. Unduhan wajib token berkas (`POST /api/files/token`), jadi tidak bisa tanpa login.
- `pb_hooks/bq_bin.pb.js`: setelah berkas tersimpan, server menghitung `sha256` dan `size` dari isi berkas. Bila `sha256` atau `size` yang dikirim klien tidak cocok, permintaan ditolak (400). Pada create record dihapus; pada update record tidak dihapus dan berkas serta field lama dikembalikan.

Jalankan PocketBase lokal dengan folder ini (hanya untuk uji; jangan dijalankan di server produksi tanpa persetujuan):

```
pocketbase serve --http 127.0.0.1:8193 \
  --hooksDir ./pocketbase/pb_hooks \
  --migrationsDir ./pocketbase/pb_migrations
```

Catatan: handler JSVM tidak berbagi fungsi top-level, jadi logika hook ditulis di tiap handler.
