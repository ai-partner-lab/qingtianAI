"""Real public HTTP/browser flow; only the dedicated executor is synthetic."""
import json

if __package__:
    from .test_evolution_release_batches import payload
else:
    from test_evolution_release_batches import payload


def check_evolution_views(page, url, receipt_dir, summary, expect):
    page.set_viewport_size({"width": 1440, "height": 1000})
    while page.locator("dialog[open]").count():
        page.keyboard.press("Escape")
    created = page.request.post(url + "/api/tasks", data={"title": "Synthetic browser admission", "model": "gpt-5.6-sol", "reasoning": "high", "speed": "standard"})
    assert created.status == 201
    task_id = created.json()["id"]
    assert page.request.post(url + "/api/tasks/" + task_id + "/plan", data={}).status == 200
    before = page.request.get(url + "/api/tasks/" + task_id).json()
    assert before["runs"] == []
    page.locator("#clarityViewButton").click()
    page.locator("#clarityRefresh").click()
    card = page.locator('[data-clarity-task="' + task_id + '"]')
    expect(card).to_contain_text("Synthetic browser admission")
    expect(card).to_contain_text("下一动作")
    expect(card).to_contain_text("未登记")
    card.get_by_role("button", name="说明更正（预览后人工确认）", exact=True).click()
    for key, value in {
        "clarityReason": "Synthetic display clarification", "clarityNextAction": "Review synthetic scope",
        "clarityCorrectionReason": "Synthetic independent review", "clarityActor": "synthetic-reviewer",
        "claritySourceRef": "synthetic:browser-review", "claritySourceHash": "a" * 64,
        "clarityObservedAt": "2026-01-01T00:00:00Z", "clarityReviewedAt": "2026-01-01T00:01:00Z",
        "clarityReviewer": "synthetic-reviewer",
    }.items():
        page.locator("#" + key).fill(value)
    page.locator("#clarityCorrectionPreview").click()
    expect(page.locator("#clarityCorrectionConfirm")).to_be_enabled()
    preview = page.request.get(url + "/api/tasks/" + task_id).json()
    assert preview["blocking_reason"] == before["blocking_reason"]
    page.locator("#clarityCorrectionConfirm").click()
    expect(page.locator("#clarityCorrectionFeedback")).to_contain_text("已登记独立展示说明")
    page.locator("#clarityCorrectionClose").click()
    after = page.request.get(url + "/api/tasks/" + task_id).json()
    assert (after["state"], after["blocking_reason"], after["runs"]) == (before["state"], before["blocking_reason"], [])
    page.screenshot(path=str(receipt_dir / "evolution-clarity.png"), full_page=True, animations="disabled")

    page.locator("#taskViewButton").click()
    page.locator('.task-card[data-task-id="' + task_id + '"]').click()
    page.get_by_label("本次明确执行指令", exact=True).fill("Synthetic receipt-only executor; no model or process")
    page.get_by_role("button", name="确认范围并手动派发", exact=True).click()
    expect(page.locator(".admission-dispatch")).to_contain_text("synthetic-browser-run")
    first = page.request.get(url + "/api/tasks/" + task_id + "/admission").json()
    assert first["receipt"]["state"] == "queued"
    page.get_by_role("button", name="用原请求核对 / 重试", exact=True).click()
    expect(page.locator(".admission-dispatch")).to_contain_text("synthetic-browser-run")
    second = page.request.get(url + "/api/tasks/" + task_id + "/admission").json()
    assert first["receipt"] == second["receipt"] and first["history"] == second["history"]
    assert len(page.request.get(url + "/api/tasks/" + task_id).json()["runs"]) == 1
    page.screenshot(path=str(receipt_dir / "evolution-admission.png"), full_page=True, animations="disabled")
    page.get_by_role("button", name="关闭任务详情", exact=True).click()

    page.locator("#releaseViewButton").click()
    expect(page.locator("#releaseEmpty")).to_be_visible()
    page.locator("#releaseRecordButton").click()
    batch = payload(key="synthetic-browser-release", task_ids=[task_id])
    page.locator("#releaseReceiptInput").fill(json.dumps(batch))
    page.locator("#releasePreviewButton").click()
    expect(page.locator("#releaseConfirmButton")).to_be_enabled()
    assert page.request.get(url + "/api/release-batches").json()["total"] == 0
    page.locator("#releaseConfirmButton").click()
    expect(page.locator("#releaseRecordFeedback")).to_contain_text("已登记人工回执")
    page.locator("#releaseRecordClose").click()
    expect(page.locator("#releaseBatches")).to_contain_text("Synthetic batch")
    listing = page.request.get(url + "/api/release-batches").json()
    assert listing["total"] == 1
    assert listing["batches"][0]["counts"] == {"total": 1, "deployed": 1, "enabled": 0, "accepted": 0, "rolled_back": 0, "unverified": 0}
    assert page.request.get(url + "/api/tasks/" + task_id).json()["state"] == "QUEUED"
    page.screenshot(path=str(receipt_dir / "evolution-release.png"), full_page=True, animations="disabled")
    page.set_viewport_size({"width": 390, "height": 844})
    expect(page.locator("#releaseViewButton")).to_be_visible()
    page.locator("#clarityViewButton").click()
    page.locator("#clarityRefresh").click()
    expect(card).to_be_visible()
    page.screenshot(path=str(receipt_dir / "evolution-mobile.png"), full_page=True, animations="disabled")
    summary["evolution"] = {"task_id": task_id, "release_id": listing["batches"][0]["id"],
                            "receipt_id": first["receipt"]["id"], "synthetic_executor": True,
                            "real_http_and_browser": True, "real_model_execution": False}
    summary["steps"].extend(["real_clarity_preview_and_correction_preserve_native_fields", "real_dispatch_receipt_retry_keeps_single_synthetic_run", "real_release_preview_register_separates_three_facts", "real_evolution_views_mobile"])
