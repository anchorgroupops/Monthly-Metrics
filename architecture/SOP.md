# Anchor Group Monthly Metrics - Architecture SOP

## Overview

This project handles monthly metrics for Anchor Group.

## Core Principles (A.N.T. Layer 1)

1. **Logic Persistence**: All business rules must be documented here before implementation.
2. **Atomic Tools**: Scripts should be single-purpose and stored in `scripts/` or `tools/`.
3. **Data Integrity**: Metrics calculation must be idempotent and verifiable.

## Repository Setup

- **Source**: `https://github.com/anchorgroupops/Monthly-Metrics`
- **Main Branch**: `main` (default; protected — see `.github/workflows/ci.yml`)
- **Onboarding Strategy**: `git clone`, run `scripts/install.sh`, verify `pytest` green.

### Branch model

- `main` — stable, deployed. Only fast-forward merges from PRs that pass CI.
- `harden/p<N>-<topic>` — short-lived implementation branches per phase
  (see `docs/superpowers/specs/2026-05-07-audit-harden-deploy-design.md`).
- Archived ancestors: `archive/claude/{add-claude-documentation-WkXo8,agent-dashboard-metrics-q9fTw,analyze-test-coverage-1y5b6,analyze-test-coverage-JLXX4,analyze-test-coverage-pKsDU,implement-todo-hxs9O,notebooklm-mcp-access-js94b,zillow-digest-system-OGMIF}`
  preserved as tags for forensic reference; not for active development.
