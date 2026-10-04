"""Tests for dockerfile_checks.py — rule collection + structural checks."""
import pytest
from build_q.dockerfile_checks import (
    KNOWN_ISSUES,
    analyze_dockerfile,
    has_error,
    format_issues_markdown,
    format_issues_console,
    _parse_instructions,
    _structural_checks,
)


# ─── Collection integrity ──────────────────────────────────────────────────

class TestRuleCollection:
    def test_minimum_rule_count(self):
        assert len(KNOWN_ISSUES) >= 22

    def test_all_rules_have_required_fields(self):
        required = {"id", "severity", "title", "reason", "fix", "gist_ref"}
        for rule in KNOWN_ISSUES:
            missing = required - set(rule.keys())
            assert not missing, f"Rule '{rule.get('id', '?')}' missing: {missing}"

    def test_unique_ids(self):
        ids = [r["id"] for r in KNOWN_ISSUES]
        assert len(ids) == len(set(ids)), f"Duplicate IDs: {[x for x in ids if ids.count(x) > 1]}"

    def test_valid_severities(self):
        valid = {"error", "warning", "info"}
        for rule in KNOWN_ISSUES:
            assert rule["severity"] in valid, f"Rule '{rule['id']}': invalid severity '{rule['severity']}'"

    def test_valid_categories(self):
        valid = {"build", "security", "performance", "compliance"}
        for rule in KNOWN_ISSUES:
            cat = rule.get("category", "build")
            assert cat in valid, f"Rule '{rule['id']}': invalid category '{cat}'"

    def test_auto_fixable_is_bool(self):
        for rule in KNOWN_ISSUES:
            if "auto_fixable" in rule:
                assert isinstance(rule["auto_fixable"], bool), f"Rule '{rule['id']}': auto_fixable not bool"

    def test_has_error_rules(self):
        error_rules = [r for r in KNOWN_ISSUES if r["severity"] == "error"]
        assert len(error_rules) >= 5

    def test_has_warning_rules(self):
        warn_rules = [r for r in KNOWN_ISSUES if r["severity"] in ("warning", "warn")]
        assert len(warn_rules) >= 10

    def test_has_auto_fixable_rules(self):
        fixable = [r for r in KNOWN_ISSUES if r.get("auto_fixable")]
        assert len(fixable) >= 7


# ─── Regex-based detection ─────────────────────────────────────────────────

class TestRegexDetection:
    def test_legacy_github_secrets(self):
        content = 'RUN --mount=type=secret,id=github_token cat /run/secrets/github_token'
        issues = analyze_dockerfile(content)
        assert any(i["id"] == "legacy-github-secrets" for i in issues)

    def test_netrc_wrong_target(self):
        content = 'RUN --mount=type=secret,id=netrc go mod download'
        issues = analyze_dockerfile(content)
        assert any(i["id"] == "netrc-secret-wrong-target" for i in issues)

    def test_netrc_correct_target_no_issue(self):
        content = 'RUN --mount=type=secret,id=netrc,target=/root/.netrc go mod download'
        issues = analyze_dockerfile(content)
        assert not any(i["id"] == "netrc-secret-wrong-target" for i in issues)

    def test_arg_github_token(self):
        content = 'ARG GITHUB_TOKEN\nFROM golang:1.23-alpine'
        issues = analyze_dockerfile(content)
        assert any(i["id"] == "arg-github-token-legacy" for i in issues)

    def test_go_toolchain_old(self):
        content = 'FROM golang:1.19-alpine AS builder\nWORKDIR /app'
        issues = analyze_dockerfile(content)
        assert any(i["id"] == "go-toolchain-mismatch" for i in issues)

    def test_go_toolchain_new_ok(self):
        content = 'FROM golang:1.23-alpine AS builder\nWORKDIR /app'
        issues = analyze_dockerfile(content)
        assert not any(i["id"] == "go-toolchain-mismatch" for i in issues)

    def test_deprecated_maintainer(self):
        content = 'FROM alpine\nMAINTAINER john@example.com'
        issues = analyze_dockerfile(content)
        assert any(i["id"] == "deprecated-maintainer" for i in issues)

    def test_env_legacy_syntax(self):
        content = 'FROM alpine\nENV PORT 8080'
        issues = analyze_dockerfile(content)
        assert any(i["id"] == "env-legacy-syntax" for i in issues)

    def test_env_modern_syntax_ok(self):
        content = 'FROM alpine\nENV PORT=8080'
        issues = analyze_dockerfile(content)
        assert not any(i["id"] == "env-legacy-syntax" for i in issues)

    def test_from_as_lowercase(self):
        content = 'FROM golang:1.23-alpine as builder\nWORKDIR /app'
        issues = analyze_dockerfile(content)
        assert any(i["id"] == "from-as-lowercase" for i in issues)

    def test_from_as_uppercase_ok(self):
        content = 'FROM golang:1.23-alpine AS builder\nWORKDIR /app'
        issues = analyze_dockerfile(content)
        assert not any(i["id"] == "from-as-lowercase" for i in issues)

    def test_base_image_latest(self):
        content = 'FROM node:latest\nWORKDIR /app'
        issues = analyze_dockerfile(content)
        assert any(i["id"] == "base-image-latest" for i in issues)

    def test_registry_literal_placeholder(self):
        content = 'FROM REGISTRY01/PROJECT/golang:1.23'
        issues = analyze_dockerfile(content)
        assert any(i["id"] == "registry-literal-placeholder" for i in issues)

    def test_private_repo_bitbucket(self):
        content = 'FROM golang\nRUN GOPRIVATE=bitbucket.org/qoin go mod download'
        issues = analyze_dockerfile(content)
        assert any(i["id"] == "private-repo-bitbucket" for i in issues)

    def test_workdir_commented(self):
        content = 'FROM alpine\n# WORKDIR /app\nCOPY . .'
        issues = analyze_dockerfile(content)
        assert any(i["id"] == "workdir-commented-out" for i in issues)

    def test_add_instead_of_copy(self):
        content = 'FROM alpine\nADD main.go /app/'
        issues = analyze_dockerfile(content)
        assert any(i["id"] == "add-instead-of-copy" for i in issues)

    def test_add_url_ok(self):
        content = 'FROM alpine\nADD https://example.com/file.tar.gz /tmp/'
        issues = analyze_dockerfile(content)
        assert not any(i["id"] == "add-instead-of-copy" for i in issues)

    def test_apt_get_no_clean(self):
        content = 'FROM debian\nRUN apt-get update && apt-get install -y curl'
        issues = analyze_dockerfile(content)
        assert any(i["id"] == "apt-get-no-clean" for i in issues)

    def test_pip_no_cache(self):
        content = 'FROM python:3.12\nRUN pip install flask'
        issues = analyze_dockerfile(content)
        assert any(i["id"] == "pip-no-cache" for i in issues)

    def test_pip_with_cache_ok(self):
        content = 'FROM python:3.12\nRUN pip install --no-cache-dir flask'
        issues = analyze_dockerfile(content)
        assert not any(i["id"] == "pip-no-cache" for i in issues)

    def test_curl_without_fail(self):
        content = 'FROM alpine\nRUN curl https://example.com/install.sh | sh'
        issues = analyze_dockerfile(content)
        assert any(i["id"] == "curl-without-fail" for i in issues)

    def test_empty_content(self):
        assert analyze_dockerfile("") == []
        assert analyze_dockerfile(None) == []


# ─── Structural checks ────────────────────────────────────────────────────

class TestStructuralChecks:
    def test_missing_user(self):
        content = 'FROM golang:1.23-alpine\nRUN go build\nCMD ["./app"]'
        ids = _structural_checks(content)
        assert "missing-user" in ids

    def test_user_present_ok(self):
        content = 'FROM golang:1.23-alpine\nRUN go build\nUSER appuser\nCMD ["./app"]'
        ids = _structural_checks(content)
        assert "missing-user" not in ids

    def test_user_in_final_stage(self):
        content = (
            'FROM golang:1.23-alpine AS builder\n'
            'USER root\n'
            'RUN go build\n'
            'FROM alpine:3.20\n'
            'COPY --from=builder /app /app\n'
            'USER appuser\n'
            'CMD ["./app"]'
        )
        ids = _structural_checks(content)
        assert "missing-user" not in ids

    def test_user_only_in_builder_not_runtime(self):
        content = (
            'FROM golang:1.23-alpine AS builder\n'
            'USER builduser\n'
            'RUN go build\n'
            'FROM alpine:3.20\n'
            'COPY --from=builder /app /app\n'
            'CMD ["./app"]'
        )
        ids = _structural_checks(content)
        assert "missing-user" in ids

    def test_copy_all_before_deps(self):
        content = (
            'FROM golang:1.23-alpine\n'
            'WORKDIR /app\n'
            'COPY . .\n'
            'RUN go mod download\n'
            'RUN go build\n'
        )
        ids = _structural_checks(content)
        assert "copy-all-before-deps" in ids

    def test_copy_deps_first_ok(self):
        content = (
            'FROM golang:1.23-alpine\n'
            'WORKDIR /app\n'
            'COPY go.mod go.sum ./\n'
            'RUN go mod download\n'
            'COPY . .\n'
            'RUN go build\n'
        )
        ids = _structural_checks(content)
        assert "copy-all-before-deps" not in ids

    def test_expose_missing(self):
        content = 'FROM alpine\nCMD ["./app"]'
        ids = _structural_checks(content)
        assert "expose-missing" in ids

    def test_expose_present_ok(self):
        content = 'FROM alpine\nEXPOSE 8080\nCMD ["./app"]'
        ids = _structural_checks(content)
        assert "expose-missing" not in ids

    def test_healthcheck_missing(self):
        content = 'FROM alpine\nCMD ["./app"]'
        ids = _structural_checks(content)
        assert "missing-healthcheck" in ids

    def test_healthcheck_present_ok(self):
        content = 'FROM alpine\nHEALTHCHECK CMD curl -f http://localhost/health\nCMD ["./app"]'
        ids = _structural_checks(content)
        assert "missing-healthcheck" not in ids

    def test_missing_multistage_go(self):
        content = 'FROM golang:1.23-alpine\nRUN go build\nCMD ["./app"]'
        ids = _structural_checks(content)
        assert "missing-multistage" in ids

    def test_multistage_present_ok(self):
        content = (
            'FROM golang:1.23-alpine AS builder\n'
            'RUN go build\n'
            'FROM alpine:3.20\n'
            'COPY --from=builder /app /app\n'
            'CMD ["./app"]'
        )
        ids = _structural_checks(content)
        assert "missing-multistage" not in ids

    def test_run_too_many_layers(self):
        runs = "\n".join(f"RUN echo step{i}" for i in range(10))
        content = f'FROM alpine\n{runs}\nCMD ["./app"]'
        ids = _structural_checks(content)
        assert "run-too-many-layers" in ids

    def test_run_few_layers_ok(self):
        content = 'FROM alpine\nRUN echo a\nRUN echo b\nCMD ["./app"]'
        ids = _structural_checks(content)
        assert "run-too-many-layers" not in ids

    def test_empty_content(self):
        assert _structural_checks("") == []
        assert _structural_checks("   ") == []


# ─── Instruction parser ───────────────────────────────────────────────────

class TestParseInstructions:
    def test_basic_parse(self):
        content = 'FROM alpine:3.20\nWORKDIR /app\nCOPY . .\nCMD ["./app"]'
        insts = _parse_instructions(content)
        assert len(insts) == 4
        assert insts[0]["instruction"] == "FROM"
        assert insts[1]["instruction"] == "WORKDIR"
        assert insts[2]["instruction"] == "COPY"
        assert insts[3]["instruction"] == "CMD"

    def test_skip_comments_and_blanks(self):
        content = '# comment\n\nFROM alpine\n# another comment\nRUN echo hi'
        insts = _parse_instructions(content)
        assert len(insts) == 2
        assert insts[0]["instruction"] == "FROM"
        assert insts[1]["instruction"] == "RUN"

    def test_line_continuation(self):
        content = 'FROM alpine\nRUN echo a && \\\n    echo b && \\\n    echo c'
        insts = _parse_instructions(content)
        assert len(insts) == 2
        run_inst = insts[1]
        assert "echo a" in run_inst["args"]
        assert "echo c" in run_inst["args"]

    def test_lineno_tracking(self):
        content = '# header\nFROM alpine\nRUN echo hi'
        insts = _parse_instructions(content)
        assert insts[0]["lineno"] == 2
        assert insts[1]["lineno"] == 3

    def test_uppercase_normalization(self):
        content = 'from alpine\nrun echo hi\ncopy . .'
        insts = _parse_instructions(content)
        assert all(i["instruction"] == i["instruction"].upper() for i in insts)


# ─── Backward compat (pr_fix.py API) ──────────────────────────────────────

class TestBackwardCompat:
    def test_has_error_true(self):
        issues = [{"severity": "error", "id": "test"}]
        assert has_error(issues) is True

    def test_has_error_false(self):
        issues = [{"severity": "warning", "id": "test"}]
        assert has_error(issues) is False

    def test_has_error_empty(self):
        assert has_error([]) is False

    def test_format_markdown_nonempty(self):
        issues = analyze_dockerfile('FROM alpine\nMAINTAINER test')
        md = format_issues_markdown(issues)
        assert "deprecated-maintainer" in md
        assert "**Dockerfile issues" in md

    def test_format_markdown_empty(self):
        assert format_issues_markdown([]) == ""

    def test_format_console_nonempty(self):
        issues = analyze_dockerfile('FROM alpine\nMAINTAINER test')
        lines = format_issues_console(issues)
        assert len(lines) > 0
        assert any("deprecated-maintainer" in line for line in lines)

    def test_format_console_empty(self):
        assert format_issues_console([]) == []


# ─── Clean Dockerfile (golden path) ───────────────────────────────────────

class TestCleanDockerfile:
    CLEAN_DOCKERFILE = """\
FROM golang:1.23-alpine AS builder
WORKDIR /app
COPY go.mod go.sum ./
RUN --mount=type=secret,id=netrc,target=/root/.netrc go mod download
COPY . .
RUN go build -o /app/main .

FROM alpine:3.20
WORKDIR /app
COPY --from=builder /app/main .
RUN adduser -D appuser
USER appuser
EXPOSE 8080
HEALTHCHECK CMD wget -q --spider http://localhost:8080/health || exit 1
CMD ["./main"]
"""

    def test_no_errors(self):
        issues = analyze_dockerfile(self.CLEAN_DOCKERFILE)
        errors = [i for i in issues if i["severity"] == "error"]
        assert len(errors) == 0, f"Unexpected errors: {[e['id'] for e in errors]}"

    def test_no_warnings(self):
        issues = analyze_dockerfile(self.CLEAN_DOCKERFILE)
        warnings = [i for i in issues if i["severity"] == "warning"]
        assert len(warnings) == 0, f"Unexpected warnings: {[w['id'] for w in warnings]}"

    def test_no_findings_at_all(self):
        issues = analyze_dockerfile(self.CLEAN_DOCKERFILE)
        assert len(issues) == 0, f"Unexpected issues: {[i['id'] for i in issues]}"
