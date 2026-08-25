#!/usr/bin/env python3
"""Add activity-aware scheduler governance to the aggressive V5 runtime."""
from __future__ import annotations

import runpy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
runpy.run_path(
    str(ROOT / "scripts/apply_aggressive_v5_followup_20260825.py"),
    run_name="__main__",
)

controller = r'''name: Polymarket Aggressive Operations Controller

on:
  workflow_dispatch:
  schedule:
    - cron: "*/10 * * * *"
  workflow_run:
    workflows:
      - v4-live-paper-smoke
      - deploy-paper-server
      - paper-server-health
    types: [completed]

permissions:
  actions: write
  contents: read

concurrency:
  group: polymarket-aggressive-operations-controller
  cancel-in-progress: true

jobs:
  govern:
    runs-on: ubuntu-24.04
    timeout-minutes: 10
    env:
      GH_TOKEN: ${{ github.token }}
    steps:
      - name: Govern validation deployment health and activity
        shell: bash
        run: |
          set -euo pipefail
          repo="$GITHUB_REPOSITORY"
          now="$(date +%s)"
          main_sha="$(gh api "repos/$repo/git/ref/heads/main" --jq .object.sha)"
          validated_sha="$(gh api "repos/$repo/git/ref/heads/paper-validated" --jq .object.sha 2>/dev/null || true)"

          run_state() {
            local workflow="$1"
            gh run list --repo "$repo" --workflow "$workflow" --limit 1 \
              --json status,conclusion,createdAt,headSha \
              --jq 'if length == 0 then {status:"missing",conclusion:"",createdAt:"1970-01-01T00:00:00Z",headSha:""} else .[0] end'
          }
          age_minutes() {
            local created="$1"
            local epoch
            epoch="$(date -d "$created" +%s 2>/dev/null || echo 0)"
            echo $(( (now - epoch) / 60 ))
          }
          dispatch_unless_running() {
            local workflow="$1"; shift
            local state status
            state="$(run_state "$workflow")"
            status="$(jq -r .status <<<"$state")"
            if [[ "$status" == "queued" || "$status" == "in_progress" ]]; then
              echo "$workflow already $status"
              return 0
            fi
            gh workflow run "$workflow" --repo "$repo" --ref main "$@"
            echo "dispatched $workflow"
          }

          if [[ "$validated_sha" != "$main_sha" ]]; then
            dispatch_unless_running v4-live-smoke.yml -f expected_sha="$main_sha"
            exit 0
          fi

          deploy="$(run_state deploy-paper-server.yml)"
          deploy_age="$(age_minutes "$(jq -r .createdAt <<<"$deploy")")"
          deploy_ok="$(jq -r '.status == "completed" and .conclusion == "success"' <<<"$deploy")"
          if [[ "$deploy_ok" != "true" || "$deploy_age" -gt 20 ]]; then
            dispatch_unless_running deploy-paper-server.yml
          fi

          health="$(run_state server-health.yml)"
          health_age="$(age_minutes "$(jq -r .createdAt <<<"$health")")"
          health_ok="$(jq -r '.status == "completed" and .conclusion == "success"' <<<"$health")"
          if [[ "$health_ok" != "true" || "$health_age" -gt 15 ]]; then
            dispatch_unless_running server-health.yml
          fi

          # A technically alive process with no candidates, intents, positions or
          # trades for an extended period is not healthy for this mission.
          snapshot="$(
            gh api "repos/$repo/contents/telemetry/latest-live-smoke.json?ref=telemetry" \
              --jq .content 2>/dev/null | tr -d '\n' | base64 -d 2>/dev/null || echo '{}'
          )"
          generated="$(jq -r '.generated_ts // 0' <<<"$snapshot")"
          snapshot_age=$(( generated > 0 ? (now - generated) / 60 : 1000000 ))
          activity="$(jq -r '[
              (.intents.rows // 0),
              (.walk_forward.input_trades // 0),
              (.metrics.polymarket_runtime_live_units // 0),
              (.metrics.polymarket_runtime_oos_trades // 0),
              ((.candidates.b1 // []) | length),
              ((.candidates.b2 // []) | length),
              ((.candidates.b3_rewards // []) | length)
            ] | add' <<<"$snapshot" 2>/dev/null || echo 0)"
          activity_int="$(awk -v x="$activity" 'BEGIN {printf "%d", x + 0}')"
          if [[ "$activity_int" -eq 0 && "$snapshot_age" -gt 30 ]]; then
            echo "zero_activity_unhealthy=1 snapshot_age_minutes=$snapshot_age"
            dispatch_unless_running v4-live-smoke.yml -f expected_sha="$main_sha"
            # Ask the existing meta-supervisor to reprioritise research toward
            # opportunity generation rather than another passive green check.
            if gh workflow view control-plane.yml --repo "$repo" >/dev/null 2>&1; then
              dispatch_unless_running control-plane.yml
            fi
            dispatch_unless_running deploy-paper-server.yml
          else
            echo "zero_activity_unhealthy=0 activity_units=$activity snapshot_age_minutes=$snapshot_age"
          fi

          echo "main=$main_sha validated=$validated_sha deploy_age_min=$deploy_age health_age_min=$health_age"
'''
(ROOT / ".github/workflows/aggressive-operations-controller.yml").write_text(
    controller, encoding="utf-8"
)

smoke_path = ROOT / ".github/workflows/v4-live-smoke.yml"
smoke = smoke_path.read_text(encoding="utf-8")
smoke = smoke.replace('- cron: "37 * * * *"', '- cron: "7,37 * * * *"')
smoke_path.write_text(smoke, encoding="utf-8")

policy_path = ROOT / "config/aggressive_operations_policy.json"
if policy_path.exists():
    import json

    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    policy["activity_targets"]["zero_activity_is_healthy_for_minutes"] = 30
    policy["repair"]["activity_aware"] = True
    policy["repair"]["dispatch_meta_supervisor_on_zero_activity"] = True
    policy_path.write_text(json.dumps(policy, indent=2) + "\n", encoding="utf-8")

policy_test = ROOT / "tests/test_aggressive_v5_policy.py"
body = policy_test.read_text(encoding="utf-8")
anchor = '    assert "server-health.yml" in controller\n'
addition = anchor + '    assert "zero_activity_unhealthy" in controller\n    assert "control-plane.yml" in controller\n'
if 'assert "zero_activity_unhealthy" in controller' not in body:
    if anchor not in body:
        raise RuntimeError("scheduler policy test anchor missing")
    body = body.replace(anchor, addition, 1)
policy_test.write_text(body, encoding="utf-8")

print("activity-aware scheduler governance applied")
