# PRD: build-q (bq) - Simplified Docker Buildx CLI

## 1. Overview
`build-q` (short for Build-Quick) is a lightweight, zero-dependency Python CLI tool designed to simplify local development workflows by streamlining Docker Buildx operations. It bridges the gap between manual Docker commands and complex CI/CD pipelines by providing a one-command build experience for developers.

## 2. Problem Statement
Developers often need to build Docker images locally that match the configuration of their CI/CD pipelines (secrets, build-args, resource limits). Manually constructing long `docker buildx build` commands is error-prone and tedious.

## 3. Goals
- **Simplicity**: Perform complex builds with a single short command (`bq`).
- **Convention over Configuration**: Auto-detect repository and branch names from Git.
- **Portability**: No external Python dependencies, easy to install in any environment.
- **CI/CD Alignment**: Use local `cicd/cicd.json` to mirror production build parameters.

## 4. Key Features
- **Git Auto-detection**: Automatically identifies the service name and git reference (branch/tag) if not specified.
- **Unified Configuration**: Centralized configuration in `~/.build-q/.env` for registry URLs and default resource limits.
- **Resource Management**: Default memory and CPU limits to prevent local machine exhaustion during heavy builds.
- **Default Flags**: Includes `--push` and `--secret id=netrc,src=$HOME/.netrc` by default for streamlined workflows.
- **Conditional Branching**: Dynamically sets `--build-arg BRANCH` based on Git reference (`production` for `v*` tags, `develop` otherwise).
- **Buildx Integration**: Full support for `--secret`, `--platform`, and `--build-arg`.
- **Dry Run Mode**: Preview the generated Docker command without executing it.

## 5. Technical Requirements
- Python 3.7+
- Docker with Buildx plugin installed.
- Git (optional, for auto-detection).

## 6. Architecture
- **CLI Layer (`cli.py`)**: Handles argument parsing and user interactions.
- **Config Layer (`config.py`)**: Manages environment variables and local `.env` persistence.
- **Builder Layer (`builder.py`)**: Orchestrates Git metadata retrieval and command assembly.

## 7. Future Roadmap
- Support for multiple registry profiles.
- Integration with Kubernetes context for local testing.
- Enhanced caching strategies for faster local rebuilds.
