"""MCP prompts — workflow templates for AI assistants."""

from __future__ import annotations

PROMPT_SPECS: dict[str, dict] = {
    "deploy-new-service": {
        "name": "deploy-new-service",
        "description": (
            "Complete workflow to deploy a new service: "
            "build image, bootstrap K8s manifests, create GitOps PR, verify deployment."
        ),
        "arguments": ["repo", "ref", "env"],
    },
    "scan-and-fix": {
        "name": "scan-and-fix",
        "description": (
            "Scan Dockerfile and K8s manifests for issues, then fix them. "
            "Covers security, performance, compliance, and production readiness."
        ),
        "arguments": ["repo", "ref"],
    },
    "rollout-update": {
        "name": "rollout-update",
        "description": (
            "Update a service: build new image, then rollout via "
            "imperative (kubectl) or declarative (GitOps) path."
        ),
        "arguments": ["repo", "ref", "namespace"],
    },
}


def get_deploy_new_service_messages(repo: str, ref: str, env: str) -> list[dict]:
    return [
        {
            "role": "user",
            "content": (
                f"Deploy new service workflow for {repo} at {ref} to {env}:\n\n"
                f"1. Run `build_doctor` to verify prerequisites\n"
                f"2. Run `pipeline_check` for {repo} at {ref}\n"
                f"3. Run `dockerfile_scan` on the repo's Dockerfile\n"
                f"4. Run `docker_build` with push=true for {repo} {ref}\n"
                f"5. Run `gitops_bootstrap` to create K8s manifests for {env}\n"
                f"6. Verify with `gitops_match_check` after ArgoCD sync\n\n"
                f"Repository: {repo}\n"
                f"Ref: {ref}\n"
                f"Environment: {env}\n\n"
                f"Execute each step and report the results. "
                f"Stop if any step fails with exit_code != 0."
            ),
        },
    ]


def get_scan_and_fix_messages(repo: str, ref: str = "main") -> list[dict]:
    return [
        {
            "role": "user",
            "content": (
                f"Scan and fix workflow for {repo} at {ref}:\n\n"
                f"1. Run `dockerfile_scan_remote` on {repo} ref={ref}\n"
                f"2. If issues found, run `dockerfile_fix` to auto-fix\n"
                f"3. Run `k8s_anomaly_scan` on deployment manifests "
                f"(check GitOps repo for this service)\n"
                f"4. Report summary: total issues found, fixed, remaining\n\n"
                f"Repository: {repo}\n"
                f"Ref: {ref}\n\n"
                f"For each finding, explain the risk and whether it was auto-fixed."
            ),
        },
    ]


def get_rollout_update_messages(
    repo: str, ref: str, namespace: str,
) -> list[dict]:
    return [
        {
            "role": "user",
            "content": (
                f"Rollout update workflow for {repo} at {ref} in {namespace}:\n\n"
                f"1. Run `image_check` to see if image already exists\n"
                f"2. If not, run `docker_build` with push=true\n"
                f"3. Choose rollout path:\n"
                f"   a. **Imperative**: `k8s_set_image` for immediate hot-patch\n"
                f"   b. **Declarative**: `gitops_set_image` for ArgoCD-managed rollout\n"
                f"4. Verify with `gitops_match_check`\n\n"
                f"Repository: {repo}\n"
                f"Ref: {ref}\n"
                f"Namespace: {namespace}\n\n"
                f"Recommend declarative path for production, imperative for staging/develop."
            ),
        },
    ]


def register_prompts(server) -> None:
    """Register all MCP prompts on the server."""

    @server.prompt(
        name="deploy-new-service",
        description=PROMPT_SPECS["deploy-new-service"]["description"],
    )
    def deploy_new_service(repo: str, ref: str, env: str) -> list[dict]:
        return get_deploy_new_service_messages(repo, ref, env)

    @server.prompt(
        name="scan-and-fix",
        description=PROMPT_SPECS["scan-and-fix"]["description"],
    )
    def scan_and_fix(repo: str, ref: str = "main") -> list[dict]:
        return get_scan_and_fix_messages(repo, ref)

    @server.prompt(
        name="rollout-update",
        description=PROMPT_SPECS["rollout-update"]["description"],
    )
    def rollout_update(repo: str, ref: str, namespace: str) -> list[dict]:
        return get_rollout_update_messages(repo, ref, namespace)
