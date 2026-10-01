# Verify Remote Build (`DOCKER_CONTEXT`)

Panduan membuktikan bahwa `bq` benar-benar menjalankan `docker build` di host remote (via `DOCKER_CONTEXT`), bukan di laptop lokal.

Berguna saat memakai perintah seperti:

```bash
DOCKER_CONTEXT=sls-j \
BUILD_Q_ENV_FILE="$HOME/build-q/.env" \
BUILD_Q_DISABLE_PB_API=true \
  bq plus-be-rusttransaction-module staging --remote --rebuild
```

Di atas, build harus eksekusi di VM `sls-j` (contoh endpoint `ssh://mamat@193.1.1.3`), bukan di Docker Desktop/OrbStack lokal.

---

## Resource yang terlibat

| Resource | Peran | Beban |
|---|---|---|
| Laptop lokal | CLI `bq` (Python), `git clone` (karena `--remote`), SSH client ke VM | Ringan |
| **Host remote (`DOCKER_CONTEXT`)** | **`docker buildx build` + push image** | **Berat** |
| GitHub | Sumber repo (clone saat `--remote`) | — |
| Docker Hub (`REGISTRY_URL`) | Target push image hasil build | — |

Build berat berjalan di host remote — laptop lokal hanya orkestrasi.

---

## 1. Cek endpoint docker context

```bash
docker context inspect "$DOCKER_CONTEXT" | grep -i host
# Contoh output:
#   "Host": "ssh://mamat@193.1.1.3"
```

Pastikan endpoint-nya `ssh://` ke host remote yang diharapkan.

---

## 2. Monitor proses build real-time di VM (bukti paling kuat)

Buka dua terminal.

**Terminal A — monitor VM:**

```bash
ssh mamat@193.1.1.3 'watch -n 1 "docker ps --filter name=buildx; docker buildx ls; uptime"'
```

**Terminal B — jalankan build:**

```bash
DOCKER_CONTEXT=sls-j BUILD_Q_ENV_FILE="$HOME/build-q/.env" BUILD_Q_DISABLE_PB_API=true \
  bq plus-be-rusttransaction-module staging --remote --rebuild
```

Saat build berjalan, Terminal A akan menampilkan container `buildx_buildkit_mybuilder*` aktif + load average VM naik. Itu bukti build hidup di VM.

---

## 3. Pastikan laptop lokal tidak ikut build

```bash
docker --context default ps        # tidak ada container buildx
docker --context default images    # image hasil build TIDAK muncul di sini
```

Jika context default kosong dari jejak build → confirmed semua jalan di remote.

---

## 4. Verifikasi image hasil hanya ada di VM

```bash
# Di VM remote — harusnya ADA
ssh mamat@193.1.1.3 'docker images | grep plus-be-rusttransaction-module'

# Di laptop lokal — harusnya KOSONG
docker --context default images | grep plus-be-rusttransaction-module
```

---

## 5. Monitor konsumsi resource VM saat build

```bash
ssh mamat@193.1.1.3 'top -b -n 1 | head -20'
```

Proses `buildkitd` / `containerd` akan terlihat konsumsi CPU & RAM besar.

Alternatif ringan:

```bash
ssh mamat@193.1.1.3 'uptime && free -h && docker stats --no-stream'
```

---

## 6. Verifikasi via log `bq` verbose

```bash
DOCKER_CONTEXT=sls-j BUILD_Q_ENV_FILE="$HOME/build-q/.env" BUILD_Q_DISABLE_PB_API=true \
  bq plus-be-rusttransaction-module staging --remote --rebuild -v 2>&1 \
  | grep -iE "context|host|ssh|builder"
```

Cari baris yang menyebutkan `DOCKER_HOST=ssh://...` atau `using context sls-j`.

---

## 7. One-liner cepat (saat build sedang berjalan)

```bash
ssh mamat@193.1.1.3 'docker ps --format "{{.Names}}\t{{.Image}}" | grep -i buildkit'
```

Muncul `buildx_buildkit_mybuilder0` → **confirmed** build eksekusi di host remote.

---

## Checklist pembuktian

- [ ] `docker context inspect $DOCKER_CONTEXT` menunjukkan endpoint SSH remote
- [ ] Saat build jalan, VM remote punya container `buildx_buildkit_*` aktif
- [ ] Laptop lokal (`--context default`) tidak punya container buildx baru
- [ ] Image hasil build hanya muncul di `docker images` VM, tidak di laptop
- [ ] CPU/RAM VM remote naik saat build berlangsung

Jika semua ✅ → build sudah pasti berjalan di host remote via `DOCKER_CONTEXT`.

---

## Catatan

- `--remote` membuat `bq` meng-clone repo via `git` di laptop lokal, lalu kirim build context ke daemon remote via SSH tunnel. Transfer context awal bisa terasa di jaringan laptop → VM, tapi proses build (`FROM`, `RUN`, `COPY` layer) seluruhnya di VM.
- `--rebuild` menonaktifkan cache → build penuh, konsumsi resource VM lebih besar & waktu lebih lama.
- Builder `BUILDER_NAME` (default `mybuilder` dari `~/build-q/.env`) harus sudah ter-create di host remote. Cek:
  ```bash
  docker --context sls-j buildx ls
  ```
