// Collection bq_bin (PRD-bin fase 1). Setelah berkas tersimpan, server menghitung
// sha256 dan size dari isi berkas. Bila sha256 atau size yang dikirim klien tidak cocok:
//  - create: record dihapus (berkas ikut terhapus) dan permintaan ditolak.
//  - update: record TIDAK dihapus. Berkas dan field lama dikembalikan, lalu permintaan ditolak.
// Klien tidak menentukan sha256 sendiri.
// Catatan: handler JSVM tidak berbagi fungsi top-level, jadi logika ditulis per handler.

// PB menghapus berkas lama saat update tersimpan (sebelum hook after), jadi isinya disalin dulu.
onRecordUpdateExecute((e) => {
  const old = e.record.original().get("file");
  if (old) {
    const base = $app.dataDir() + "/storage/" + e.record.baseFilesPath() + "/";
    $os.writeFile(base + ".stash-" + old, $os.readFile(base + old), 0o644);
  }
  e.next();
}, "bq_bin");

onRecordAfterCreateSuccess((e) => {
  const stored = e.record.get("file");
  const path = $app.dataDir() + "/storage/" + e.record.baseFilesPath() + "/" + stored;
  const data = $os.readFile(path);
  const sha = $security.sha256(toString(data));
  const size = data.length;

  const claimed = e.record.get("sha256");
  if (claimed && claimed !== sha) {
    $app.delete(e.record);
    throw new BadRequestError("Sha256 tidak cocok dengan isi berkas.");
  }
  if (claimed !== sha || e.record.get("size") !== size) {
    e.record.set("sha256", sha);
    e.record.set("size", size);
    $app.save(e.record);
  }
  e.next();
}, "bq_bin");

onRecordAfterUpdateSuccess((e) => {
  const orig = e.record.original();
  const base = $app.dataDir() + "/storage/" + e.record.baseFilesPath() + "/";
  const data = $os.readFile(base + e.record.get("file"));
  const sha = $security.sha256(toString(data));
  const size = data.length;
  const stash = base + ".stash-" + orig.get("file");

  // Update wajib membawa sha256 dan size yang benar (CLI selalu mengirimnya). Tidak ada
  // penghitungan ulang di sini: nested save pada update gagal karena validasi file.
  if (e.record.get("sha256") !== sha || e.record.get("size") !== size) {
    // Kembalikan berkas lama dan semua field lama. Berkas yang ditolak ikut dibersihkan PB.
    $os.writeFile(base + orig.get("file"), $os.readFile(stash), 0o644);
    for (const f of ["file", "name", "sha256", "size", "mode", "kind", "version", "notes"]) {
      e.record.set(f, orig.get(f));
    }
    // Validasi file menolak nama lama sebagai berkas baru, jadi simpan tanpa validasi ulang.
    $app.saveNoValidate(e.record);
    throw new BadRequestError("Sha256 atau ukuran tidak cocok dengan isi berkas.");
  }
  try { $os.remove(stash); } catch (err) { /* salinan sudah tidak ada */ }
  e.next();
}, "bq_bin");
