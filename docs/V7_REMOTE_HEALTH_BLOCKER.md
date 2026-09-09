# Remote PAPER health: verified external blocker

Inspected on 9 September 2026. GitHub health run `34338632981`, job `102423871558`, passed the requested-SHA gate but failed during the Tailscale auth-key fallback. Its SSH and live-health steps did not run. The exact service error was `backend error: node quota reached on this tailnet; please contact support`.

The local Tailscale node reported 999 visible peers, including 997 peers with CI-style `gh-` / `github-` names. All 997 were offline and marked expired. This is the inventory visible to the existing node, not an administrator API inventory. Names and expiry do not prove the nodes' ephemeral attributes. The user subsequently explicitly authorized deletion of GitHub runner nodes.

Evidence is retained in `runs/permanent_evidence_20260909/remote-health-blocker.json`, `tailscale-visible-inventory.json` and the referenced GitHub job. No nodes or credentials were deleted or changed. The existing local PAPER runtime continued separately from this failed remote monitoring path.

Health, universe-archive and deployment workflows already prefer OIDC, then OAuth, over auth-key fallback. The proposed change prevents fallback unless repository variable `TS_AUTHKEY_EPHEMERAL_VERIFIED=true` explicitly records administrator verification. This guard cannot remove the existing quota exhaustion.

The unresolved external action is precise: a tailnet administrator must inspect and retire confirmed obsolete CI nodes, then configure tagged OIDC/OAuth credentials or verify that the fallback key is reusable, ephemeral and correctly tagged. The task tools do not expose administrator credentials. After that change, rerun both workflows against the exact deployed SHA and verify successful SSH, PAPER health and runner-node removal.

Tailscale documents ephemeral CI use and credential options in the [GitHub Action documentation](https://tailscale.com/docs/integrations/github/github-action). Ephemeral cleanup behavior is described in [Ephemeral nodes](https://tailscale.com/docs/features/ephemeral-nodes). A successful post-action cleanup log alone does not prove every historical CI node was ephemeral or removed.

## Authorized CI cleanup

A fresh local inventory still shows 997 expired, offline peers, all named `github-runnervm…`, and three non-CI nodes including this host. The exact 997 candidate IDs are frozen in `runs/permanent_evidence_20260909/tailscale-ci-deletion-scope.json`. The user authorized their removal.

`scripts/v7_tailscale_ci_cleanup.py` prepares and applies that exact scope using an administrative API token file. Before each DELETE it checks current administrative identity, expiry, inactivity, tags, route absence and current local offline/expired state. It retains a write-ahead audit and verifies the final inventory, including preservation of other nodes. Two deterministic tests cover identity/state exclusions and plan/apply behavior. These tests are not proof of actual removal.

No administrative token is currently available in the task environment; the user has been asked only for the path to an existing token file. No nodes have yet been deleted. A node enrollment auth key cannot substitute for administrative API access. See the official [device removal procedure](https://tailscale.com/docs/features/access-control/device-management/how-to/remove).
