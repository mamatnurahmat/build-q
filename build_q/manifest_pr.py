"""`bq --manifest-init <repo> <namespace> --pr` — ajukan saran manifest sebagai PR ke repo GitOps.

Isi PR:
  - YAML Deployment / Service saran `--manifest-init` (baru, atau perbaikan file yang ada);
  - `kustomization.yaml`: hanya baris `resources` yang DITAMBAHKAN (indentasi, komentar & isi
    lain tetap persis); hasilnya divalidasi — bila selain `resources` ikut berubah, PR dibatalkan;
  - TIDAK PERNAH Secret: saran Secret dibuang, dan file ber-`kind: Secret` / nama `*secret*`
    ditolak (Secret dibuat & di-apply terpisah, terenkripsi SOPS bila disimpan).

Satu commit di branch baru `manifest/<app>-<namespace>-<waktu>` (GitHub Git Data API, tanpa
clone) lalu PR ke branch GitOps env (develop/staging/sandbox atau main) — tidak pernah push
langsung ke branch tujuan.
"""
from __future__ import annotations

import json
import posixpath
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional
from urllib.parse import quote


class ManifestPRError(RuntimeError):
    """Tidak ada perubahan, kustomization tidak bisa diubah dengan aman, atau GitHub menolak."""


@dataclass
class PRPlan:
    repo: str
    base: str
    branch: str
    title: str
    body: str
    files: Dict[str, str] = field(default_factory=dict)   # path → isi baru
    skipped: List[str] = field(default_factory=list)       # saran yang sengaja tidak di-commit
    notes: List[str] = field(default_factory=list)


# ─── kustomization: tambah resources tanpa merusak isi lain ───────────────

def append_resources(text: str, new_files: List[str]) -> str:
    """Tambahkan `new_files` ke `resources:` kustomization secara tekstual (minimal diff).

    Item baru mengikuti indentasi item terakhir; komentar, urutan & key lain tidak disentuh.
    Raise ManifestPRError bila hasil parse berbeda selain penambahan resources.
    """
    import yaml

    before = yaml.safe_load(text) or {}
    if not isinstance(before, dict):
        raise ManifestPRError("kustomization.yaml bukan mapping YAML")
    existing = [str(r).removeprefix("./") for r in before.get("resources") or []]
    todo = [f for f in new_files if f not in existing]
    if not todo:
        return text
    lines = text.splitlines(keepends=True)
    idx = next((i for i, line in enumerate(lines) if re.match(r"^resources:\s*(#.*)?$", line)), None)
    if idx is None:
        out = text + ("" if not text or text.endswith("\n") else "\n") + "resources:\n"
        out += "".join(f"  - {f}\n" for f in todo)
    else:
        last, indent = idx, "  "
        for j in range(idx + 1, len(lines)):
            line = lines[j]
            item = re.match(r"^(\s*)-\s", line)
            if item:
                last, indent = j, item.group(1)
            elif not line.strip() or line.lstrip().startswith("#"):
                continue
            else:
                break  # key berikutnya
        if not lines[last].endswith("\n"):
            lines[last] += "\n"
        lines.insert(last + 1, "".join(f"{indent}- {f}\n" for f in todo))
        out = "".join(lines)

    after = yaml.safe_load(out) or {}
    others = lambda d: {k: v for k, v in d.items() if k != "resources"}  # noqa: E731
    if others(after) != others(before) or list(after.get("resources") or []) != \
            list(before.get("resources") or []) + todo:
        raise ManifestPRError("kustomization.yaml tidak bisa diubah dengan aman (mis. resources inline) "
                              "— tambahkan manual: " + ", ".join(todo))
    return out


# ─── Rencana PR ──────────────────────────────────────────────────────────

def _is_secret(path: str, text: str) -> bool:
    from . import manifest_check as mc

    if "secret" in posixpath.basename(path).lower():
        return True
    try:
        return any(d.get("kind") == "Secret" for d in mc.parse_yaml(text))
    except Exception:
        return True  # tidak bisa dipastikan → jangan commit


def plan(res, *, kustomization_text: Optional[str] = None) -> PRPlan:
    """Rencana PR dari `manifest_init.InitResult` (tanpa efek samping)."""
    from . import manifest_check as mc

    app = posixpath.basename(res.path).removesuffix("_deployment.yaml")
    folder = posixpath.dirname(res.path)
    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
    p = PRPlan(repo=res.gitops_repo, base=res.gitops_branch,
               branch=f"manifest/{app}-{posixpath.basename(folder)}-{stamp}",
               title=f"chore(manifest): {app} di {folder} ({'baru' if res.mode == 'new' else 'perbaikan'})",
               body="")
    for sug in res.suggestions:
        if _is_secret(sug["path"], sug["yaml"]):
            p.skipped.append(f"{sug['path']} (Secret — buat & apply terpisah, jangan commit plaintext)")
            continue
        if posixpath.basename(sug["path"]) == "kustomization.yaml":
            if kustomization_text is not None:
                continue  # folder sudah punya kustomization → cukup tambah resources di bawah
            p.files[sug["path"]] = sug["yaml"]
            continue
        p.files[sug["path"]] = sug["yaml"]

    # Daftarkan file Deployment/Service yg belum ada di resources (baru, atau temuan not-in-kustomization).
    kz_path = f"{folder}/kustomization.yaml"
    if kustomization_text is not None:
        register = [posixpath.basename(path) for path in p.files if path.startswith(folder + "/")]
        if res.mode == "existing":
            register += [posixpath.basename(f["message"].split(" ", 1)[0]) for f in res.findings
                         if f["rule_name"] == "manifest_not_in_kustomization"]
        new_kz = append_resources(kustomization_text, list(dict.fromkeys(register)))
        if new_kz != kustomization_text:
            p.files[kz_path] = new_kz
            p.notes.append(f"{kz_path}: hanya menambah resources {', '.join(dict.fromkeys(register))}")
    if not any(path != kz_path for path in p.files) and kz_path not in p.files:
        raise ManifestPRError("tidak ada perubahan Deployment/Service untuk di-PR"
                              + (" — manifest sudah sesuai" if res.mode == "existing" else ""))

    lines = [f"Saran manifest dari `bq --manifest-init {res.repo.split('/')[-1]} {res.namespace}` "
             f"({'aplikasi baru' if res.mode == 'new' else 'perbaikan manifest yang ada'}).", "",
             f"- Image: `{res.image}` · namespace `{res.namespace}` · stack {res.stack} · port {res.port}",
             f"- Dasar YAML: {res.origin}", "", "**File**", ""]
    lines += [f"- `{path}`" for path in p.files]
    if res.findings:
        lines += ["", "**Temuan yang diperbaiki**", ""]
        lines += [f"- [{f['severity']}] {f['rule_name']}: {f['message']}" for f in res.findings]
    if p.skipped:
        lines += ["", "**Tidak di-commit**", ""] + [f"- {s}" for s in p.skipped]
    lines += ["", *[f"> {n}" for n in p.notes], "",
              "_Dibuat otomatis oleh build-q (read-only terhadap cluster). Review sebelum merge._"]
    p.body = "\n".join(lines)
    return p


def render_plan(p: PRPlan) -> str:
    lines = [f"📦 PR GitOps: {p.repo}  {p.branch} → {p.base}", f"   judul: {p.title}"]
    lines += [f"   + {path}" for path in p.files]
    lines += [f"   ✗ {s}" for s in p.skipped]
    lines += [f"   ℹ️  {n}" for n in p.notes]
    return "\n".join(lines)


# ─── Eksekusi (GitHub Git Data API) ──────────────────────────────────────

def create(p: PRPlan) -> dict:
    """Satu commit di branch baru dari `base` lalu PR. Return respons PR (html_url, number)."""
    from . import github_api as gh

    for path, text in p.files.items():
        if _is_secret(path, text):  # pengaman terakhir
            raise ManifestPRError(f"menolak commit Secret: {path}")
    post = lambda path, body: gh._request("POST", path, body=json.dumps(body).encode())  # noqa: E731
    try:
        base_sha = gh._request("GET", f"/repos/{p.repo}/git/ref/heads/{quote(p.base)}")["object"]["sha"]
        base_tree = gh._request("GET", f"/repos/{p.repo}/git/commits/{base_sha}")["tree"]["sha"]
        tree = post(f"/repos/{p.repo}/git/trees", {
            "base_tree": base_tree,
            "tree": [{"path": path, "mode": "100644", "type": "blob", "content": text}
                     for path, text in p.files.items()],
        })
        commit = post(f"/repos/{p.repo}/git/commits", {
            "message": f"{p.title}\n\nDibuat oleh bq --manifest-init --pr", "tree": tree["sha"],
            "parents": [base_sha],
        })
        post(f"/repos/{p.repo}/git/refs", {"ref": f"refs/heads/{p.branch}", "sha": commit["sha"]})
        return gh.create_pull_request(p.repo, base=p.base, head=p.branch, title=p.title, body=p.body)
    except gh.GitHubAPIError as e:
        raise ManifestPRError(str(e)[:300]) from e
