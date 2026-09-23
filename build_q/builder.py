"""Core build logic for build-q CLI."""
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .config import load_config, load_local_cicd, use_gh_cli


class BuildError(Exception):
    """Raised when build command fails."""
    pass


def get_git_info() -> Dict[str, str]:
    """Auto-detect repo name and current branch from git.

    Returns:
        dict with 'repo' and 'ref' keys
    """
    try:
        ref_raw = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, check=True
        ).stdout.strip()

        # Detached HEAD: try tag then short SHA
        if ref_raw == "HEAD":
            tag_result = subprocess.run(
                ["git", "describe", "--tags", "--exact-match"],
                capture_output=True, text=True
            )
            if tag_result.returncode == 0:
                ref_raw = tag_result.stdout.strip()
            else:
                ref_raw = subprocess.run(
                    ["git", "rev-parse", "--short", "HEAD"],
                    capture_output=True, text=True, check=True
                ).stdout.strip()

    except subprocess.CalledProcessError as e:
        raise BuildError(f"Not in a git repository: {e.stderr.strip()}")

    # Repo name from remote or directory
    try:
        remote_url = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True, text=True, check=True
        ).stdout.strip()
        repo = remote_url.rstrip("/").rstrip(".git").split("/")[-1]
    except subprocess.CalledProcessError:
        repo = Path.cwd().name

    return {"repo": repo, "ref": ref_raw}


def get_local_commit_short() -> str:
    """Return 7-char short commit hash from local git."""
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short=7", "HEAD"],
            capture_output=True, text=True, check=True
        ).stdout.strip()
    except subprocess.CalledProcessError:
        return "unknown"


def get_local_tag_or_commit() -> str:
    """Return exact git tag if HEAD sits on a tag, else short commit hash.

    Mirrors the Makefile IMAGE_TAG rule used by templates:
        git describe --tags --exact-match  ||  git rev-parse --short HEAD
    Kept in sync so the registry idempotency check on `--compose` probes
    exactly the tag `make release` will push.
    """
    try:
        result = subprocess.run(
            ["git", "describe", "--tags", "--exact-match"],
            capture_output=True, text=True, check=True,
        )
        tag = result.stdout.strip()
        if tag:
            return tag
    except subprocess.CalledProcessError:
        pass
    return get_local_commit_short()


def _env_from_ref(ref: Optional[str]) -> str:
    """Map a git ref (branch or tag) to a build ENV.

    **INLINE dengan pipeline Tekton** di ~/jenkins-x/pipeline (lighthouse
    triggers.yaml + webhook-server.py). Aturan case shell yang di-mirror:

        v* | refs/tags/v*  → production     (IMAGE_TAG = basename ref, mis. v1.2.3)
        develop            → develop        (IMAGE_TAG = short SHA)
        staging            → staging        (IMAGE_TAG = short SHA)
        sandbox            → sandbox        (IMAGE_TAG = short SHA)
        else / kosong      → staging        (fallback default, cocokan Tekton `*)`)

    Perhatikan:
      • `main`/`master` masuk ke fallback (`*)` di Tekton → `staging`, bukan
        production. Push ke main TIDAK memicu production; tag `v*` yang memicu.
      • `development` (alias develop) juga masuk fallback → `staging` di
        Tekton. bq mengikuti; kalau developer memang mau `develop`, pakai
        branch bernama `develop` (bukan `development`) atau override via
        `--build-arg BRANCH=develop`.

    Override manual: `bq --build-arg BRANCH=<env>` (untuk buildx).
    """
    if not ref:
        return "staging"
    r = ref.lower()
    if r.startswith("v") or r.startswith("refs/tags/v"):
        return "production"
    if r == "develop":
        return "develop"
    if r == "staging":
        return "staging"
    if r == "sandbox":
        return "sandbox"
    return "staging"


def build_command(
    repo: str,
    ref: str,
    cicd: Dict[str, Any],
    config: Dict[str, Any],
    *,
    platform: Optional[str] = "linux/amd64",
    push: bool = False,
    tag: Optional[str] = None,
    dockerfile: str = "Dockerfile",
    context: str = ".",
    extra_build_args: Optional[List[str]] = None,
    secrets: Optional[List[str]] = None,
) -> Tuple[List[str], str]:
    """Assemble the docker buildx build command.

    Args:
        repo: Repository/service name (used for image tag fallback)
        ref: Git branch/tag reference
        cicd: Parsed cicd.json dict
        config: Tool configuration dict
        platform: Target platform string e.g. "linux/amd64"
        push: Whether to add --push flag
        tag: Explicit image tag override
        dockerfile: Path to Dockerfile
        context: Build context directory
        extra_build_args: List of KEY=VALUE strings to pass as --build-arg
        secrets: List of secret specs e.g. ["id=netrc,src=/home/me/.netrc"]

    Returns:
        Tuple of (command_list, image_tag)
    """
    builder = config["builder"]
    registry_url = config["registry"]["url"]

    cmd: List[str] = ["docker", "buildx", "build"]

    # Builder
    cmd += ["--builder", builder["name"]]

    # Progress
    cmd += ["--progress=plain"]

    # No cache
    cmd += ["--no-cache"]

    # Resource limits
    cmd += ["--memory", builder["memory"]]
    cmd += ["--cpu-period", builder["cpu_period"]]
    cmd += ["--cpu-quota", builder["cpu_quota"]]

    # Platform
    if platform:
        cmd += ["--platform", platform]

    # Secrets
    # Add default netrc secret if not provided
    default_secret = f"id=netrc,src={Path.home()}/.netrc"
    all_secrets = list(secrets) if secrets else []
    if not any(s.startswith("id=netrc") for s in all_secrets):
        all_secrets.append(default_secret)

    for secret in all_secrets:
        cmd += ["--secret", secret]

    # Default build arguments — BRANCH via _env_from_ref so both:
    #   - build-arg BRANCH   (dipakai Dockerfile untuk `.env.${BRANCH}`)
    #   - and the compose `ENV=` in run_compose
    # menyimpulkan environment yang sama dari ref (branch atau tag).
    branch_val = _env_from_ref(ref)

    # Check if BRANCH is already in extra_build_args (user override wins)
    extra_args_list = list(extra_build_args) if extra_build_args else []
    if not any(arg.startswith("BRANCH=") for arg in extra_args_list):
        cmd += ["--build-arg", f"BRANCH={branch_val}"]

    # Build args from cicd.json
    for key in ("PORT", "PORT2", "PROJECT"):
        val = cicd.get(key, "")
        if val:
            cmd += ["--build-arg", f"{key}={val}"]

    # Auto-inject GITHUB_USER/GITHUB_TOKEN dari env (INLINE dengan compose.yaml
    # yang mem-forward ${GIT_USER:-${GITHUB_USER:-…}} + ${GIT_TOKEN:-${GITHUB_TOKEN:-…}}).
    # Dockerfile Go/.NET Qoin butuh ini untuk `netrc` (private repo qoinhubhelper).
    # User override via --build-arg tetap menang.
    for key in ("GITHUB_USER", "GITHUB_TOKEN"):
        if any(a.startswith(f"{key}=") for a in extra_args_list):
            continue
        val = os.environ.get(key) or os.environ.get(f"GIT_{key.split('_', 1)[1]}", "")
        if val:
            cmd += ["--build-arg", f"{key}={val}"]

    # Extra --build-arg from CLI
    for arg in extra_args_list:
        cmd += ["--build-arg", arg]

    # Tag — pakai `git describe --tags --exact-match || short commit`, INLINE
    # dengan Makefile IMAGE_TAG rule dan `_predict_compose_image_tag`. Dengan ini
    # `bq` default dan `bq --compose` menghasilkan tag yang identik.
    if tag:
        image_tag = str(tag)
    else:
        image_name = cicd.get("IMAGE", repo)
        ref_id = get_local_tag_or_commit()
        image_tag = f"{registry_url}/{image_name}:{ref_id}" if registry_url else f"{image_name}:{ref_id}"

    cmd += ["-t", image_tag]

    # Push (enabled by default unless explicitly False)
    # Note: CLI currently uses store_true, so we can't easily distinguish 'default' from 'False'.
    # However, if the user wants it by default, we'll just check the flag.
    # To correctly support 'default ON' while allowing 'OFF', we might need to change CLI.
    # For now, let's assume if push is True or it's the default call, we add it.
    if push:
        cmd += ["--push"]

    # Dockerfile
    cmd += ["-f", dockerfile]

    # Context (must be last)
    cmd.append(context)

    return cmd, image_tag


def ensure_builder(name: str, *, bootstrap: bool = False) -> bool:
    """Ensure docker buildx builder exists. Create it (and use it) if missing.

    Args:
        name: Builder name (e.g. from config BUILDER_NAME).
        bootstrap: If True, also boot the builder container so it's ready immediately.

    Returns:
        True if the builder is ready, False if docker/buildx is unavailable.
    """
    try:
        inspect = subprocess.run(
            ["docker", "buildx", "inspect", name],
            capture_output=True, text=True
        )
        if inspect.returncode == 0:
            if "Error:" in inspect.stdout:
                print(f"⚠️ Buildx builder '{name}' exists but endpoint is stale. Recreating ...")
                subprocess.run(
                    ["docker", "buildx", "rm", "-f", name],
                    capture_output=True, text=True
                )
            else:
                print(f"✅ Buildx builder '{name}' already exists.")
                use = subprocess.run(
                    ["docker", "buildx", "use", name],
                    capture_output=True, text=True
                )
                if use.returncode != 0:
                    print(f"⚠️ Could not set '{name}' as active: {use.stderr.strip()}", file=sys.stderr)
                return True

        print(f"🔧 Creating buildx builder '{name}' ...")
        create_cmd = ["docker", "buildx", "create", "--name", name, "--use"]
        if bootstrap:
            create_cmd.append("--bootstrap")
        create = subprocess.run(create_cmd, capture_output=True, text=True)
        if create.returncode == 0:
            print(f"✅ Buildx builder '{name}' created and set as active.")
            return True
        print(f"❌ Failed to create builder: {create.stderr.strip()}", file=sys.stderr)
        return False
    except FileNotFoundError:
        print("⚠️ Docker not found in PATH — skipping builder init.", file=sys.stderr)
        return False


def _parse_github_remote(url: str) -> Optional[str]:
    """Return `owner/repo` from any GitHub remote URL, else None."""
    if not url:
        return None
    if url.startswith("git@github.com:"):
        path = url[len("git@github.com:"):]
    elif "github.com/" in url:
        path = url.split("github.com/", 1)[1]
    else:
        return None
    if path.endswith(".git"):
        path = path[:-4]
    parts = path.strip("/").split("/")
    if len(parts) >= 2:
        return f"{parts[0]}/{parts[1]}"
    return None


def detect_github_repo() -> Optional[str]:
    """Detect current GitHub repo (owner/repo) from `git remote get-url origin`."""
    try:
        remote = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True, text=True, check=True
        ).stdout.strip()
        return _parse_github_remote(remote)
    except subprocess.CalledProcessError:
        return None


def _fetch_jx_token(context: str, namespace: str, secret_name: str) -> Optional[str]:
    """Fetch webhook trigger token from k8s secret. Returns None on failure."""
    import base64

    cmd = ["kubectl"]
    if context:
        cmd += ["--context", context]
    cmd += [
        "-n", namespace, "get", "secret", secret_name,
        "-o", "jsonpath={.data.token}",
    ]
    try:
        b64 = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout.strip()
        if not b64:
            return None
        return base64.b64decode(b64).decode().strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def init_secrets(
    repo: Optional[str] = None,
    token: Optional[str] = None,
    url: Optional[str] = None,
) -> bool:
    """Set GitHub Actions secrets WEBHOOK_TRIGGER_URL & WEBHOOK_TRIGGER_TOKEN on `repo`.

    - `repo`: `owner/repo`. Auto-detected from `git remote origin` if None.
    - `token`: webhook token. Fetched from configured k8s secret if None.
    - `url`: webhook URL. Falls back to config WEBHOOK_TRIGGER_URL.

    Returns True on success.
    """
    config = load_config()
    webhook = config.get("webhook", {})

    if not repo:
        repo = detect_github_repo()
    if not repo:
        print("❌ Cannot detect target repo. Pass `bq --init-secrets <owner>/<repo>` "
              "or run inside a git repo with GitHub origin.", file=sys.stderr)
        return False

    if not url:
        url = webhook.get("trigger_url", "https://cicd-hw.qoin.id/trigger")

    if not token:
        ctx = webhook.get("k8s_context", "")
        ns = webhook.get("k8s_namespace", "jenkins-x")
        secret = webhook.get("k8s_secret", "webhook-trigger-token")
        ctx_display = ctx or "(current context)"
        print(f"🔑 Fetching webhook token from k8s: context={ctx_display} ns={ns} secret={secret}")
        token = _fetch_jx_token(ctx, ns, secret)
        if not token:
            print(f"❌ Could not fetch token. Ensure kubectl is authenticated and "
                  f"secret `{secret}` exists in `{ns}`. Or pass `--token <VALUE>`.", file=sys.stderr)
            return False

    print(f"🔐 Setting secrets on {repo}:")
    entries = [
        ("WEBHOOK_TRIGGER_URL", url, url),
        ("WEBHOOK_TRIGGER_TOKEN", token, "***"),
    ]
    use_cli = use_gh_cli()
    for name, value, display in entries:
        if use_cli:
            result = subprocess.run(
                ["gh", "secret", "set", name, "--repo", repo, "--body", value],
                capture_output=True, text=True
            )
            if result.returncode != 0:
                print(f"   ❌ {name}: {result.stderr.strip()}", file=sys.stderr)
                return False
        else:
            from .github_api import GitHubAPIError, set_secret
            try:
                set_secret(repo, name, value)
            except GitHubAPIError as e:
                print(f"   ❌ {name}: {e}", file=sys.stderr)
                return False
        print(f"   ✅ {name} = {display}")
    print(f"\n✅ GitHub Actions secrets configured on {repo}.")
    return True


def init_jx(cicd_path: str = "cicd/cicd.json", force: bool = False) -> bool:
    """Scaffold Makefile / compose.yaml / Dockerfile / .github/workflows/trigger-ci.yml
    from `cicd/cicd.json`.

    Returns True if any file was written.
    """
    from .templates import load_template, render

    try:
        cicd = load_local_cicd(cicd_path)
    except FileNotFoundError:
        print(f"❌ {cicd_path} not found. Create it first with IMAGE/PROJECT/PORT keys.", file=sys.stderr)
        return False

    config = load_config()
    registry = config.get("registry", {}).get("url", "") or "loyaltolpi"

    image = cicd.get("IMAGE") or Path.cwd().name
    ctx = {
        "IMAGE": image,
        "PROJECT": cicd.get("PROJECT", "qoin"),
        "PORT": cicd.get("PORT", "8080"),
        "CLUSTER": cicd.get("CLUSTER", "qoin"),
        "DEPLOYMENT": cicd.get("DEPLOYMENT", image),
        "NODETYPE": cicd.get("NODETYPE", "back"),
        "ORG_REGISTRY": registry,
    }

    print(f"📦 Scaffolding from {cicd_path} (templates dari gist mamatnurahmat/35cc4c36e7c7c2d236a1b5149cdbcfd9):")
    for k, v in ctx.items():
        print(f"   {k:12} = {v}")
    print()

    files = [
        (Path("Makefile"), render(load_template("makefile"), ctx)),
        (Path("compose.yaml"), render(load_template("compose"), ctx)),
        (Path("Dockerfile"), render(load_template("dockerfile"), ctx)),
        (Path(".github/workflows/trigger-ci.yml"), load_template("trigger_ci")),
    ]

    written = 0
    for path, content in files:
        if path.exists() and not force:
            print(f"⏭️  Skip existing: {path} (use --force to overwrite)")
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        print(f"✅ Wrote {path}")
        written += 1

    if written == 0:
        print("\nℹ️  No files written. Use --force to overwrite existing files.")
    else:
        print(f"\n✅ Scaffolded {written} file(s).")

    repo = detect_github_repo()
    if repo:
        print(f"\n🔗 Detected GitHub remote: {repo} — configuring Actions secrets ...")
        init_secrets(repo=repo)
    else:
        print("\nℹ️  No GitHub remote detected. After pushing the repo, configure secrets:")
        print("   bq --init-secrets <owner>/<repo>")

    print("\n📋 Next steps:")
    print("   • Review generated Dockerfile & adjust go build path (server.go or cmd/…)")
    print("   • Ensure .env.<env> files exist for each environment")
    print("   • Commit and push to trigger Jenkins X pipeline")
    return written > 0


def init_gh_action(token: Optional[str] = None) -> bool:
    """Bootstrap .github/workflows/trigger-ci.yml sebagai satu-satunya workflow.

    - Hapus semua file .yml/.yaml lain di .github/workflows/ (workflow lama)
    - Tulis ulang trigger-ci.yml dari TRIGGER_CI_TPL
    - Set WEBHOOK_TRIGGER_URL + WEBHOOK_TRIGGER_TOKEN via `gh secret set`
    """
    from .templates import load_template

    workflows_dir = Path(".github/workflows")
    workflows_dir.mkdir(parents=True, exist_ok=True)

    trigger_path = workflows_dir / "trigger-ci.yml"

    # 1. Hapus workflow lain
    existing = [
        p for p in workflows_dir.iterdir()
        if p.is_file() and p.suffix in {".yml", ".yaml"} and p.name != "trigger-ci.yml"
    ]
    if existing:
        print(f"🗑️  Menghapus {len(existing)} workflow lain di {workflows_dir}:")
        for p in existing:
            print(f"   • {p.name}")
            p.unlink()
    else:
        print(f"ℹ️  Tidak ada workflow lain untuk dihapus di {workflows_dir}.")

    # 2. Tulis ulang trigger-ci.yml (selalu overwrite — file standar dari gist)
    if trigger_path.exists():
        print(f"♻️  Overwrite existing {trigger_path}")
    trigger_path.write_text(load_template("trigger_ci"))
    print(f"✅ Wrote {trigger_path} (dari central gist)")

    # 3. Set webhook secrets (butuh git remote origin)
    repo = detect_github_repo()
    if not repo:
        print("\n⚠️  Git remote origin belum ada — skip pengaturan secret.")
        print("   Setelah push repo ke GitHub, jalankan:")
        print("     bq --init-secrets <owner>/<repo>")
        return True

    print(f"\n🔐 Setting webhook secrets on {repo} ...")
    ok = init_secrets(repo=repo, token=token)
    if not ok:
        print("\n⚠️  Workflow ter-generate, tapi secret gagal di-set. "
              "Perbaiki lalu jalankan `bq --init-secrets` lagi.", file=sys.stderr)
        return False

    print("\n📋 Next steps:")
    print("   • Commit & push perubahan .github/workflows/trigger-ci.yml")
    print("   • Push branch develop/staging/master atau tag v* akan trigger pipeline JX")
    return True


def init_legacy(cicd_path: str = "cicd/cicd.json", force: bool = False) -> bool:
    """Scaffold Makefile + compose.yaml pakai pola legacy netrc (build-args).

    Cocok untuk repo yang Dockerfile-nya masih pakai ARG GITHUB_USER + GITHUB_TOKEN
    (bukan BuildKit secret). Makefile auto-ambil `gh auth token` untuk local dev.
    Dockerfile TIDAK di-overwrite — biarkan sesuai pola legacy repo.

    Returns True jika ada file yang ditulis.
    """
    from .templates import load_template, render

    try:
        cicd = load_local_cicd(cicd_path)
    except FileNotFoundError:
        print(f"⚠️  {cicd_path} not found — memakai default (image dari nama folder).")
        cicd = {}

    config = load_config()
    registry = config.get("registry", {}).get("url", "") or "loyaltolpi"

    image = cicd.get("IMAGE") or Path.cwd().name
    ctx = {
        "IMAGE": image,
        "PROJECT": cicd.get("PROJECT", "qoin"),
        "PORT": cicd.get("PORT", "80"),
        "ORG_REGISTRY": registry,
    }

    print(f"📦 Scaffolding legacy (netrc via build-args) from {cicd_path} (templates dari central gist):")
    for k, v in ctx.items():
        print(f"   {k:12} = {v}")
    print()

    files = [
        (Path("Makefile"), render(load_template("makefile_legacy"), ctx)),
        (Path("compose.yaml"), render(load_template("compose_legacy"), ctx)),
    ]

    written = 0
    for path, content in files:
        if path.exists() and not force:
            print(f"⏭️  Skip existing: {path} (use --force to overwrite)")
            continue
        path.write_text(content)
        print(f"✅ Wrote {path}")
        written += 1

    if written == 0:
        print("\nℹ️  No files written. Use --force to overwrite existing files.")
    else:
        print(f"\n✅ Scaffolded {written} file(s).")

    print("\n📋 Next steps:")
    print("   • Pastikan Dockerfile menerima ARG GITHUB_USER + GITHUB_TOKEN")
    print("   • Local dev: `gh auth login` sekali, lalu `make build ENV=staging`")
    print("   • CI/CD    : inject GITHUB_TOKEN dari secret & pakai COMPOSE_FILE=build.compose bila perlu")
    return written > 0


def check_image_exists(image_tag: str) -> bool:
    """Check if image exists in destination registry using docker buildx imagetools."""
    try:
        result = subprocess.run(
            ["docker", "buildx", "imagetools", "inspect", image_tag],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False
        )
        return result.returncode == 0
    except Exception:
        return False


def _mask_sensitive(part: str) -> str:
    """Mask sensitive build-arg values in log output."""
    if part.startswith("GITHUB_TOKEN=") and len(part) > len("GITHUB_TOKEN="):
        return "GITHUB_TOKEN=***"
    return part


def format_cmd(cmd: List[str]) -> str:
    """Pretty-print the command with line continuations. Masks sensitive values."""
    lines: List[str] = []
    buf = ""
    for raw in cmd:
        part = _mask_sensitive(raw)
        if buf and (part.startswith("--") or part.startswith("-f") or part.startswith("-t")):
            lines.append(buf)
            buf = f"  {part}"
        else:
            buf = f"{buf} {part}".strip() if buf else part
    if buf:
        lines.append(buf)
    return " \\\n".join(lines)


NETRC_RUN_SH_RE = re.compile(
    r"(?m)^(?P<indent>\s*)RUN\s+sh\s+-c\s+'"
    r"echo\s+\"machine\s+github\.com\s+login\s+\$\{?GITHUB_USER\}?\s+"
    r"password\s+\$\{?GITHUB_TOKEN\}?\"\s*>\s*~/\.netrc\s*&&\s*"
    r"chmod\s+\d+\s+~/\.netrc\s*&&\s*(?P<rest>.+?)'\s*$"
)
NETRC_RUN_BARE_RE = re.compile(
    r"(?m)^(?P<indent>\s*)RUN\s+"
    r"echo\s+\"machine\s+github\.com\s+login\s+\$\{?GITHUB_USER\}?\s+"
    r"password\s+\$\{?GITHUB_TOKEN\}?\"\s*>\s*~/\.netrc\s*&&\s*"
    r"chmod\s+\d+\s+~/\.netrc\s*&&\s*(?P<rest>.+?)$"
)
# Standalone: `RUN echo "..." > ~/.netrc` tanpa `&& chmod && ...`
NETRC_RUN_STANDALONE_RE = re.compile(
    r"(?m)^[ \t]*RUN[ \t]+"
    r"echo[ \t]+\"machine[ \t]+github\.com[ \t]+login[ \t]+\$\{?GITHUB_USER\}?[ \t]+"
    r"password[ \t]+\$\{?GITHUB_TOKEN\}?\"[ \t]*>[ \t]*~/\.netrc[ \t]*\n"
)
# RUN line yang butuh netrc saat build (go mod tidy/download, go get)
GO_FETCH_RE = re.compile(
    r"(?m)^(?P<indent>[ \t]*)RUN[ \t]+(?!--mount)(?P<body>[^\n]*?"
    r"\bgo[ \t]+(?:mod[ \t]+(?:tidy|download)|get)\b[^\n]*)$"
)
FROM_CASING_RE = re.compile(r"(?m)^(\s*FROM\s+\S+(?:\s+\S+)*?)\s+as\s+(\S+)")
ARG_GITHUB_RE = re.compile(r"(?m)^\s*ARG\s+GITHUB_(USER|TOKEN)\s*(=[^\n]*)?\n")
# Instruksi deprecated `Maintainer` (case-insensitive) → LABEL maintainer=
MAINTAINER_RE = re.compile(r"(?im)^(?P<indent>\s*)maintainer\s+(?P<val>.+?)\s*$")
# Legacy `ENV KEY value` (tanpa `=`) → `ENV KEY=value`
ENV_LEGACY_RE = re.compile(
    r"(?m)^(?P<indent>\s*)ENV\s+(?P<key>[A-Z_][A-Z0-9_]*)\s+(?P<val>[^=\n][^\n]*?)\s*$"
)


def fix_dockerfile(path: str = "Dockerfile") -> bool:
    """Migrate Dockerfile from ARG-based netrc to `--mount=type=secret` pattern.

    Also normalizes `FROM ... as ...` casing and removes `ARG GITHUB_USER/TOKEN`.
    A `.bak` backup is written alongside the file.

    Returns:
        True if any change was applied.
    """
    p = Path(path)
    if not p.exists():
        print(f"❌ Dockerfile not found: {path}", file=sys.stderr)
        return False

    original = p.read_text()
    content = original
    changes: List[str] = []

    content, n = FROM_CASING_RE.subn(r"\1 AS \2", content)
    if n:
        changes.append(f"normalized {n} `FROM ... AS ...` casing")

    def _run_repl(m: "re.Match[str]") -> str:
        indent = m.group("indent")
        rest = m.group("rest").strip()
        return f"{indent}RUN --mount=type=secret,id=netrc,target=/root/.netrc \\\n{indent}    {rest}"

    content, n = NETRC_RUN_SH_RE.subn(_run_repl, content)
    if n:
        changes.append(f"migrated {n} `RUN sh -c` block to secret mount")

    content, n = NETRC_RUN_BARE_RE.subn(_run_repl, content)
    if n:
        changes.append(f"migrated {n} bare `RUN echo` block to secret mount")

    # Standalone `RUN echo ... > ~/.netrc` — hapus + prepend mount ke go mod/get RUN
    content, n_standalone = NETRC_RUN_STANDALONE_RE.subn("", content)
    if n_standalone:
        changes.append(f"removed {n_standalone} standalone `RUN echo ... > ~/.netrc` line(s)")

        def _add_mount(m: "re.Match[str]") -> str:
            indent = m.group("indent")
            body = m.group("body").rstrip()
            return (
                f"{indent}RUN --mount=type=secret,id=netrc,target=/root/.netrc \\\n"
                f"{indent}    {body}"
            )

        content, m_count = GO_FETCH_RE.subn(_add_mount, content)
        if m_count:
            changes.append(
                f"added netrc secret mount to {m_count} `go mod|get` RUN line(s)"
            )

    content, n = ARG_GITHUB_RE.subn("", content)
    if n:
        changes.append(f"removed {n} `ARG GITHUB_*` line(s)")

    def _maintainer_repl(m: "re.Match[str]") -> str:
        indent = m.group("indent")
        val = m.group("val").strip().strip('"').replace('"', '\\"')
        return f'{indent}LABEL maintainer="{val}"'

    content, n = MAINTAINER_RE.subn(_maintainer_repl, content)
    if n:
        changes.append(f"converted {n} deprecated `MAINTAINER` → `LABEL maintainer=`")

    def _env_repl(m: "re.Match[str]") -> str:
        indent = m.group("indent")
        key = m.group("key")
        val = m.group("val").strip()
        if " " in val and not (val.startswith('"') and val.endswith('"')):
            val = f'"{val}"'
        return f"{indent}ENV {key}={val}"

    content, n = ENV_LEGACY_RE.subn(_env_repl, content)
    if n:
        changes.append(f"converted {n} legacy `ENV KEY value` → `ENV KEY=value`")

    if not changes:
        print(f"ℹ️  No known netrc/ARG pattern found in {path}. Nothing to fix.")
        return False

    backup = p.with_suffix(p.suffix + ".bak")
    backup.write_text(original)
    p.write_text(content)

    print(f"✅ Fixed {path} (backup: {backup.name})")
    for c in changes:
        print(f"   • {c}")
    print(f"\n💡 Rebuild now — `bq` already injects `--secret id=netrc,src=~/.netrc`.")
    return True


def _predict_image_tag(
    repo: str,
    tag: Optional[str],
    cicd: Dict[str, Any],
    config: Dict[str, Any],
) -> str:
    """Compute the image tag we'll build/push, using the same rule as `build_command`.

    Mirrors the Makefile IMAGE_TAG rule (`git describe --tags --exact-match
    || short commit`) so the pre-build registry check probes the exact tag
    `docker buildx build` will push — dan konsisten dengan `--compose`.
    """
    if tag:
        return str(tag)
    registry_url = config.get("registry", {}).get("url", "")
    image_name = cicd.get("IMAGE", repo)
    ref_id = get_local_tag_or_commit()
    base = f"{image_name}:{ref_id}"
    return f"{registry_url}/{base}" if registry_url else base


def run_build(
    repo: str,
    ref: str,
    *,
    cicd_path: str = "cicd/cicd.json",
    cicd_dict: Optional[Dict[str, Any]] = None,
    platform: Optional[str] = "linux/amd64",
    push: bool = False,
    tag: Optional[str] = None,
    dockerfile: str = "Dockerfile",
    context: str = ".",
    extra_build_args: Optional[List[str]] = None,
    secrets: Optional[List[str]] = None,
    dry_run: bool = False,
    image_check: bool = True,
    rollout_ns: Optional[str] = None,
    rollout_infra: Optional[str] = None,
    rollout_path: Optional[str] = None,
) -> int:
    """Load config, build the command, and execute it.

    Returns:
        Exit code (0 = success)
    """
    config = load_config()

    # Load cicd.json early but tolerantly — a missing file must not block the
    # early registry check (we can still predict the tag from `repo`).
    if cicd_dict is not None:
        cicd = cicd_dict
    else:
        try:
            cicd = load_local_cicd(cicd_path)
        except FileNotFoundError:
            cicd = {}

    predicted_tag = _predict_image_tag(repo, tag, cicd, config)

    # Early skip: probe the registry BEFORE `ensure_builder` and command build.
    # When the image is already published, print the ready tag and exit — no
    # Docker daemon, no builder bootstrap, no gh calls beyond what the caller did.
    if image_check and not dry_run:
        print(f"\n🔍 Checking registry for existing image: {predicted_tag} ...")
        if check_image_exists(predicted_tag):
            print(f"✅ Image ready: {predicted_tag}")
            print("⏭️  Skipping build (use --rebuild to force).")
            return 0
        print("   Image not found. Proceeding with build.")

    if not dry_run:
        ensure_builder(config["builder"]["name"])

    # cicd.json is required to build (needs PORT/PROJECT/etc). If we get here,
    # the image did not exist in registry — surface the missing file clearly.
    if cicd_dict is None and not cicd:
        try:
            cicd = load_local_cicd(cicd_path)
        except FileNotFoundError as e:
            raise BuildError(str(e))

    cmd, image_tag = build_command(
        repo=repo,
        ref=ref,
        cicd=cicd,
        config=config,
        platform=platform,
        push=push,
        tag=tag,
        dockerfile=dockerfile,
        context=context,
        extra_build_args=extra_build_args,
        secrets=secrets,
    )

    print(f"\n🚀 Build command:")
    print("=" * 60)
    print(format_cmd(cmd))
    print("=" * 60)

    if dry_run:
        print("\n🔍 Dry-run mode — command not executed.")
        _print_rollout_suggestion(
            cicd, repo, ref, image_tag, config, rollout_ns, rollout_infra, rollout_path,
        )
        return 0

    result = subprocess.run(cmd)
    if result.returncode == 0:
        print("\n✅ Build completed successfully.")
        _print_rollout_suggestion(
            cicd, repo, ref, image_tag, config, rollout_ns, rollout_infra, rollout_path,
        )
    else:
        print(f"\n❌ Build failed (exit code {result.returncode}).", file=sys.stderr)

    return result.returncode


def _print_rollout_suggestion(cicd, repo, ref, image_tag, config,
                              ns, infra, gitops_path) -> None:
    """Print copy-paste rollout suggestions. Swallow errors — never break the build."""
    try:
        from .rollout import compute_rollout, render_suggestion
        rollout = compute_rollout(
            cicd=cicd, repo=repo, ref=ref, image_tag=image_tag,
            config=config, ns=ns, infra=infra, gitops_path=gitops_path,
        )
        print(render_suggestion(rollout))
    except Exception as e:
        print(f"⚠️  Could not render rollout suggestion: {e}", file=sys.stderr)

def _predict_compose_image_tag(
    repo: str,
    tag: Optional[str],
    cicd: Dict[str, Any],
    config: Dict[str, Any],
) -> str:
    """Compose-mode tag predictor — mirrors the Makefile IMAGE_TAG rule.

    Berbeda dengan `_predict_image_tag` (buildx mode, selalu short commit),
    di sini pakai `git describe --tags --exact-match || short commit` supaya
    early registry check di `--compose` probe tag yang sama dengan yang
    akan di-push `make release`.
    """
    if tag:
        return str(tag)
    registry_url = config.get("registry", {}).get("url", "")
    image_name = cicd.get("IMAGE", repo)
    ref_id = get_local_tag_or_commit()
    base = f"{image_name}:{ref_id}"
    return f"{registry_url}/{base}" if registry_url else base


def run_compose(
    repo: str,
    ref: str,
    *,
    cicd_path: str = "cicd/cicd.json",
    cicd_dict: Optional[Dict[str, Any]] = None,
    tag: Optional[str] = None,
    dry_run: bool = False,
    image_check: bool = True,
    rollout_ns: Optional[str] = None,
    rollout_infra: Optional[str] = None,
    rollout_path: Optional[str] = None,
) -> int:
    """Run make build and make release instead of docker buildx."""
    config = load_config()

    if cicd_dict is not None:
        cicd = cicd_dict
    else:
        try:
            cicd = load_local_cicd(cicd_path)
        except FileNotFoundError:
            cicd = {}

    predicted_tag = _predict_compose_image_tag(repo, tag, cicd, config)

    if image_check and not dry_run:
        print(f"\n🔍 Checking registry for existing image: {predicted_tag} ...")
        if check_image_exists(predicted_tag):
            print(f"✅ Image ready: {predicted_tag}")
            print("⏭️  Skipping compose build (use --rebuild to force).")
            return 0
        print("   Image not found. Proceeding with compose build.")

    env_name = _env_from_ref(ref)
    print(f"🌱 ENV = {env_name}  (dari ref: {ref})")

    cmds = [
        ["make", "build", f"ENV={env_name}"],
        ["make", "release", f"ENV={env_name}"],
    ]

    for cmd in cmds:
        print(f"\n🚀 Compose command: {' '.join(cmd)}")
        if dry_run:
            continue

        res = subprocess.run(cmd)
        if res.returncode != 0:
            print(f"❌ Compose failed (exit code {res.returncode}): {' '.join(cmd)}", file=sys.stderr)
            return res.returncode

    if not dry_run:
        print("\n✅ Compose completed successfully.")

    _print_rollout_suggestion(
        cicd, repo, ref, predicted_tag, config, rollout_ns, rollout_infra, rollout_path,
    )
    return 0
