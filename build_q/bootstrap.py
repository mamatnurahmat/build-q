"""`bq --bootstrap-k8s <repo> <ref> --gitops-repo <r> --gitops-branch <b> --path-yaml <p>`

One-shot bootstrap manifest K8s ke repo GitOps:
  1. fetch `cicd/cicd.json` dari repo source (native REST)
  2. deteksi stack (dotnet vs default: go/node/rust) via cicd hint / probing config file
  3. fetch config file: appsettings.{DotnetEnv}.json  (dotnet) atau .env.{env} / .env (default)
  4. clone gitops repo shallow → branch baru → render 3 YAML
  5. commit → push → open PR ke gitops_branch

Selector deployment & service auto-match (single `app: {{APP}}` placeholder), tidak
mungkin drift. Path output:
  {path_yaml}/file-config/{app}-{env}.yaml   ← Secret
  {path_yaml}/{app}_deployment.yaml          ← Deployment (mount secretRef)
  {path_yaml}/{app}_services.yaml            ← Service (selector match)
"""
from __future__ import annotations

import base64
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import github_api
from ._common import fetch_cicd_data
from .config import cicd_candidates, load_config
from .templates import load_template, render


_TAG_LIKE = re.compile(r"^v\d")


def _derive_env(refs: str) -> str:
    """Map git ref → environment name (develop/staging/production)."""
    if refs in ("develop",):
        return "develop"
    if refs in ("staging",):
        return "staging"
    if refs in ("main", "master") or _TAG_LIKE.match(refs or ""):
        return "production"
    return refs


def _dotnet_env(env: str) -> str:
    """develop→Development, staging→Staging, production→Production."""
    return {
        "develop": "Development",
        "staging": "Staging",
        "production": "Production",
    }.get(env, env.capitalize())


def _ref_id_for_image_tag(ref: str, sha: str) -> str:
    """Match Makefile IMAGE_TAG: tag jika HEAD di tag, else short-sha."""
    if ref and _TAG_LIKE.match(ref):
        return ref
    return sha[:7]


def _sh(cmd: List[str], *, cwd: Optional[str] = None, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, check=check, capture_output=True, text=True)


def _preflight(config: Dict) -> Optional[str]:
    """Return error string bila prasyarat kurang, None kalau OK."""
    gh = config["github"]
    if gh["use_cli"]:
        return ("GH_CLI=true — --bootstrap-k8s butuh jalur native. "
                "Set GH_CLI=false di ~/.build-q/.env.")
    if not gh["token"]:
        return "GITHUB_TOKEN kosong di ~/.build-q/.env."
    return None


def _detect_stack_and_fetch_config(
    api_repo: str, ref: str, env: str, cicd: Dict,
) -> Tuple[str, str, bytes]:
    """Return (stack, config_path_in_source, config_raw_bytes).

    Urutan deteksi:
      1. Hint eksplisit dari cicd.json (`STACK` / `TYPE` = dotnet / go / node / rust)
      2. Probe `appsettings.{DotnetEnv}.json` — kalau ada → dotnet
      3. Probe `.env.{env}` → default
      4. Probe `.env` → default
      5. Probe `.env.example` → default (fallback template — biasa untuk Rust)
    Raise ValueError bila tidak ada yg ketemu.
    """
    hint = str(cicd.get("STACK") or cicd.get("TYPE") or "").lower().strip()
    dotnet_env = _dotnet_env(env)

    # 1. Hint eksplisit
    if hint == "dotnet":
        candidates = [f"appsettings.{dotnet_env}.json"]
    elif hint in ("go", "node", "nodejs", "rust", "default"):
        candidates = [f".env.{env}", ".env", ".env.example"]
    else:
        # 2/3/4/5. Probe berurutan: dotnet dulu, lalu default
        candidates = [
            f"appsettings.{dotnet_env}.json",
            f".env.{env}",
            ".env",
            ".env.example",
        ]

    for cand in candidates:
        try:
            raw = github_api.get_contents_raw(api_repo, cand, ref)
        except github_api.GitHubAPIError:
            continue
        if not raw:
            continue
        stack = "dotnet" if cand.startswith("appsettings.") else "default"
        return stack, cand, raw

    raise ValueError(
        f"Config file tidak ditemukan di {api_repo}@{ref}. "
        f"Coba (berurutan): {', '.join(candidates)}"
    )


def _update_kustomization(
    repo_dir: Path, path_yaml: str, app: str,
) -> Optional[str]:
    """Sisipkan `{app}_services.yaml` + `{app}_deployment.yaml` di `resources:`.

    Text-based (tanpa dep PyYAML). Idempotent — skip entries yg sudah ada.
    Return path relatif kustomization bila di-modify; None kalau tidak ada
    file / block resources / sudah include entries.
    """
    kus_rel = f"{path_yaml}/kustomization.yaml"
    kus_path = repo_dir / kus_rel
    if not kus_path.exists():
        print(f"   ⏭  {kus_rel} tidak ada — skip kustomization update")
        return None

    new_entries = [f"{app}_services.yaml", f"{app}_deployment.yaml"]
    lines = kus_path.read_text().split("\n")

    res_idx = next(
        (i for i, ln in enumerate(lines) if ln.rstrip() == "resources:"),
        None,
    )
    if res_idx is None:
        print(f"   ⏭  {kus_rel} tidak punya block `resources:` — skip")
        return None

    # Cari akhir list resources: entry berbentuk `  - <file>` boleh diselingi
    # komentar/blank. Stop saat ketemu baris non-item non-empty non-komentar.
    end_idx = len(lines)
    for i in range(res_idx + 1, len(lines)):
        ln = lines[i]
        stripped = ln.strip()
        if not stripped or stripped.startswith("#") or ln.startswith("  - "):
            continue
        end_idx = i
        break

    existing = {
        ln[len("  - "):].strip()
        for ln in lines[res_idx + 1:end_idx]
        if ln.startswith("  - ")
    }
    to_add = [e for e in new_entries if e not in existing]
    if not to_add:
        print(f"   ⏭  {kus_rel} sudah include {new_entries}")
        return None

    # Sisipkan tepat setelah entry terakhir di block resources.
    last_item_idx = res_idx
    for i in range(res_idx + 1, end_idx):
        if lines[i].startswith("  - "):
            last_item_idx = i

    insert = [f"  - {e}" for e in to_add]
    lines = lines[:last_item_idx + 1] + insert + lines[last_item_idx + 1:]
    kus_path.write_text("\n".join(lines))
    print(f"   ✏️  {kus_rel} (+{len(to_add)}: {', '.join(to_add)})")
    return kus_rel


def _recreate_deploy_if_selector_drift(
    app: str,
    namespace: str,
    ctx: Dict[str, str],
    env: str,
    kube_context: Optional[str],
    dry_run: bool,
) -> None:
    """Delete Deployment lama bila `spec.selector.matchLabels` drift dari ctx.

    K8s Deployment `spec.selector` **immutable** — kalau schema label berubah
    (mis. 1-tuple → 4-tuple), apply baru akan reject dengan
    `field is immutable`. Solusinya: delete Deployment → ArgoCD selfHeal
    (atau kubectl apply berikutnya) akan re-create dengan spec baru.

    Guardrail: TOLAK recreate saat env=production — recreate = downtime,
    production wajib procedure manual (blue-green / canary).
    """
    if env == "production":
        print(f"\n🛑 Skip --force-recreate-deploy: env=production wajib manual.")
        return

    ctx_args = ["--context", kube_context] if kube_context else []
    ctx_str = kube_context or "(current)"

    get_cmd = [
        "kubectl", *ctx_args, "-n", namespace, "get", "deploy", app,
        "-o", "jsonpath={.spec.selector.matchLabels}",
    ]
    result = subprocess.run(get_cmd, capture_output=True, text=True)
    if result.returncode != 0:
        # Deployment belum ada — nothing to recreate (fresh apply akan sukses)
        print(f"\n🔎 Cek selector drift ({ctx_str}/{namespace}) ...")
        print(f"   ℹ️  Deployment {app} belum ada — skip recreate check.")
        return

    try:
        current = json.loads(result.stdout.strip() or "{}")
    except json.JSONDecodeError:
        print(f"\n⚠️  Cek selector drift: gagal parse output kubectl — skip.")
        return

    rendered = {
        "app":     ctx["APP"],
        "env":     ctx["ENV"],
        "project": ctx["PROJECT"],
        "role":    ctx["ROLE"],
    }

    print(f"\n🔎 Cek selector drift ({ctx_str}/{namespace}) ...")
    if current == rendered:
        print(f"   ✅ selector match — no drift, skip recreate.")
        return

    print(f"   ⚠️  Drift terdeteksi (selector Deployment IMMUTABLE):")
    print(f"      cluster: {current}")
    print(f"      target : {rendered}")

    del_cmd = ["kubectl", *ctx_args, "-n", namespace, "delete", "deploy", app]
    if dry_run:
        print(f"   [dry-run] {' '.join(del_cmd)}")
        return

    print(f"   🗑️  Delete deployment lama ...")
    del_result = subprocess.run(del_cmd, capture_output=True, text=True)
    if del_result.returncode == 0:
        print(f"   ✅ deleted — ArgoCD selfHeal akan re-create dari GitOps.")
    else:
        print(
            f"   ⚠️  Delete gagal (non-fatal): {del_result.stderr.strip()}",
            file=sys.stderr,
        )


def _apply_secret_if_missing(
    repo_dir: Path,
    secret_rel_path: str,
    secret_name: str,
    namespace: str,
    env: str,
    kube_context: Optional[str],
    dry_run: bool,
) -> None:
    """kubectl apply file secret ke cluster HANYA bila belum ada.

    Guardrail: TOLAK apply untuk env=production — production wajib via GitOps
    (secret harus review + merge, tidak boleh ditulis langsung ke cluster).
    Failure mode lain (RBAC, network): log warning, tidak raise — jangan blok PR.
    """
    if env == "production":
        print(f"\n🛑 Skip --apply-secret: env=production wajib via GitOps (PR-only).")
        return

    ctx_args = ["--context", kube_context] if kube_context else []
    ctx_str = kube_context or "(current)"

    get_cmd = ["kubectl", *ctx_args, "-n", namespace, "get", "secret", secret_name]
    print(f"\n🔎 Cek secret di cluster ({ctx_str}/{namespace}) ...")
    if dry_run:
        print(f"   [dry-run] {' '.join(get_cmd)}")
        apply_cmd = ["kubectl", *ctx_args, "-n", namespace, "apply", "-f", secret_rel_path]
        print(f"   [dry-run] {' '.join(apply_cmd)}")
        return

    check = subprocess.run(get_cmd, capture_output=True, text=True)
    if check.returncode == 0:
        print(f"   ℹ️  {secret_name} sudah ada — skip apply.")
        return

    apply_cmd = [
        "kubectl", *ctx_args, "-n", namespace, "apply",
        "-f", str(repo_dir / secret_rel_path),
    ]
    print(f"   ✚ Secret belum ada — apply ...")
    result = subprocess.run(apply_cmd, capture_output=True, text=True)
    if result.returncode == 0:
        print(f"   ✅ {result.stdout.strip()}")
    else:
        print(f"   ⚠️  Apply gagal (non-fatal): {result.stderr.strip()}", file=sys.stderr)


def _resolve_image_tag(api_repo: str, ref: str, cicd: Dict, config: Dict) -> str:
    """`<registry>/<IMAGE>:<tag-or-sha>` — mirror rule Makefile."""
    registry = config.get("registry", {}).get("url", "") or "loyaltolpi"
    image_name = cicd.get("IMAGE") or api_repo.split("/")[-1]
    try:
        sha = github_api.get_commit_sha(api_repo, ref)
    except github_api.GitHubAPIError:
        sha = ""
    ref_id = _ref_id_for_image_tag(ref, sha) if sha else (ref or "latest")
    return f"{registry}/{image_name}:{ref_id}"


def _render_and_write(
    repo_dir: Path,
    path_yaml: str,
    ctx: Dict[str, str],
    stack: str,
) -> List[str]:
    """Render 3 template → tulis ke workdir. Return list path relatif yang di-write."""
    secret_tpl = "secret_dotnet" if stack == "dotnet" else "secret_default"
    deploy_tpl = "deployment_dotnet" if stack == "dotnet" else "deployment_default"

    outputs = [
        (f"{path_yaml}/file-config/{ctx['APP']}-{ctx['ENV']}.yaml", secret_tpl),
        (f"{path_yaml}/{ctx['APP']}_deployment.yaml",               deploy_tpl),
        (f"{path_yaml}/{ctx['APP']}_services.yaml",                 "services"),
    ]

    written: List[str] = []
    for rel_path, tpl_name in outputs:
        tpl = load_template(tpl_name)
        content = render(tpl, ctx)
        target = repo_dir / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        written.append(rel_path)
        print(f"   ✏️  {rel_path}")
    return written


def run_bootstrap_k8s(
    source_repo: str,
    refs: str,
    gitops_repo: str,
    gitops_branch: str,
    path_yaml: str,
    *,
    cicd_path: str = "cicd/cicd.json",
    dry_run: bool = False,
    keep_workdir: bool = False,
    replicas: int = 2,
    stack_override: Optional[str] = None,
    env_override: Optional[str] = None,
    pr_branch: Optional[str] = None,
    apply_secret: bool = False,
    kube_context: Optional[str] = None,
    image_pull_secret: str = "regcred",
    force_recreate_deploy: bool = False,
    namespace_override: Optional[str] = None,
    nodepool_override: Optional[str] = None,
) -> int:
    config = load_config()

    print("🔐 Preflight:")
    err = _preflight(config)
    if err:
        print(f"   ❌ {err}", file=sys.stderr)
        return 1
    print(f"   ✅ GITHUB_TOKEN ({len(config['github']['token'])} chars)")

    # 1. Env & namespace derivation
    env = env_override or _derive_env(refs)
    namespace = namespace_override or Path(path_yaml.rstrip("/")).name
    ns_note = " (override)" if namespace_override else " (dari path-yaml)"
    print(f"\n📐 Derived: env={env}  namespace={namespace}{ns_note}")

    # 2. Fetch cicd.json dari source repo
    print(f"\n📡 Fetch cicd config dari {source_repo}@{refs} ...")
    cicd, cicd_found, _ = fetch_cicd_data(source_repo, refs, cicd_path)
    if not cicd:
        print(
            f"   ❌ cicd config tidak ada / invalid "
            f"(tried: {', '.join(cicd_candidates(cicd_path))})",
            file=sys.stderr,
        )
        return 1
    print(f"   ✅ {cicd_found} — IMAGE={cicd.get('IMAGE')} PORT={cicd.get('PORT')}")

    # 3. Deteksi stack + fetch config file
    print(f"\n🔍 Deteksi stack + fetch config file dari {source_repo}@{refs} ...")
    try:
        if stack_override:
            dotnet_env_val = _dotnet_env(env)
            cand = (
                f"appsettings.{dotnet_env_val}.json" if stack_override == "dotnet"
                else f".env.{env}"
            )
            try:
                raw = github_api.get_contents_raw(source_repo, cand, refs)
            except github_api.GitHubAPIError:
                raw = b""
            if not raw and stack_override != "dotnet":
                try:
                    cand = ".env"
                    raw = github_api.get_contents_raw(source_repo, cand, refs)
                except github_api.GitHubAPIError:
                    raw = b""
            if not raw:
                raise ValueError(
                    f"Stack override='{stack_override}' — tapi config file "
                    f"tidak ditemukan di {source_repo}@{refs}"
                )
            stack, config_src_path, config_raw = stack_override, cand, raw
        else:
            stack, config_src_path, config_raw = _detect_stack_and_fetch_config(
                source_repo, refs, env, cicd,
            )
    except ValueError as e:
        print(f"   ❌ {e}", file=sys.stderr)
        return 1
    print(f"   ✅ stack={stack}  source={config_src_path}  size={len(config_raw)}B")

    # 4. Build render context
    app = cicd.get("DEPLOYMENT") or cicd.get("IMAGE") or source_repo.split("/")[-1]
    image_full = _resolve_image_tag(source_repo, refs, cicd, config)
    # NODETYPE=front → nodepool suffix `manager`; back → `service` (pola Qoin CCE)
    role = (cicd.get("NODETYPE") or "back").lower()
    nodepool_suffix = "manager" if role == "front" else "service"
    nodepool = nodepool_override or f"{namespace}-{nodepool_suffix}"
    ctx: Dict[str, str] = {
        "APP":               app,
        "ENV":               env,
        "NAMESPACE":         namespace,
        "DOTNET_ENV":        _dotnet_env(env),
        "REPLICAS":          str(replicas),
        "IMAGE_FULL":        image_full,
        "PORT":              str(cicd.get("PORT", "8080")),
        "PROJECT":           cicd.get("PROJECT") or "qoin",
        "ROLE":              role,
        "NODEPOOL":          nodepool,
        "IMAGE_PULL_SECRET": image_pull_secret,
        "CONFIG_B64":        base64.b64encode(config_raw).decode("ascii"),
    }
    print(f"\n🧩 Render context:")
    for k in ("APP", "ENV", "NAMESPACE", "PROJECT", "ROLE", "NODEPOOL",
              "DOTNET_ENV", "REPLICAS", "IMAGE_FULL", "PORT", "IMAGE_PULL_SECRET"):
        print(f"   {k:18s} = {ctx[k]}")

    # 5. Clone gitops repo shallow
    ts = time.strftime("%Y%m%d-%H%M%S")
    base_workdir = Path(config["pr_fix"]["base_workdir"])
    workdir = base_workdir / f"bq-bootstrap-{app}-{env}-{ts}"
    if workdir.exists():
        shutil.rmtree(workdir)
    workdir.mkdir(parents=True)
    repo_dir = workdir / "gitops"

    print(f"\n📥 Clone gitops {gitops_repo}@{gitops_branch} → {repo_dir} ...")
    token = config["github"]["token"]
    clone_url = f"https://x-access-token:{token}@github.com/{gitops_repo}.git"
    try:
        _sh([
            "git", "clone", "--depth", "1",
            "--branch", gitops_branch, clone_url, str(repo_dir),
        ])
    except subprocess.CalledProcessError as e:
        print(f"   ❌ Clone gagal: {e.stderr}", file=sys.stderr)
        return 1

    head_sha = _sh(["git", "rev-parse", "HEAD"], cwd=str(repo_dir)).stdout.strip()
    print(f"   ↪ HEAD {head_sha[:12]}")

    # 6. Branch + render
    branch_name = pr_branch or f"bootstrap/{app}-{env}-{ts}"
    print(f"\n🌿 Branch: {branch_name} (dari {gitops_branch})")
    _sh(["git", "checkout", "-b", branch_name], cwd=str(repo_dir))

    print(f"\n📝 Render 3 file YAML ke {path_yaml}/:")
    written = _render_and_write(repo_dir, path_yaml, ctx, stack)

    # 6a. Update kustomization.yaml agar deployment + service ke-pick-up kustomize
    kus_rel = _update_kustomization(repo_dir, path_yaml, app)
    if kus_rel:
        written.append(kus_rel)

    # 6b. kubectl apply secret jika belum ada (opt-in via --apply-secret)
    if apply_secret:
        secret_rel = f"{path_yaml}/file-config/{app}-{env}.yaml"
        _apply_secret_if_missing(
            repo_dir, secret_rel,
            f"file-config-{app}-{env}", namespace, env,
            kube_context, dry_run,
        )

    # 6c. Deteksi selector drift → delete Deployment lama (opt-in)
    if force_recreate_deploy:
        _recreate_deploy_if_selector_drift(
            app, namespace, ctx, env, kube_context, dry_run,
        )

    # 7. Commit — skip bila tidak ada diff (re-run pasca-merge PR sebelumnya)
    _sh(["git", "add", "--"] + written, cwd=str(repo_dir))
    status = _sh(["git", "status", "--porcelain"], cwd=str(repo_dir)).stdout.strip()
    if not status:
        print(f"\n✅ Semua file sudah up-to-date di {gitops_branch}. Tidak perlu PR.")
        if not keep_workdir:
            shutil.rmtree(workdir, ignore_errors=True)
        return 0

    print(f"\n📦 Commit ...")
    commit_msg = (
        f"bootstrap-k8s: {app} @ {env} ({stack})\n\n"
        f"Source     : {source_repo}@{refs}\n"
        f"Namespace  : {namespace}\n"
        f"Config from: {config_src_path}\n"
        f"Files      : {', '.join(written)}\n"
        f"Generated with bq --bootstrap-k8s (build-q)"
    )
    _sh(
        ["git", "-c", "user.name=build-q bot", "-c", "user.email=bq@build-q.local",
         "commit", "-m", commit_msg],
        cwd=str(repo_dir),
    )
    print(f"   ✅ {len(written)} file")

    if dry_run:
        print(f"\n🔍 Dry-run: skip push + open PR. Workdir: {repo_dir}")
        return 0

    # 8. Push
    print(f"\n🚀 Push → origin/{branch_name} ...")
    try:
        _sh(["git", "push", "origin", branch_name], cwd=str(repo_dir))
    except subprocess.CalledProcessError as e:
        print(f"   ❌ Push gagal: {(e.stderr or '').strip()}", file=sys.stderr)
        return 2

    # 9. PR (dedupe)
    # NOTE: list_open_prs tanpa `owner:branch` format kadang di-ignore GitHub
    # → filter ulang di sini berdasarkan head.ref agar tidak salah dedupe.
    print(f"\n🔀 Open PR → {gitops_branch} ...")
    try:
        existing = github_api.list_open_prs(gitops_repo, branch_name)
        matching = [
            p for p in (existing or [])
            if (p.get("head") or {}).get("ref") == branch_name
        ]
        if matching:
            url = matching[0].get("html_url", "(unknown)")
            print(f"   ℹ️  PR sudah ada: {url}")
            return 0
    except github_api.GitHubAPIError:
        pass

    body = (
        f"Bootstrap manifest K8s untuk **{app}** di namespace `{namespace}` "
        f"(env `{env}`, stack `{stack}`).\n\n"
        f"**Source repo:** `{source_repo}@{refs}`\n"
        f"**Config file:** `{config_src_path}` → di-encode ke Secret "
        f"`file-config-{app}-{env}`\n"
        f"**Image:** `{image_full}`\n\n"
        f"**Files:**\n"
        + "\n".join(f"- `{p}`" for p in written)
        + "\n\nGenerated with `bq --bootstrap-k8s`."
    )
    try:
        pr = github_api.create_pull_request(
            gitops_repo,
            base=gitops_branch,
            head=branch_name,
            title=f"bootstrap: {app} @ {env} ({stack})",
            body=body,
        )
        print(f"   ✅ {pr.get('html_url')}")
    except github_api.GitHubAPIError as e:
        print(f"   ❌ Gagal buat PR: {e}", file=sys.stderr)
        return 2

    if not keep_workdir:
        shutil.rmtree(workdir, ignore_errors=True)
        print(f"\n🧹 Cleanup {workdir}")
    else:
        print(f"\n📂 Workdir dipertahankan: {workdir}")

    return 0
