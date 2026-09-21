import ops.v7_maker_challenger_ssm as runner


def test_maker_challenger_execute_uses_literal_remote_template():
    source = __import__("inspect").getsource(runner.execute)
    assert 'command = r"""' in source
    assert 'command = f"""' not in source
    assert '.replace("__REMOTE__", remote)' in source
    assert '.replace("__RUN_ROOT__", run_root)' in source
