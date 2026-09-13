"""API 级测试：提交、检索、批次重审并列查看、配方版次并列查看。"""
from __future__ import annotations

from .factory import build_request


def _post(client, req):
    return client.post("/evaluate", json=req.model_dump(mode="json"))


def test_evaluate_and_retrieve(client):
    resp = _post(client, build_request())
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "PASS"
    eid = body["evaluation_id"]
    assert eid

    got = client.get(f"/evaluations/{eid}").json()
    assert got["batch_no"] == body["batch_no"]
    assert got["input_summary"]["readings_total"] == body["input_summary"]["readings_total"]
    assert got["input_summary"]["params"]["max_overshoot_c"] == 10.0
    # 每条判定都带引用读数
    assert all(f["citations"] for f in got["findings"])

    listed = client.get(f"/batches/{body['batch_no']}/evaluations").json()
    assert len(listed) == 1


def test_batch_rereview_compare(client):
    # 第一次：合格曲线（配方 R-A）
    r1 = _post(client, build_request(batch_no="B-REV", recipe_version="R-A"))
    id1 = r1.json()["evaluation_id"]
    # 重审：同一批次带超温尖峰（配方仍 R-A，代表复测数据）
    r2 = _post(client, build_request(batch_no="B-REV", recipe_version="R-A",
                                     spike={"at": 3540.0, "amount": 50.0}))
    id2 = r2.json()["evaluation_id"]
    assert r2.json()["status"] == "FAIL"

    history = client.get("/batches/B-REV/evaluations").json()
    assert [h["evaluation_id"] for h in history] == [id1, id2]

    cmp_resp = client.get(f"/compare?a={id2}&b={id1}").json()
    assert cmp_resp["same_verdict"] is False
    removed_codes = {d["code"] for d in cmp_resp["removed"]}
    added_codes = {d["code"] for d in cmp_resp["added"]}
    assert "OVERTEMP" in added_codes
    assert removed_codes == set()
    assert cmp_resp["status_a"] == "FAIL" and cmp_resp["status_b"] == "PASS"

    # 缺省 b：自动取同批次上一条
    auto = client.get(f"/compare?a={id2}").json()
    assert auto["b"]["evaluation_id"] == id1

    # 最新一条
    latest = client.get("/batches/B-REV/latest").json()
    assert latest["evaluation_id"] == id2


def test_recipe_versions_side_by_side(client):
    # 同批次分别以两个配方版次校核（29°C 尖峰到 929：1.1 OVERTEMP，1.0 不算超温）
    spike = {"at": 3540.0, "amount": 29.0}
    a = _post(client, build_request(batch_no="B-REC", recipe_version="R-A",
                                    rule_version="1.1.0", spike=spike))
    b = _post(client, build_request(batch_no="B-REC", recipe_version="R-B",
                                    rule_version="1.0.0", spike=spike))
    id_a, id_b = a.json()["evaluation_id"], b.json()["evaluation_id"]
    cmp_resp = client.get(f"/compare?a={id_a}&b={id_b}").json()
    assert {cmp_resp["a"]["recipe_version"], cmp_resp["b"]["recipe_version"]} == {"R-A", "R-B"}
    assert "OVERTEMP" in {d["code"] for d in cmp_resp["added"]}
    assert cmp_resp["same_verdict"] is False


def test_compare_different_batches_rejected(client):
    a = _post(client, build_request(batch_no="B1"))
    b = _post(client, build_request(batch_no="B2"))
    resp = client.get(f"/compare?a={a.json()['evaluation_id']}&b={b.json()['evaluation_id']}")
    assert resp.status_code == 422


def test_rule_versions_listed(client):
    body = client.get("/rules").json()
    assert body["default"] == "1.1.0"
    assert {v["version"] for v in body["versions"]} == {"1.0.0", "1.1.0"}


def test_unknown_rule_version_422(client):
    resp = _post(client, build_request(rule_version="9.9.9"))
    assert resp.status_code == 422


def test_structural_validation_422(client):
    # extra=forbid：拼错字段名直接 422
    payload = build_request().model_dump(mode="json")
    payload["batach_no"] = payload["batch_no"]
    resp = client.post("/evaluate", json=payload)
    assert resp.status_code == 422


def test_input_error_unordered_returned_201_and_stored(client):
    # 反序提交读数 → READINGS_NOT_ORDERED（致命 INPUT_ERROR，仍落库可追溯）
    req = build_request(batch_no="B-ORDER")
    req.readings = list(reversed(req.readings))
    resp = _post(client, req)
    assert resp.status_code == 201
    assert resp.json()["status"] == "INPUT_ERROR"
    assert resp.json()["first_violation"]["code"] == "READINGS_NOT_ORDERED"
    # 阶段结果未产出
    assert resp.json()["stages"] == []
    listed = client.get("/batches/B-ORDER/evaluations").json()
    assert len(listed) == 1


def test_unknown_sensor_reading(client):
    req = build_request(batch_no="B-UNK")
    req.readings[3] = req.readings[3].model_copy(update={"sensor_id": "GHOST"})
    resp = _post(client, req)
    body = resp.json()
    assert body["status"] == "INPUT_ERROR"
    assert body["first_violation"]["code"] == "UNKNOWN_SENSOR"
    assert body["first_violation"]["citations"][0]["sensor_id"] == "GHOST"
