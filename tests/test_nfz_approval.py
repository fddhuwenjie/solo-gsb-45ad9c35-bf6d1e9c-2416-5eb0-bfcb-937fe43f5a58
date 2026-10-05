# -*- coding: utf-8 -*-
"""禁飞区变更核准（POST /api/nfz-approval）回归测试。

覆盖：
- 尚未发布 → 400 no_release；字段缺失 / nfzs 非列表 → 400 missing_fields；
- 核准通过：候选禁飞区与最新发布快照航线组合审查，仅原子替换工作区禁飞区、
  修订号递增，最新工作区航线与发布快照均保留；
- 审查以发布快照航线为准（与工作区当前航线无关）；
- 审查错误 / 审查冲突（附明细）→ 工作区与发布均不变、修订号不变；
- 工作区修订号或已发布号变化 → 409 并附 expected/actual 明细。
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app  # noqa: E402

# 干净航线：4km 直线（y=100），200m 等高，08:00 起飞
ROUTE = {
    "departureTime": "2026-09-24T08:00",
    "unitScale": 100,
    "corridorHalfWidth": 0.3,
    "speeds": [25],
    "waypoints": [
        {"id": "w1", "name": "A", "x": 100, "y": 100, "alt": 200},
        {"id": "w2", "name": "B", "x": 500, "y": 100, "alt": 200},
    ],
}
# 发布后工作区改飞的新航线：整体平移到 y=900（与发布快照不同）
ROUTE2 = {
    "departureTime": "2026-09-24T08:00",
    "unitScale": 100,
    "corridorHalfWidth": 0.3,
    "speeds": [25],
    "waypoints": [
        {"id": "w1", "name": "A", "x": 100, "y": 900, "alt": 200},
        {"id": "w2", "name": "B", "x": 500, "y": 900, "alt": 200},
    ],
}
# 覆盖发布航线（y=100）中段、时窗覆盖全程的禁飞区（与 ROUTE 必冲突，与 ROUTE2 不冲突）
BLOCK_NFZ = {
    "id": "nz1", "name": "阻断区", "enabled": True,
    "vertices": [[250, 50], [350, 50], [350, 150], [250, 150]],
    "minAlt": 0, "maxAlt": 400,
    "startTime": "2026-09-24T07:00", "endTime": "2026-09-24T09:00",
}
# 只覆盖工作区新航线（y=900）的禁飞区（与 ROUTE2 必冲突，与 ROUTE 不冲突）
NFZ_Y900 = {
    "id": "nz9", "name": "低空区-900", "enabled": True,
    "vertices": [[250, 850], [350, 850], [350, 950], [250, 950]],
    "minAlt": 0, "maxAlt": 400,
    "startTime": "2026-09-24T07:00", "endTime": "2026-09-24T09:00",
}
# 与两条航线都不相交的安全禁飞区（高空 500m+，平面上也错开）
SAFE_NFZ = {
    "id": "nzS", "name": "高空限制区", "enabled": True,
    "vertices": [[700, 140], [830, 140], [830, 220], [700, 220]],
    "minAlt": 500, "maxAlt": 1200,
    "startTime": "", "endTime": "",
}


def clone(obj):
    return json.loads(json.dumps(obj))


class NfzApprovalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.old_path = app.DB_PATH
        app.DB_PATH = os.path.join(self.tmp, "test.db")
        app.init_db()
        self.client = app.app.test_client()

    def tearDown(self):
        app.DB_PATH = self.old_path
        shutil.rmtree(self.tmp, ignore_errors=True)

    # —— 辅助 ——
    def put_state(self, state):
        r = self.client.put("/api/state", json=state)
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertTrue(body["ok"])
        return body["rev"]

    def save_version(self, state, name="v1"):
        r = self.client.post("/api/versions", json={"name": name, "state": state})
        self.assertEqual(r.status_code, 200)
        return r.get_json()["id"]

    def publish(self, vid, rev, seq):
        return self.client.post("/api/release", json={
            "versionId": vid, "workspaceRev": rev, "releaseSeq": seq})

    def approve(self, nfzs, rev, seq):
        return self.client.post("/api/nfz-approval", json={
            "nfzs": nfzs, "workspaceRev": rev, "releaseSeq": seq})

    def workspace(self):
        """当前工作区状态与修订号。"""
        r = self.client.get("/api/state")
        self.assertEqual(r.status_code, 200)
        return r.get_json(), int(r.headers["X-Workspace-Rev"])

    def current_release(self):
        r = self.client.get("/api/release")
        self.assertEqual(r.status_code, 200)
        return r.get_json()

    def make_release(self):
        """空禁飞区工作区 + ROUTE 版本发布，返回 (发布时工作区修订号, 版本 id)。"""
        rev = self.put_state({"route": {"waypoints": []}, "nfzs": []})
        vid = self.save_version({"route": ROUTE, "nfzs": []}, name="发布源")
        r = self.publish(vid, rev, 0)
        self.assertEqual(r.status_code, 200, r.get_json())
        return rev, vid

    # —— 前置与参数校验 ——
    def test_no_release_rejected(self):
        rev = self.put_state({"route": {"waypoints": []}, "nfzs": []})
        r = self.approve([], rev, 0)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()["reason"], "no_release")

    def test_missing_fields(self):
        self.make_release()
        r = self.client.post("/api/nfz-approval", json={"workspaceRev": 1})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()["reason"], "missing_fields")
        r = self.client.post("/api/nfz-approval", json={
            "nfzs": {"id": "x"}, "workspaceRev": 1, "releaseSeq": 1})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()["reason"], "missing_fields")
        r = self.client.post("/api/nfz-approval", json={
            "nfzs": [], "workspaceRev": "abc", "releaseSeq": 1})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()["reason"], "missing_fields")

    # —— 核准通过：仅替换工作区禁飞区，保留工作区航线与发布快照 ——
    def test_approve_replaces_only_workspace_nfzs(self):
        rev, _vid = self.make_release()
        # 发布后工作区航线被继续编辑（模拟自动保存落库）
        rev2 = self.put_state({"route": clone(ROUTE2), "nfzs": []})
        self.assertEqual(rev2, rev + 1)

        r = self.approve([clone(SAFE_NFZ)], rev2, 1)
        self.assertEqual(r.status_code, 200, r.get_json())
        body = r.get_json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["rev"], rev2 + 1)  # 修订号递增
        self.assertEqual(body["releaseSeq"], 1)  # 发布号不变

        # 工作区：禁飞区被原子替换为候选，航线保留工作区最新（非发布快照）
        state, ws_rev = self.workspace()
        self.assertEqual(ws_rev, rev2 + 1)
        self.assertEqual(state["nfzs"], [SAFE_NFZ])
        self.assertEqual(state["route"]["waypoints"], ROUTE2["waypoints"])

        # 发布快照不变
        cur = self.current_release()
        self.assertEqual(cur["releaseSeq"], 1)
        self.assertEqual(cur["release"]["state"]["route"]["waypoints"], ROUTE["waypoints"])
        self.assertEqual(cur["release"]["state"]["nfzs"], [])

    def test_approval_review_uses_release_route_not_workspace(self):
        rev, _vid = self.make_release()
        # 工作区航线改到 y=900（与发布快照 y=100 不同）
        rev2 = self.put_state({"route": clone(ROUTE2), "nfzs": []})

        # 候选只与工作区航线冲突、与发布航线不冲突 → 核准通过
        r = self.approve([clone(NFZ_Y900)], rev2, 1)
        self.assertEqual(r.status_code, 200, r.get_json())
        rev3 = r.get_json()["rev"]

        # 候选与发布航线冲突（尽管与工作区航线不冲突）→ 核准拒绝
        r = self.approve([clone(BLOCK_NFZ)], rev3, 1)
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.get_json()["reason"], "analysis_conflicts")

        # 工作区保持上一次核准的结果，修订号不再变化
        state, ws_rev = self.workspace()
        self.assertEqual(ws_rev, rev3)
        self.assertEqual(state["nfzs"], [NFZ_Y900])
        self.assertEqual(state["route"]["waypoints"], ROUTE2["waypoints"])

    def test_approval_does_not_touch_release(self):
        rev, _vid = self.make_release()
        before = self.current_release()["release"]
        r = self.approve([clone(SAFE_NFZ)], rev, 1)
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(self.current_release()["release"], before)

    # —— 失败情形：工作区与发布均不变 ——
    def test_analysis_errors_reject(self):
        rev, _vid = self.make_release()
        bad = clone(SAFE_NFZ)
        bad["vertices"] = [[1, 2], [3, 4]]  # 顶点少于 3 个
        r = self.approve([bad], rev, 1)
        self.assertEqual(r.status_code, 400)
        body = r.get_json()
        self.assertEqual(body["reason"], "analysis_errors")
        self.assertTrue(body["errors"])
        state, ws_rev = self.workspace()
        self.assertEqual(ws_rev, rev)  # 修订号不变
        self.assertEqual(state["nfzs"], [])

    def test_analysis_conflicts_reject_with_details(self):
        rev, _vid = self.make_release()
        r = self.approve([clone(BLOCK_NFZ)], rev, 1)
        self.assertEqual(r.status_code, 409)
        body = r.get_json()
        self.assertEqual(body["reason"], "analysis_conflicts")
        self.assertTrue(body["entries"])
        self.assertEqual(body["entries"][0]["nfzId"], "nz1")
        self.assertEqual(body["first"]["nfzId"], "nz1")
        # 工作区与发布均不变
        state, ws_rev = self.workspace()
        self.assertEqual(ws_rev, rev)
        self.assertEqual(state["nfzs"], [])
        self.assertEqual(self.current_release()["release"]["state"]["nfzs"], [])

    def test_stale_workspace_rev(self):
        rev, _vid = self.make_release()
        rev2 = self.put_state({"route": {}, "nfzs": [{"note": "changed"}]})
        self.assertEqual(rev2, rev + 1)
        r = self.approve([clone(SAFE_NFZ)], rev, 1)  # 提交旧修订号
        self.assertEqual(r.status_code, 409)
        body = r.get_json()
        self.assertEqual(body["reason"], "stale")
        self.assertEqual(body["details"]["workspaceRev"],
                         {"expected": rev, "actual": rev2})
        state, ws_rev = self.workspace()
        self.assertEqual(ws_rev, rev2)
        self.assertEqual(state["nfzs"], [{"note": "changed"}])

    def test_stale_release_seq(self):
        rev, _vid = self.make_release()
        r = self.approve([clone(SAFE_NFZ)], rev, 0)  # 已发布号已是 1
        self.assertEqual(r.status_code, 409)
        body = r.get_json()
        self.assertEqual(body["reason"], "stale")
        self.assertEqual(body["details"]["releaseSeq"],
                         {"expected": 0, "actual": 1})
        state, ws_rev = self.workspace()
        self.assertEqual(ws_rev, rev)
        self.assertEqual(state["nfzs"], [])

    # —— 既有行为不回归：未发布时禁飞区仍随工作区自动保存 ——
    def test_workspace_autosave_unchanged_before_release(self):
        rev = self.put_state({"route": clone(ROUTE), "nfzs": [clone(BLOCK_NFZ)]})
        state, ws_rev = self.workspace()
        self.assertEqual(ws_rev, rev)
        self.assertEqual(state["nfzs"], [BLOCK_NFZ])
        self.assertIsNone(self.current_release()["release"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
