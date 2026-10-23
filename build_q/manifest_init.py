"""`bq --manifest-init <repo> <env>` — saran YAML Deployment + Service (+ Secret, kustomization)
untuk satu aplikasi, mengikuti konsep GitOps yang dipakai pipeline saat ini. Read-only.

Alur:
  1. env → ref source: develop/staging/sandbox → branch yang sama; production → tag `v*`
     terbaru. Tag image = aturan pipeline (short SHA, atau nama tag untuk production).
  2. `cicd.json` (IMAGE, DEPLOYMENT, PROJECT, PORT, STACK) pada ref tsb → path GitOps lewat
     `rollout.compute_rollout` (sama dgn saran rollout pasca-build):
     `<infra>/<env>-<PROJECT>/<deployment>_deployment.yaml`, branch GitOps = env
     (develop/staging/sandbox) atau `main`.
  3. Manifest sudah ada → pemeriksaan tahap Manifest (`manifest_check.check`) + saran perbaikan.
     Belum ada → Deployment + Service dari sample tetangga `main:cce/<namespace>` (fallback
     generator standar), kerangka Secret yang belum ada di cluster, dan baris `resources`
     kustomization.yaml (atau kustomization.yaml baru bila folder belum ada).

Hasil hanya dicetak / ditulis ke `--out-dir` / `--export-md`; tidak ada commit atau apply.
"""
from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import quote

ENVS = ("develop", "staging", "sandbox", "production")
_SEMVER = re.compile(r"v(\d+)\.(\d+)\.(\d+)")


class ManifestInitError(RuntimeError):
    """Repo/env/ref tidak valid atau data source tidak bisa dibaca."""


@dataclass
class InitResult:
    repo: str
    env: str
    ref: str
    image: str
    gitops_repo: str
    gitops_branch: str
    path: str
    namespace: str
    stack: str
    port: int
    language: str = ""
    mode: str = "new"  # new | existing
    origin: str = ""
    details: List[str] = field(default_factory=list)
    findings: List[dict] = field(default_factory=list)
    secrets: Dict[str, str] = field(default_factory=dict)
    suggestions: List[dict] = field(default_factory=list)
    kustomization_path: str = ""
    kustomization_add: List[str] = field(default_factory=list)

    @property
    def exit_code(self) -> int:
        """0 = saran siap / manifest sehat, 1 = manifest yang ada punya temuan critical."""
        return 1 if any(f["severity"] == "critical" for f in self.findings) else 0


def _latest_tag(repo: str) -> Optional[str]:
    from . import e2e

    tags = e2e.gh_get(f"/repos/{repo}/tags?per_page=100") or []
    versions = [(tuple(int(x) for x in m.groups()), t["name"]) for t in tags
                if (m := _SEMVER.fullmatch(t.get("name", "")))]
    return max(versions)[1] if versions else None


# Penanda file root → bahasa (urutan = prioritas).
_MARKERS = (
    ("dotnet", lambda f: f.endswith((".csproj", ".sln", ".fsproj")) or f in ("global.json", "Directory.Build.props")
     or (f.startswith("appsettings") and f.endswith(".json"))),
    ("go", lambda f: f == "go.mod"),
    ("rust", lambda f: f == "Cargo.toml"),
    ("node", lambda f: f == "package.json"),
    ("python", lambda f: f in ("requirements.txt", "pyproject.toml", "setup.py", "Pipfile")),
)
_IMAGE_LANG = (("krakend", ("krakend",)), ("dotnet", ("mcr.microsoft.com/dotnet", "dotnet/")),
               ("go", ("golang",)), ("rust", ("rust",)), ("node", ("node",)), ("python", ("python",)))
_GH_LANG = {"Go": "go", "C#": "dotnet", "F#": "dotnet", "Rust": "rust", "TypeScript": "node",
            "JavaScript": "node", "Python": "python"}
DOCKERFILES = ("Dockerfile", "build.Dockerfile", "Dockerfile.production")


def _dockerfile(repo: str, ref: str) -> tuple:
    """(nama, isi) Dockerfile pertama yang ada di root repo, atau ("", "")."""
    from . import e2e, github_api

    for name in DOCKERFILES:
        try:
            return name, e2e.gh_raw(repo, name, ref)
        except github_api.GitHubAPIError:
            continue
    return "", ""


def _dockerfile_facts(text: str) -> dict:
    """Fakta stage FINAL Dockerfile: base image, WORKDIR efektif (ikut diwarisi lewat alias
    stage, mis. `FROM base`), dan ada/tidaknya ENTRYPOINT/CMD. WORKDIR berisi `$VAR` diabaikan."""
    stages, current = [], None
    for line in text.splitlines():
        m = re.match(r"^\s*FROM\s+(?:--platform=\S+\s+)?(\S+)(?:\s+AS\s+(\S+))?", line, re.I)
        if m:
            current = {"image": m.group(1), "alias": (m.group(2) or "").lower(), "workdir": "", "entry": False,
                       "config_copy": ""}
            stages.append(current)
            continue
        if current is None:
            continue
        w = re.match(r"^\s*WORKDIR\s+(\S+)", line, re.I)
        if w:
            path = w.group(1).strip("\"'")
            current["workdir"] = path if path.startswith("/") or not current["workdir"] else \
                f"{current['workdir'].rstrip('/')}/{path}"
        if re.match(r"^\s*(ENTRYPOINT|CMD)\s", line, re.I):
            current["entry"] = True
        # `COPY .env.${BRANCH} .env` / `COPY appsettings.json .` → lokasi file config di image
        cp = re.match(r"^\s*COPY\s+(?:--\S+\s+)*(\S+)\s+(\S+)\s*$", line, re.I)
        if cp and re.search(r"(^|/)\.env|appsettings", cp.group(1) + " " + cp.group(2)):
            dest = cp.group(2).strip("\"'")
            if dest in (".", "./"):
                dest = posixpath.basename(cp.group(1))
            base = current["workdir"] or "/"
            current["config_copy"] = dest if dest.startswith("/") else posixpath.join(base, dest)
    if not stages:
        return {"images": [], "final_image": "", "workdir": "", "entrypoint": False}
    by_alias = {st["alias"]: st for st in stages if st["alias"]}

    def resolve(st: dict, key: str, depth: int = 0):
        """Nilai stage, diwarisi dari stage induk bila `FROM <alias>`."""
        if st[key] or depth > 5:
            return st[key]
        parent = by_alias.get(st["image"].lower())
        return resolve(parent, key, depth + 1) if parent and parent is not st else st[key]

    final = stages[-1]
    image = final["image"]
    while image.lower() in by_alias and by_alias[image.lower()] is not final:
        image = by_alias[image.lower()]["image"]  # base image nyata di balik alias
        if image.lower() == final["image"].lower():
            break
    workdir = resolve(final, "workdir") or "/"  # tanpa WORKDIR → direktori kerja default Docker "/"
    copy = final["config_copy"]
    return {"images": [st["image"] for st in stages], "final_image": image,
            "workdir": "" if "$" in workdir else workdir, "entrypoint": bool(resolve(final, "entry")),
            "config_copy": "" if "$" in copy else copy}

def detect_language(repo: str, ref: str, cicd: dict) -> tuple:
    """(bahasa, bukti). Urutan: cicd.json STACK/TYPE → file penanda root → base image Dockerfile
    → GitHub Languages API → default go."""
    from . import e2e, github_api

    hint = str(cicd.get("STACK") or cicd.get("TYPE") or "").lower().strip()
    for lang, words in (("dotnet", ("dotnet", ".net", "csharp")), ("go", ("go", "golang")),
                        ("rust", ("rust",)), ("node", ("node", "nodejs", "next", "react")),
                        ("python", ("python",))):
        if hint in words:
            return lang, f"cicd.json STACK={hint}"
    files = e2e.gh_list_dir(repo, "", ref)
    for lang, match in _MARKERS:
        hits = [f for f in files if match(f)]
        if hits:
            return lang, f"file {', '.join(hits[:3])}"
    name, text = _dockerfile(repo, ref)
    if text:
        images = _dockerfile_facts(text)["images"]
        for lang, prefixes in _IMAGE_LANG:
            hit = next((img for img in images if any(p in img.lower() for p in prefixes)), "")
            if hit:
                return lang, f"{name} FROM {hit}"
    try:
        langs = e2e.gh_get(f"/repos/{repo}/languages") or {}
    except github_api.GitHubAPIError:
        langs = {}
    for name_ in sorted(langs, key=langs.get, reverse=True):
        if name_ in _GH_LANG:
            return _GH_LANG[name_], f"GitHub languages ({name_})"
    return "go", "default (tidak ada penanda)"


def detect_dotnet_env_mode(repo: str, ref: str, rules: dict) -> tuple:
    """(mode, bukti): `env` bila source membaca env var environment langsung
    (`GetEnvironmentVariable("ASPNETCORE_ENVIRONMENT")`), selain itu default (`args`).
    Pola & file sumber dari params rule `dotnet_env_mode` di PB."""
    from . import e2e, github_api, manifest_check as mc

    params = (rules.get("dotnet_env_mode") or mc.DEFAULT_RULES["dotnet_env_mode"])["params"]
    wanted = tuple(params.get("source_files") or ("Program.cs", "Startup.cs"))
    sources = []
    for name in wanted:
        try:
            sources.append((name, e2e.gh_raw(repo, name, ref)))
        except github_api.GitHubAPIError:
            continue
    if not sources:  # proyek di subfolder (mis. src/Api/Program.cs)
        try:
            tree = e2e.gh_get(f"/repos/{repo}/git/trees/{quote(ref, safe='')}?recursive=1") or {}
        except github_api.GitHubAPIError:
            tree = {}
        paths = [t["path"] for t in tree.get("tree") or [] if t.get("type") == "blob"
                 and posixpath.basename(t["path"]) in wanted and "test" not in t["path"].lower()]
        for path in sorted(paths, key=len)[:4]:
            try:
                sources.append((path, e2e.gh_raw(repo, path, ref)))
            except github_api.GitHubAPIError:
                continue
    read_only = []
    for name, text in sources:
        for pattern in params.get("inline_logic_patterns") or []:
            hit = re.search(pattern, text)
            if hit:
                return "env", f"{name}: env var dipakai langsung dlm logika — {hit.group(0)[:70]}"
        for pattern in params.get("env_patterns") or []:
            for hit in re.finditer(pattern, text):
                var = re.search(r"(\w+)\s*=\s*[^=;]*$", text[max(0, hit.start() - 80):hit.start()])
                if not var:
                    read_only.append(f"{name}: {hit.group(0)}")
                    continue
                for logic in params.get("logic_patterns") or []:
                    used = re.search(logic.replace("{var}", re.escape(var.group(1))), text)
                    if used:
                        return "env", (f"{name}: {var.group(1)} = {hit.group(0)} dipakai dlm logika "
                                       f"({used.group(0).strip()})")
                read_only.append(f"{name}: {var.group(1)} = {hit.group(0)} hanya utk AddJsonFile/log")
    default = params.get("default_mode", "args")
    if read_only:
        return default, (f"{read_only[0]} — redundan dgn host default yg membaca --environment → args")
    if sources:
        return default, f"{', '.join(n for n, _ in sources)} tanpa baca env var langsung → --environment"
    return default, "Program.cs/Startup.cs tidak ditemukan → default"


def config_dir(facts: dict) -> str:
    """Direktori yang dibaca aplikasi utk file config: tujuan `COPY … .env|appsettings` di stage final,
    selain itu WORKDIR stage final (default "/"). "" bila tak bisa dipastikan ($VAR)."""
    if facts.get("config_copy"):
        return posixpath.dirname(facts["config_copy"]) or "/"
    return facts.get("workdir", "")


def uses_dotnet_env(repo: str, ref: str, files: List[str], packages: List[str]) -> bool:
    """Proyek .NET memakai paket pembaca .env (DotNetEnv / dotenv.net) di *.csproj root."""
    from . import e2e, github_api

    for name in [f for f in files if f.endswith(".csproj")][:3]:
        try:
            text = e2e.gh_raw(repo, name, ref)
        except github_api.GitHubAPIError:
            continue
        if any(re.search(rf'Include="{re.escape(p)}"', text, re.I) for p in packages):
            return True
    return False


def config_stack(language: str) -> str:
    """Format file config: dotnet → appsettings.<env>.json, lainnya → .env."""
    return "dotnet" if language == "dotnet" else "go"

def init(
    repo: str,
    target: str,
    *,
    ns: Optional[str] = None,
    infra: Optional[str] = None,
    gitops_repo: Optional[str] = None,
    gitops_branch: Optional[str] = None,
    config: Optional[dict] = None,
) -> InitResult:
    from . import e2e, github_api, manifest_check as mc, manifest_scan
    from ._common import fetch_cicd_data
    from .config import load_config
    from .rollout import compute_rollout

    # Format utama: <repo> <namespace> (mis. production-qoin) → env = prefix namespace.
    # Kompatibel: <repo> <env> → namespace dari cicd.json PROJECT (atau --ns).
    target = (target or "").strip()
    if target in ENVS:
        env = target
    else:
        if not re.fullmatch(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?", target):
            raise ManifestInitError(f"namespace tidak valid: {target!r} (contoh: production-qoin)")
        env = mc.env_from_namespace(target)
        if env not in ENVS:
            raise ManifestInitError(
                f"prefix namespace {target!r} bukan env ({' | '.join(ENVS)}), contoh: develop-qoin")
        ns = ns or target
    config = config or load_config()
    if gitops_repo:
        config = {**config, "gitops": {**config["gitops"], "repo": gitops_repo}}
    repo = github_api.normalize_repo(repo)
    if "/" not in repo:
        org = config["git"]["org"] or config["gitops"]["repo"].split("/")[0]
        repo = f"{org}/{repo}"
    short = repo.split("/")[-1]

    fallback_note = ""
    try:
        ref = (_latest_tag(repo) if env == "production" else env) or ""
        try:
            if not ref:
                raise github_api.GitHubAPIError("belum ada tag v*")
            sha = e2e.gh_get(f"/repos/{repo}/commits/{quote(ref, safe='')}")["sha"]
        except github_api.GitHubAPIError as e:
            # Aplikasi baru: branch env / tag belum ada → baca dari branch default repo.
            default = (e2e.gh_get(f"/repos/{repo}") or {}).get("default_branch") or "main"
            missing = f"branch {ref}" if ref else "tag v*"
            fallback_note = (f"{missing} belum ada di repo — dibaca dari branch default {default}; "
                             f"tag image berubah setelah pipeline {env} berjalan")
            ref = default
            sha = e2e.gh_get(f"/repos/{repo}/commits/{quote(ref, safe='')}")["sha"]
    except github_api.GitHubAPIError as e:
        raise ManifestInitError(f"repo {repo} tidak bisa dibaca: {str(e)[:150]}") from e
    cicd, cicd_path, _ = fetch_cicd_data(repo, ref, "cicd/cicd.json")
    cicd = cicd or {}
    org = config["dockerhub"]["org"] or config["registry"]["url"] or "loyaltolpi"
    image = f"{org}/{cicd.get('IMAGE') or short}:{e2e.expected_tag(ref, sha)}"
    # compute_rollout menurunkan ENV dari ref → pakai ref env (bukan branch default fallback).
    env_ref = "v0.0.0" if env == "production" else env
    r = compute_rollout(cicd=cicd, repo=short, ref=env_ref, image_tag=image, config=config, ns=ns, infra=infra)
    path, app = r["gitops_path"], r["deployment"]
    folder = posixpath.dirname(path)
    infra_final, folder_ns = folder.split("/", 1)[0], folder.split("/", 1)[-1]
    gbranch = gitops_branch or e2e.gitops_branch_for(env, config["gitops"]["branch"])
    port = int(cicd["PORT"]) if str(cicd.get("PORT", "")).isdigit() else 80
    language, lang_evidence = detect_language(repo, ref, cicd)
    stack = config_stack(language)
    df_name, df_text = _dockerfile(repo, ref)
    facts = _dockerfile_facts(df_text) if df_text else {}
    grepo = config["gitops"]["repo"]

    res = InitResult(repo=repo, env=env, ref=ref, image=image, gitops_repo=grepo, gitops_branch=gbranch,
                     path=path, namespace=folder_ns, stack=stack, port=port)
    res.details.append(f"source {repo}@{ref} ({sha[:7]}) · cicd.json: {cicd_path or 'TIDAK ditemukan'}")
    res.language = language
    res.details.append(f"bahasa: {language} ({lang_evidence}) → file config "
                       f"{'appsettings.<env>.json' if stack == 'dotnet' else '.env'}")
    if facts:
        res.details.append(f"{df_name}: FROM {facts['final_image'] or '?'}"
                           + (f", WORKDIR {facts['workdir']}" if facts["workdir"] else "")
                           + (", ENTRYPOINT/CMD ada" if facts["entrypoint"] else ""))
    if fallback_note:
        res.details.append(fallback_note)
    if r["ns_from_fallback"] and not cicd.get("PROJECT"):
        res.details.append(f"namespace dari NS_SUFFIX ({folder_ns}) — cicd.json tanpa PROJECT; override --ns")

    rules, _ = mc.load_rules()
    env_mode = None
    if stack == "dotnet":
        env_mode, mode_evidence = detect_dotnet_env_mode(repo, ref, rules)
        res.details.append(f"mode environment .NET: {env_mode} ({mode_evidence})")
    suffixes = (rules.get("service_missing") or mc.DEFAULT_RULES["service_missing"])["params"]["suffixes"]
    kz, kz_note = e2e._fetch_kustomization(grepo, folder, gbranch)
    res.kustomization_path = f"{folder}/kustomization.yaml"
    if kz_note:
        res.details.append(kz_note)
    res.namespace = (kz or {}).get("namespace") or folder_ns
    if res.namespace != folder_ns:
        res.details.append(f"namespace efektif: {res.namespace} (folder {folder_ns})")

    try:
        existing = e2e.gh_raw(grepo, path, gbranch)
    except github_api.GitHubAPIError:
        existing = None
    secrets, secret_src = manifest_scan._namespace_secrets(
        res.namespace, [app], {}, config, infra_final, res.details)
    res.details.append(f"secret: {secret_src}")
    if secrets is not None and not secrets:
        res.details.append(f"namespace {res.namespace} belum punya Secret apa pun (mungkin belum dibuat) — "
                           "siapkan namespace + imagePullSecret regcred sebelum deploy")
    if infra_final == "cce" and not mc.node_selector(rules, app, res.namespace)[1]:
        res.details.append(f"nodepool CCE untuk {res.namespace} belum ada di params rule nodepool_mismatch_cce "
                           "— isi nodeSelector manual atau tambahkan ke seed")
    in_repo = None
    if secrets is None:
        in_repo = set(e2e.gh_list_dir(grepo, folder, gbranch)) | set(
            e2e.gh_list_dir(grepo, f"{folder}/file-config", gbranch))

    deploy = mc.find_kind(mc.parse_yaml(existing), "Deployment") if existing else None
    if deploy is not None:
        # Sudah ada → cek seperti tahap Manifest e2e + saran perbaikan.
        res.mode = "existing"
        docs = mc.parse_yaml(existing)
        service, spath = mc.find_kind(docs, "Service"), path
        if service is None:
            service, spath = e2e._fetch_service(grepo, path, gbranch, suffixes)
        sample = (e2e._manifest_sample(grepo, folder, app, rules)
                  if service is None and mc._container_ports(deploy) else None)
        check = mc.check(rules, deploy=deploy, service=service, app=app, ns=res.namespace,
                         deploy_path=path, service_path=spath, cicd=cicd,
                         secrets_in_cluster=secrets, secrets_in_repo=in_repo, kustomization=kz, sample=sample)
        res.origin = f"manifest yang sudah ada di {grepo}@{gbranch}"
        res.findings = [vars(f) for f in check.findings]
        res.secrets = dict(check.secrets)
        res.details += check.notes
        res.suggestions = list(check.suggestions)
        # Butuh source (Dockerfile/csproj): path & format file config; saran mountPath di depan.
        res.findings += _source_config_findings(rules, deploy, app, repo, ref, language, facts,
                                                df_name, path, res)
        return res

    sample = e2e._manifest_sample(grepo, folder, app, rules)
    if sample:
        new_deploy = mc.adapt_sample_deployment(
            rules, sample, app=app, ns=res.namespace, image=image, port=port, stack=stack,
            infra=infra_final, kz=kz, config_mount_dir=config_dir(facts) or None, env_mode=env_mode)
        # args `dotnet <app>.dll --environment=<env>` mengikuti template: `--environment` memilih
        # appsettings.<env>.json, jadi TIDAK dibuang walau image punya ENTRYPOINT.
        new_service = mc.adapt_sample_service(sample, new_deploy)
        res.origin = f"sample {sample['path']}"
    else:
        new_deploy = mc.suggest_deployment(rules, app=app, ns=res.namespace, image=image, port=port,
                                           stack=stack, infra=infra_final)
        if stack == "dotnet" and env_mode:
            mc.apply_env_mode(mc._containers(new_deploy)[0], env_mode, app, mc.env_from_namespace(res.namespace))
        new_service = mc.suggest_service(new_deploy)
        res.origin = "generator standar (sample tidak ditemukan)"
    service_path = path.replace("_deployment.yaml", suffixes[0])
    res.suggestions = [
        {"title": f"Deployment baru ({res.origin})", "path": path, "yaml": mc.dump_yaml(new_deploy)},
        {"title": f"Service baru ({res.origin})", "path": service_path, "yaml": mc.dump_yaml(new_service)},
    ]
    for name in mc.referenced_secrets(new_deploy):
        state = ("ada" if name in secrets else "tidak-ada") if secrets is not None else (
            "repo" if in_repo and mc.secret_in_repo(name, mc.env_from_namespace(res.namespace), in_repo)
            else "tidak-diketahui")
        res.secrets[name] = state
        if state != "ada":
            res.suggestions.append(mc._secret_suggestion(
                rules, name=name, app=app, ns=res.namespace, stack=stack, infra=infra_final,
                env_name=mc.runtime_env(mc._containers(new_deploy)[0])[1] or None))
    files = [posixpath.basename(path), posixpath.basename(service_path)]
    if kz is None:
        res.suggestions.append({
            "title": "kustomization.yaml baru (folder belum ada)", "path": res.kustomization_path,
            "yaml": mc.dump_yaml({"apiVersion": "kustomize.config.k8s.io/v1beta1", "kind": "Kustomization",
                                  "namespace": res.namespace, "resources": files}),
        })
    elif kz.get("resources") is not None:
        res.kustomization_add = [f for f in files if f not in kz["resources"]]
    return res


def _source_config_findings(rules: dict, deploy: dict, app: str, repo: str, ref: str, language: str,
                            facts: dict, df_name: str, path: str, res: "InitResult") -> List[dict]:
    """Temuan yang butuh source: path & format file config yang di-mount vs yang dibaca aplikasi."""
    from . import e2e, manifest_check as mc

    out: List[dict] = []
    prefix = rules.get("missing_file_config_volume", mc.DEFAULT_RULES["missing_file_config_volume"])["params"]["secret_prefix"]
    pod = mc._pod(deploy)
    fc = {v.get("name") for v in pod.get("volumes") or []
          if str((v.get("secret") or {}).get("secretName", "")).startswith(prefix)}
    mounts = [m for m in mc._containers(deploy)[0].get("volumeMounts") or [] if m.get("name") in fc]
    if not mounts:
        return out
    want_dir = config_dir(facts) if facts else ""

    def add(rule: str, message: str) -> None:
        r = rules.get(rule) or mc.DEFAULT_RULES.get(rule)
        if r:
            out.append({"rule_name": rule, "severity": r["severity"], "category": r["category"],
                        "message": message, "fix_hint": r["fix_hint"]})

    if want_dir and rules.get("config_mount_path_mismatch", mc.DEFAULT_RULES["config_mount_path_mismatch"]):
        dirs = {posixpath.dirname(m["mountPath"]) or "/" for m in mounts}
        if want_dir not in dirs:
            add("config_mount_path_mismatch",
                f"config di-mount di {', '.join(m['mountPath'] for m in mounts)} tapi aplikasi membaca dari "
                f"{want_dir} ({df_name}: {'COPY → ' + facts['config_copy'] if facts.get('config_copy') else 'WORKDIR ' + want_dir})")
            fixed = mc.copy.deepcopy(deploy)
            for m in mc._containers(fixed)[0].get("volumeMounts") or []:
                if m.get("name") in fc:
                    m["mountPath"] = posixpath.join(want_dir, posixpath.basename(m["mountPath"]))
            res.suggestions.insert(0, {"title": f"Deployment (mountPath config → {want_dir}, sesuai {df_name})",
                                       "path": path, "yaml": mc.dump_yaml(fixed)})
    subs = [posixpath.basename(m.get("subPath") or m["mountPath"]) for m in mounts]
    packages = (rules.get("config_format_mismatch") or mc.DEFAULT_RULES["config_format_mismatch"])["params"][
        "dotnet_env_packages"]
    if language == "dotnet" and any(s.startswith(".env") for s in subs):
        if not uses_dotnet_env(repo, ref, e2e.gh_list_dir(repo, "", ref), packages):
            add("config_format_mismatch",
                f"aplikasi .NET me-mount {', '.join(subs)} tanpa paket {'/'.join(packages)} — .NET membaca "
                "appsettings.<env>.json, file .env tidak pernah terbaca")
    elif language not in ("dotnet", "krakend") and any(s.startswith("appsettings") for s in subs):
        add("config_format_mismatch", f"aplikasi {language} me-mount {', '.join(subs)} — yang dibaca .env")
    return out


# ─── Render & tulis ──────────────────────────────────────────────────────

def render_text(res: InitResult) -> str:
    head = "🧱 Manifest init" if res.mode == "new" else "🔎 Manifest sudah ada — hasil cek"
    lines = [f"{head}: {res.repo} ({res.env}) → {res.gitops_repo}@{res.gitops_branch}:{res.path}",
             f"   image {res.image} · namespace {res.namespace} · bahasa {res.language or res.stack} · port {res.port}",
             f"   dasar YAML: {res.origin}"]
    lines += [f"   • {d}" for d in res.details]
    for f in res.findings:
        lines.append(f"   [{f['severity']}] {f['rule_name']}: {f['message']}")
    if res.mode == "existing" and not res.findings:
        lines.append("   ✅ selector Deployment↔Service cocok, Secret tersedia — tidak ada saran")
    for name, state in res.secrets.items():
        lines.append(f"   secret {name}: {state}")
    for sug in res.suggestions:
        lines += ["", f"📝 {sug['title']}", f"# {sug['path']}", sug["yaml"].rstrip()]
    if res.kustomization_add:
        lines += ["", f"📌 Tambahkan ke resources {res.kustomization_path}:"]
        lines += [f"  - {f}" for f in res.kustomization_add]
    if res.mode == "new":
        lines += ["", "💡 Simpan file di atas ke repo GitOps lewat PR (branch "
                  f"{res.gitops_branch}); isi Secret dari file config & jangan commit plaintext."]
    return "\n".join(lines)


def render_markdown(res: InitResult) -> str:
    md = [f"# Manifest init — `{res.repo}` ({res.env})", "",
          "| | |", "|---|---|",
          f"| Path GitOps | `{res.gitops_repo}@{res.gitops_branch}:{res.path}` |",
          f"| Image | `{res.image}` |",
          f"| Namespace | `{res.namespace}` |",
          f"| Stack / port | {res.stack} / {res.port} |",
          f"| Mode | {'baru' if res.mode == 'new' else 'sudah ada (hasil cek)'} |",
          f"| Dasar YAML | {res.origin} |", ""]
    md += [f"- {d}" for d in res.details]
    md += [f"- **[{f['severity']}] {f['rule_name']}** — {f['message']}" for f in res.findings]
    md += [f"- secret `{n}`: {s}" for n, s in res.secrets.items()]
    for sug in res.suggestions:
        md += ["", f"## {sug['title']}", "", f"`{sug['path']}`", "", "```yaml", sug["yaml"].rstrip(), "```"]
    if res.kustomization_add:
        md += ["", f"## Tambahan resources `{res.kustomization_path}`", "", "```yaml"]
        md += [f"  - {f}" for f in res.kustomization_add] + ["```"]
    return "\n".join(md) + "\n"


def write_files(res: InitResult, out_dir: str) -> List[Path]:
    """Tulis tiap saran YAML ke `<out_dir>/<path GitOps>` (struktur sama dgn repo GitOps)."""
    written = []
    for sug in res.suggestions:
        target = Path(out_dir).expanduser() / sug["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(sug["yaml"], encoding="utf-8")
        written.append(target)
    return written


# ─── Verifikasi leave-one-out: hasil generate vs manifest yang sudah ada ───────

CORE_LABELS = ("app", "env", "project", "role")


def core_deployment(doc: dict) -> dict:
    """Field inti Deployment yang menentukan perilaku runtime (tag image diabaikan)."""
    from . import manifest_check as mc

    pod = mc._pod(doc)
    c0 = (pod.get("containers") or [{}])[0]
    secret_vols = {v.get("name"): (v.get("secret") or {}).get("secretName")
                   for v in pod.get("volumes") or [] if v.get("secret")}
    labels = mc._pod_labels(doc)
    return {
        "name": (doc.get("metadata") or {}).get("name"),
        "selector": mc._match_labels(doc),
        "labels": {k: labels[k] for k in CORE_LABELS if k in labels},
        "container": c0.get("name"),
        "image_repo": str(c0.get("image", "")).rsplit(":", 1)[0],
        "args": c0.get("args") or [],
        "env_runtime": mc.runtime_env(c0),
        "ports": sorted((p.get("containerPort"), p.get("name"), p.get("protocol", "TCP"))
                        for p in c0.get("ports") or []),
        "secret_volumes": sorted(v for v in secret_vols.values() if v),
        "secret_mounts": sorted((m.get("mountPath"), m.get("subPath"))
                                for m in c0.get("volumeMounts") or [] if m.get("name") in secret_vols),
        "pull_secrets": sorted(s.get("name") for s in pod.get("imagePullSecrets") or []),
        "node_selector": pod.get("nodeSelector") or {},
        "replicas": (doc.get("spec") or {}).get("replicas", 1),
    }


def core_service(doc: dict, deploy: dict) -> dict:
    """Perilaku Service thd Deployment-nya: pod yg dipilih & port tujuan (nama port di-resolve)."""
    from . import manifest_check as mc

    spec = doc.get("spec") or {}
    named = {p.get("name"): p.get("containerPort") for p in mc._container_ports(deploy) if p.get("name")}
    selector = spec.get("selector") or {}

    def target(p: dict):
        tp = p.get("targetPort", p.get("port"))
        return named.get(tp, tp) if isinstance(tp, str) and not tp.isdigit() else int(tp)

    return {
        "name": (doc.get("metadata") or {}).get("name"),
        "type": spec.get("type", "ClusterIP"),
        "selects_pods": bool(selector) and mc._subset(selector, mc._pod_labels(deploy)),
        "ports": sorted((p.get("port"), str(target(p)), p.get("protocol", "TCP")) for p in spec.get("ports") or []),
    }


def core_diff(expected: dict, actual: dict) -> List[str]:
    return [f"{k}: asli={expected[k]!r} ≠ generate={actual.get(k)!r}"
            for k in expected if expected[k] != actual.get(k)]


@dataclass
class VerifyItem:
    app: str
    sample: str = ""
    deploy_diff: List[str] = field(default_factory=list)   # kesalahan generator
    service_diff: List[str] = field(default_factory=list)  # kesalahan generator
    nonstandard: List[str] = field(default_factory=list)   # manifest asli menyimpang dari standar
    skipped: str = ""

    @property
    def match(self) -> bool:
        return not self.skipped and not self.deploy_diff and not self.service_diff

    @property
    def status(self) -> str:
        if self.skipped:
            return "unique" if self.skipped.startswith("unik") else "skip"
        if not self.match:
            return "diff"
        return "match-nonstandard" if self.nonstandard else "match"


def _standard_core(rules: dict, app: str, ns: str, real: dict, kz: Optional[dict]) -> dict:
    """Nilai field inti yang DIHARAPKAN standar (untuk menilai apakah manifest asli menyimpang)."""
    from . import manifest_check as mc

    std_labels = mc.standard_labels(app, ns)
    label, pool = mc.node_selector(rules, app, ns)
    prefix = rules["missing_file_config_volume"]["params"]["secret_prefix"]
    env = mc.env_from_namespace(ns)
    vols = real["secret_volumes"]
    allowed = (f"{prefix}{app}", f"{prefix}{app}-{env}")
    std_vols = vols if all(v in allowed for v in vols) else [f"{prefix}{app}"]
    return {
        "labels": std_labels,
        "selector": std_labels,
        "container": app,
        "node_selector": {**{k: v for k, v in real["node_selector"].items()
                             if k not in (label, "cce.cloud.com/cce-nodepool")}, label: pool}
                         if pool else real["node_selector"],
        "replicas": 2 if env == "production" else 1,
        "secret_volumes": std_vols,
    }


def verify_folder(namespace: str, *, branch: str = "main", infra: str = "cce",
                  config: Optional[dict] = None) -> List[VerifyItem]:
    """Untuk tiap Deployment di `<branch>:<infra>/<namespace>/`: anggap belum ada, generate
    dari sample tetangga (app itu sendiri dikecualikan), bandingkan field inti dgn aslinya
    (keduanya setelah patch kustomization)."""
    from . import e2e, manifest_check as mc
    from .config import load_config

    config = config or load_config()
    repo, folder = config["gitops"]["repo"], f"{infra}/{namespace}"
    rules, _ = mc.load_rules()
    rules = {**rules, mc.SAMPLE_RULE: {**rules.get(mc.SAMPLE_RULE, mc.DEFAULT_RULES[mc.SAMPLE_RULE]),
                                        "params": {**mc.sample_source(rules), "branch": branch, "infra": infra}}}
    suffixes = rules["service_missing"]["params"]["suffixes"]
    files = e2e.gh_list_dir(repo, folder, branch)
    if not files:
        raise ManifestInitError(f"folder {folder} tidak ada di {repo}@{branch}")
    kz, _ = e2e._fetch_kustomization(repo, folder, branch)
    cache: dict = {}
    items: List[VerifyItem] = []
    for app in sorted(f.removesuffix("_deployment.yaml") for f in files if f.endswith("_deployment.yaml")):
        item = VerifyItem(app)
        items.append(item)
        path = f"{folder}/{app}_deployment.yaml"
        for attempt in (1, 2):  # gangguan jaringan sesaat ke GitHub → coba ulang sekali
            try:
                deploy = mc.find_kind(mc.parse_yaml(e2e.gh_raw(repo, path, branch)), "Deployment")
                service, _ = e2e._fetch_service(repo, path, branch, suffixes)
                break
            except Exception as e:
                if attempt == 2:
                    deploy, service, item.skipped = None, None, f"gagal membaca manifest: {str(e)[:80]}"
        if item.skipped:
            continue
        if deploy is None or service is None:
            item.skipped = "Deployment/Service asli tidak lengkap"
            continue
        ns = (kz or {}).get("namespace") or (deploy.get("metadata") or {}).get("namespace") or namespace
        # Leave-one-out: template yg dibangun dari aplikasi ini sendiri tidak boleh dipakai.
        sample = e2e._manifest_sample(repo, folder, app, rules, cache=cache, exclude_template_of=app)
        if sample is None:
            item.skipped = "tidak ada sample tetangga yang lolos"
            continue
        item.sample = sample.get("template") or sample["app"]
        if sample["app"] != mc.TEMPLATE_APP and sample["app"].rsplit("-", 1)[-1] != app.rsplit("-", 1)[-1]:
            item.skipped = f"unik — tidak ada aplikasi sejenis (-{app.rsplit('-', 1)[-1]}) sebagai sample"
            continue
        # Data khas aplikasi (di init nyata dari cicd.json/Dockerfile) diberikan sebagai input.
        fc_prefix = rules["missing_file_config_volume"]["params"]["secret_prefix"]
        fc_vols = {v.get("name") for v in mc._pod(deploy).get("volumes") or []
                   if str((v.get("secret") or {}).get("secretName", "")).startswith(fc_prefix)}
        c0 = mc._containers(deploy)[0]
        fc_paths = [m.get("mountPath") for m in c0.get("volumeMounts") or [] if m.get("name") in fc_vols]
        gen = mc.adapt_sample_deployment(
            rules, sample, app=app, ns=ns, image=c0.get("image", ""),
            ports=mc._container_ports(deploy) or None, stack=mc.detect_stack(deploy), infra=infra, kz=kz,
            config_mount_paths=fc_paths or None, dotnet_args=bool(c0.get("args")),
            config_secret=bool(fc_vols),
            # mode env .NET = keputusan source aplikasi (di init nyata dari Program.cs)
            env_mode=(mc.runtime_env(c0)[0] or "none") if mc.detect_stack(deploy) == "dotnet" else None)
        real_svc_core = core_service(service, mc.apply_patches(deploy, kz, "Deployment")[0])
        gen_svc = mc.adapt_sample_service(
            sample, gen, service_type=(service.get("spec") or {}).get("type", "ClusterIP"),
            exposed_ports=[int(t) for _, t, _ in real_svc_core["ports"] if t.isdigit()] or None)
        real_eff, _ = mc.apply_patches(deploy, kz, "Deployment")
        gen_eff, _ = mc.apply_patches(gen, kz, "Deployment")
        real_core, gen_core = core_deployment(real_eff), core_deployment(gen_eff)
        std = _standard_core(rules, app, ns, real_core, kz)
        for d in core_diff(real_core, gen_core):
            key = d.split(":", 1)[0]
            # Manifest asli menyimpang dari standar → generator benar (standar), bukan diff.
            (item.nonstandard if key in std and real_core[key] != std[key] else item.deploy_diff).append(d)
        real_svc, gen_svc_core = core_service(service, real_eff), core_service(gen_svc, gen_eff)
        same_targets = [t for _, t, _ in real_svc["ports"]] == [t for _, t, _ in gen_svc_core["ports"]]
        for d in core_diff(real_svc, gen_svc_core):
            key = d.split(":", 1)[0]
            # Tujuan port sama, hanya nomor port klien beda = konvensi port klien minoritas.
            nonstd = (key == "selects_pods" and not real_svc["selects_pods"]) or (
                key == "ports" and (same_targets or any(not t.isdigit() for _, t, _ in real_svc["ports"])))
            (item.nonstandard if nonstd else item.service_diff).append(d)
    return items


VERIFY_ICON = {"match": "✅", "match-nonstandard": "✅*", "diff": "❌", "unique": "ℹ️ ", "skip": "⏭️ "}


def render_verify(namespace: str, branch: str, items: List[VerifyItem]) -> str:
    import collections

    c = collections.Counter(i.status for i in items)
    lines = [f"🧪 Verifikasi --manifest-init (leave-one-out): {namespace} @ {branch} — {len(items)} Deployment",
             f"   ✅ match {c['match']} · ✅* match (asli tidak standar) {c['match-nonstandard']} · "
             f"❌ diff {c['diff']} · ℹ️  unik {c['unique']} · ⏭️  skip {c['skip']}"]
    for i in items:
        if i.status == "match":
            continue
        lines.append(f"{VERIFY_ICON[i.status]} {i.app} (sumber: {i.sample or '-'}) {i.skipped}".rstrip())
        lines += [f"     ❌ {d}" for d in i.deploy_diff + [f"svc {x}" for x in i.service_diff]]
        lines += [f"     * {d}" for d in i.nonstandard]
    return "\n".join(lines)


def verify_to_dict(namespace: str, branch: str, items: List[VerifyItem]) -> dict:
    from dataclasses import asdict

    return {"namespace": namespace, "branch": branch,
            "items": [{**asdict(i), "status": i.status} for i in items],
            "exit_code": 1 if any(i.status == "diff" for i in items) else 0}
