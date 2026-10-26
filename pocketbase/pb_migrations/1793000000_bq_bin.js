/// <reference path="../pb_data/types.d.ts" />
// Collection bq_bin (PRD-bin fase 1). Baca untuk pengguna login; tulis hanya superuser
// (rule null). Token admin bersama (devops_users) tidak bisa menulis.
migrate((app) => {
  const collection = new Collection({
    type: "base",
    name: "bq_bin",
    listRule: '@request.auth.id != ""',
    viewRule: '@request.auth.id != ""',
    createRule: null,
    updateRule: null,
    deleteRule: null,
    fields: [
      { type: "text", name: "name", required: true, min: 1, max: 64, pattern: "^[a-z0-9][a-z0-9._-]{0,63}$" },
      { type: "file", name: "file", required: true, maxSelect: 1, maxSize: 524288 },
      { type: "text", name: "sha256", max: 64 },
      { type: "number", name: "size", onlyInt: true, min: 0 },
      { type: "text", name: "mode", max: 4, pattern: "^0(644|755)$" },
      { type: "select", name: "kind", maxSelect: 1, values: ["bash", "python", "binary"] },
      { type: "text", name: "version", max: 64 },
      { type: "text", name: "notes", max: 2000 },
      { type: "autodate", name: "created", onCreate: true },
      { type: "autodate", name: "updated", onCreate: true, onUpdate: true },
    ],
    indexes: ["CREATE UNIQUE INDEX idx_bq_bin_name ON bq_bin (name)"],
  });
  return app.save(collection);
}, (app) => {
  const collection = app.findCollectionByNameOrId("bq_bin");
  return app.delete(collection);
});
