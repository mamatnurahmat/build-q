// Collection bq_bin (PRD-bin fase 1). Setelah berkas tersimpan, server menghitung
// sha256 dan size dari isi berkas. Bila sha256 yang dikirim klien tidak cocok, record
// dihapus (berkas ikut terhapus) dan permintaan ditolak. Klien tidak menentukan sha256 sendiri.
// Catatan: handler JSVM tidak berbagi fungsi top-level, jadi logika ditulis per handler.

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
