"""适配方式 fit（contain 保留完整画面 / cover 居中方形）：

- 同一裁切框下两种 fit 是两个独立版本，互不复用；
- cover 的两档 WebP 必须确实是方形且边长恰为 256/512；
- 相同 (裁切框, fit) 复用同一版本；不同 fit 在途作业也不共享；
- 取消/发布顺序与守卫语义在带 fit 时保持一致。
"""
from __future__ import annotations

import io
import time

from PIL import Image

from conftest import make_png, submit, upload, wait_image, wait_job


def _sizes_by_target(version):
    return {d["size"]: (d["width"], d["height"]) for d in version["derivatives"]}


def test_contain_then_cover_same_box_are_distinct_square_versions(client):
    # 宽高明显不同的原图与裁切框：contain 必为长方形，cover 必为方形。
    image = upload(client, make_png(800, 600, (17, 88, 143, 255))).json()["image"]
    box = {"x": 100, "y": 50, "w": 400, "h": 300}

    # 1) 先保留完整画面。
    r1 = submit(client, image["id"], {**box, "fit": "contain"})
    assert r1.status_code == 202, r1.text
    v1 = wait_job(client, r1.json()["job"]["id"])["version"]
    assert v1["fit"] == "contain"
    assert v1["version_no"] == 1
    dims1 = _sizes_by_target(v1)
    assert dims1 == {256: (256, 192), 512: (512, 384)}

    # 2) 同一裁切框改为居中方形：必须新建作业（202），不能立即复用 v1。
    r2 = submit(client, image["id"], {**box, "fit": "cover"})
    assert r2.status_code == 202, r2.text
    assert r2.json()["reused"] is False
    job2 = wait_job(client, r2.json()["job"]["id"])
    assert job2["status"] == "published"
    v2 = job2["version"]
    assert v2["id"] != v1["id"]
    assert v2["fit"] == "cover"
    assert v2["version_no"] == 2
    # 方形：边长恰为目标，不再是长方形。
    dims2 = _sizes_by_target(v2)
    assert dims2 == {256: (256, 256), 512: (512, 512)}

    # 派生图字节确实是方形 WebP。
    for d in v2["derivatives"]:
        rr = client.get(d["url"])
        assert rr.status_code == 200
        assert rr.content[8:12] == b"WEBP"
        with Image.open(io.BytesIO(rr.content)) as out:
            assert out.size == (d["size"], d["size"])

    # contain 的产物没有被 cover 覆盖。
    for d in v1["derivatives"]:
        rr = client.get(d["url"])
        with Image.open(io.BytesIO(rr.content)) as out:
            assert out.size == (d["width"], d["height"])
            assert out.width != out.height

    # 页面当前展示方形版本。
    state = client.get(f"/api/images/{image['id']}").json()
    assert state["current_version"]["id"] == v2["id"]
    assert state["current_version"]["fit"] == "cover"


def test_same_fit_reuses_version_after_switching_back(client):
    image = upload(client, make_png(700, 500, (96, 42, 170, 255))).json()["image"]
    box = {"x": 0, "y": 0, "w": 400, "h": 200}

    rc = submit(client, image["id"], {**box, "fit": "contain"})
    vc = wait_job(client, rc.json()["job"]["id"])["version"]

    rv = submit(client, image["id"], {**box, "fit": "cover"})
    vv = wait_job(client, rv.json()["job"]["id"])["version"]
    assert vv["id"] != vc["id"]

    # 切回 contain：相同请求立即复用，不新建版本号。
    again = submit(client, image["id"], {**box, "fit": "contain"})
    assert again.status_code == 200
    assert again.json()["job"]["status"] == "published"
    assert again.json()["job"]["version"]["id"] == vc["id"]
    assert again.json()["job"]["version"]["version_no"] == 1
    state = client.get(f"/api/images/{image['id']}").json()
    assert state["current_version"]["id"] == vc["id"]

    # 再切 cover 同样复用 v2。
    again2 = submit(client, image["id"], {**box, "fit": "cover"})
    assert again2.status_code == 200
    assert again2.json()["job"]["version"]["id"] == vv["id"]


def test_distinct_fit_inflight_jobs_are_not_shared(client):
    image = upload(client, make_png(600, 600, (210, 120, 20, 255))).json()["image"]
    box = {"x": 0, "y": 0, "w": 300, "h": 200}

    ra = submit(client, image["id"], {**box, "fit": "contain"}, delay_ms=2000)
    job_a = ra.json()["job"]["id"]
    wait_image(
        client, image["id"],
        lambda b: b["current_job"] is not None
        and b["current_job"]["id"] == job_a
        and b["current_job"]["status"] == "processing",
    )

    # 同框但换 fit：必须是另一个在途作业，而不是复用 A。
    rb = submit(client, image["id"], {**box, "fit": "cover"}, delay_ms=0)
    assert rb.status_code == 202
    assert rb.json()["reused"] is False
    job_b = rb.json()["job"]["id"]
    assert job_b != job_a

    done_b = wait_job(client, job_b)
    assert done_b["status"] == "published"
    assert done_b["version"]["fit"] == "cover"

    # A 迟到完成：current_job 已指向 B，A 记为 superseded 而不是顶掉 B。
    final_a = wait_job(client, job_a, timeout=10)
    assert final_a["status"] == "superseded", final_a
    assert final_a["version"]["fit"] == "contain"
    assert final_a["version"]["id"] != done_b["version"]["id"]

    state = client.get(f"/api/images/{image['id']}").json()
    assert state["current_version"]["id"] == done_b["version"]["id"]

    # A（contain）的成果仍可独立复用。
    reuse = submit(client, image["id"], {**box, "fit": "contain"})
    assert reuse.status_code == 200
    assert reuse.json()["job"]["version"]["id"] == final_a["version"]["id"]


def test_cancel_then_submit_other_fit_publishes_independently(client):
    image = upload(client, make_png(500, 400, (34, 150, 90, 255))).json()["image"]
    box = {"x": 0, "y": 0, "w": 200, "h": 150}

    ra = submit(client, image["id"], {**box, "fit": "cover"}, delay_ms=2000)
    job_a = ra.json()["job"]["id"]
    wait_image(
        client, image["id"],
        lambda b: b["current_job"] is not None
        and b["current_job"]["id"] == job_a
        and b["current_job"]["status"] == "processing",
    )
    cancel = client.post(f"/api/jobs/{job_a}/cancel")
    assert cancel.status_code == 200

    # cover 取消后立即提交 contain：不应命中任何复用，独立发布成功。
    rb = submit(client, image["id"], {**box, "fit": "contain"})
    assert rb.status_code == 202
    done_b = wait_job(client, rb.json()["job"]["id"])
    assert done_b["status"] == "published"
    assert done_b["version"]["fit"] == "contain"
    assert done_b["version"]["version_no"] == 1

    time.sleep(2.5)
    assert client.get(f"/api/jobs/{job_a}").json()["status"] == "cancelled"
    state = client.get(f"/api/images/{image['id']}").json()
    assert state["current_version"]["id"] == done_b["version"]["id"]

    # 取消的 cover 没有留下可复用版本：再次提交仍是新作业并正常出方形。
    rc = submit(client, image["id"], {**box, "fit": "cover"})
    assert rc.status_code == 202
    done_c = wait_job(client, rc.json()["job"]["id"])
    assert done_c["status"] == "published"
    assert done_c["version"]["fit"] == "cover"
    assert done_c["version"]["version_no"] == 2
    assert _sizes_by_target(done_c["version"]) == {256: (256, 256), 512: (512, 512)}


def test_identical_fit_concurrent_requests_share_one_job(client):
    import concurrent.futures

    image = upload(client, make_png(640, 480, (180, 60, 130, 255))).json()["image"]
    box = {"x": 0, "y": 0, "w": 300, "h": 200, "fit": "cover"}
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(
            pool.map(lambda _: submit(client, image["id"], box), range(4))
        )
    job_ids = {r.json()["job"]["id"] for r in responses}
    assert len(job_ids) == 1, job_ids
    done = wait_job(client, next(iter(job_ids)))
    assert done["version"]["fit"] == "cover"
    assert _sizes_by_target(done["version"]) == {256: (256, 256), 512: (512, 512)}


def test_invalid_fit_rejected(client):
    image = upload(client, make_png(100, 100)).json()["image"]
    r = submit(client, image["id"],
               {"x": 0, "y": 0, "w": 50, "h": 50, "fit": "stretch"})
    assert r.status_code == 400
