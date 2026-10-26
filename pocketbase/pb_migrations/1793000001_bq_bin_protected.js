/// <reference path="../pb_data/types.d.ts" />
// Field file collection bq_bin bersifat protected (PRD-bin F1, review Security HAG-63):
// unduhan wajib memakai token berkas dari POST /api/files/token, jadi tidak bisa tanpa login.
migrate((app) => {
  const collection = app.findCollectionByNameOrId("bq_bin");
  collection.fields.getByName("file").protected = true;
  return app.save(collection);
}, (app) => {
  const collection = app.findCollectionByNameOrId("bq_bin");
  collection.fields.getByName("file").protected = false;
  return app.save(collection);
});
