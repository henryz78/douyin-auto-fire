from pathlib import Path


WORKFLOW = Path(__file__).parents[1] / ".github" / "workflows" / "send.yml"


def test_workflow_persists_encrypted_storage_state_without_raw_upload() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "actions/cache/restore@v4" in workflow
    assert "actions/cache/save@v4" in workflow
    assert "DOUYIN_STORAGE_STATE_KEY" in workflow
    assert "--symmetric --cipher-algo AES256" in workflow
    assert "storage-state.next.json" in workflow
    assert "path: storage-state.enc" in workflow
    assert "path: storage-state.json" not in workflow
    assert "continue-on-error: true" in workflow


def test_workflow_has_explicit_storage_state_reset() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "reset_storage_state:" in workflow
    assert "RESET_STORAGE_STATE" in workflow
    assert "已请求忽略上一份 Storage State" in workflow


def test_workflow_retries_only_safe_pre_send_failures() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "delays=(30 120 300)" in workflow
    assert "for attempt in 1 2 3 4" in workflow
    assert '"$status" -ne 3' in workflow
