from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "v7-deploy-paper-server.yml"
SSM = ROOT / "ops" / "v7_london_ssm_deploy.py"


def test_ssm_is_fallback_after_pinned_in_memory_tailscale():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert 'TS_AUTHKEY_EPHEMERAL_VERIFIED' not in text
    assert 'mode=authkey' in text
    assert 'mode=ssm' in text
    assert 'AWS_ACCESS_KEY_ID: ${{ secrets.AWS_ACCESS_KEY_ID }}' in text
    assert 'AWS_SECRET_ACCESS_KEY: ${{ secrets.AWS_SECRET_ACCESS_KEY }}' in text
    assert 'AWS_SESSION_TOKEN: ${{ secrets.AWS_SESSION_TOKEN }}' in text
    assert 'No Tailscale or AWS SSM credential set' in text
    assert text.count('tailscale/github-action@780049a30b6ff5c378a9e7b389d15ece7a204888') == 3
    auth = text.split('- name: Join private tailnet with in-memory ephemeral auth key', 1)[1].split('- name:', 1)[0]
    assert 'authkey: ${{ secrets.TS_AUTHKEY }}' in auth
    assert "statedir: ''" in auth
    assert "Explicitly logout in-memory Tailscale CI node" in text
    assert "sudo tailscale logout" in text


def test_ssm_and_ssh_paths_are_mutually_exclusive():
    text = WORKFLOW.read_text(encoding="utf-8")
    ssh_config = text.split("- name: Configure SSH identity and await private server", 1)[1].split("- name:", 1)[0]
    ssh_deploy = text.split("- name: Reconcile exact approved main V7 SHA on server", 1)[1].split("- name:", 1)[0]
    ssm_deploy = text.split("- name: Reconcile exact approved main V7 SHA through AWS SSM", 1)[1].split("- name:", 1)[0]
    assert "steps.tailscale_auth.outputs.mode != 'ssm'" in ssh_config
    assert "steps.tailscale_auth.outputs.mode != 'ssm'" in ssh_deploy
    assert "steps.tailscale_auth.outputs.mode == 'ssm'" in ssm_deploy


def test_ssm_builds_artifact_on_ci_research_plane():
    text = WORKFLOW.read_text(encoding="utf-8")
    ssm = text.split("- name: Reconcile exact approved main V7 SHA through AWS SSM", 1)[1]
    assert 'research/build_runtime_artifacts.sh "$deploy_sha"' in ssm
    assert 'research-source-empty' in ssm
    assert 'research-archive-empty' in ssm
    assert 'research-durable' in ssm
    assert 'ops/v7_london_ssm_deploy.py' in ssm


def test_ssm_transport_reuses_canonical_cutover_scripts():
    text = SSM.read_text(encoding="utf-8")
    assert "ops/v7_london_stage_release.sh" in text
    assert "ops/v7_london_cutover.sh" in text
    assert "v7_exact_sha_ci_gate.py" not in text
    assert "'paper_only':True" in text
    assert "'authenticated_execution':False" in text
    assert "'real_order_submission':False" in text


def test_ssm_selection_is_fail_closed():
    text = SSM.read_text(encoding="utf-8")
    assert "multiple instances claim expected Tailscale IP" in text
    assert "cannot select unique London runtime" in text
    assert "EXACT_TAILSCALE_IP" in text
    assert "UNIQUE_ACTIVE_PAPER_SERVICE" in text
    assert "PAPER_SHADOW_ONLY" in text