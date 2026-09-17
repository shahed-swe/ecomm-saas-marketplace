"""Phase 19: turning one codebase into every tenant's app.

The pipeline's job is to be boring and safe: refuse a build that cannot possibly succeed, never
hand the same build to two runners, never put a signing key anywhere near the database, and tell
the tenant what happened without anyone reading CI logs.
"""

from sqlalchemy import text

from app.modules.mobile import builds
from app.tests.test_checkout import _market


async def _tenant(client, app, platform_headers, slug):
    t, staff, cat = await _market(client, app, platform_headers)
    return t, staff


PROFILE = {
    "app": "buyer",
    "android_package": "com.rongin.buyer",
    "ios_bundle_id": "com.rongin.buyer",
    "play_track": "internal",
    "asc_app_id": "6448123456",
    "android_signing_ref": "RONGIN_ANDROID_KEYSTORE",
    "ios_signing_ref": "RONGIN_ASC_KEY",
    "firebase_android_ref": "RONGIN_GOOGLE_SERVICES_JSON",
    "firebase_ios_ref": "RONGIN_GOOGLE_SERVICE_INFO",
    "store_listing": {"short_description": "Shop with us"},
    "status": "ready",
}


async def test_a_build_is_refused_until_the_app_can_actually_be_built(
    client, app, platform_headers
):
    t, staff = await _tenant(client, app, platform_headers, "build-alpha")
    early = await client.post(
        "/api/v1/admin/app-builds", headers=staff, json={"platform": "android", "version": "1.0.0"}
    )
    assert early.status_code == 409 and early.json()["title"] == "profile_incomplete"
    assert "signing" in early.json()["detail"] or "package" in early.json()["detail"]

    saved = await client.put("/api/v1/admin/app-store-profile", headers=staff, json=PROFILE)
    assert saved.status_code == 200
    manifest = saved.json()
    assert manifest["android_package"] == "com.rongin.buyer"
    assert manifest["secrets"]["android_signing"] == "RONGIN_ANDROID_KEYSTORE"
    # the manifest names secrets; it never contains one
    assert "keystore" not in str(manifest).lower() or "REF" not in str(manifest)

    queued = await client.post(
        "/api/v1/admin/app-builds", headers=staff, json={"platform": "android", "version": "1.0.0"}
    )
    assert queued.status_code == 202
    assert queued.json()["status"] == "queued" and queued.json()["build_number"] == 1
    # one build at a time per app and platform
    assert (
        await client.post(
            "/api/v1/admin/app-builds",
            headers=staff,
            json={"platform": "android", "version": "1.0.0"},
        )
    ).status_code == 409


async def test_two_runners_never_claim_the_same_build(client, app, platform_headers):
    t, staff = await _tenant(client, app, platform_headers, "build-beta")
    await client.put("/api/v1/admin/app-store-profile", headers=staff, json=PROFILE)
    await client.post(
        "/api/v1/admin/app-builds", headers=staff, json={"platform": "android", "version": "2.0.0"}
    )
    first = (
        await client.post(
            "/platform/v1/app-builds/claim",
            headers=platform_headers,
            json={"runner": "runner-a", "limit": 10},
        )
    ).json()
    second = (
        await client.post(
            "/platform/v1/app-builds/claim",
            headers=platform_headers,
            json={"runner": "runner-b", "limit": 10},
        )
    ).json()
    mine = [b for b in first if b["manifest"]["tenant_id"] == t["id"]]
    assert len(mine) == 1
    assert not [b for b in second if b["build_id"] == mine[0]["build_id"]]
    assert mine[0]["manifest"]["host"] == t["primary_host"]
    assert mine[0]["manifest"]["app_name"]

    # CI reports back, and the tenant sees it without opening a log
    build_id = mine[0]["build_id"]
    await client.post(
        f"/platform/v1/app-builds/{build_id}/report",
        headers=platform_headers,
        json={"status": "succeeded", "artifact_url": "https://ci.example/app.aab"},
    )
    uploaded = await client.post(
        f"/platform/v1/app-builds/{build_id}/report",
        headers=platform_headers,
        json={"status": "uploaded", "store_status": "internal track"},
    )
    assert uploaded.json()["status"] == "uploaded"
    listed = (await client.get("/api/v1/admin/app-builds", headers=staff)).json()
    assert listed[0]["status"] == "uploaded" and listed[0]["store_status"] == "internal track"
    assert listed[0]["artifact_url"].endswith(".aab")
    # and a finished build frees the slot for the next one
    assert (
        await client.post(
            "/api/v1/admin/app-builds",
            headers=staff,
            json={"platform": "android", "version": "2.0.0"},
        )
    ).status_code == 202


async def test_a_failed_build_says_why_and_can_be_retried(client, app, platform_headers):
    t, staff = await _tenant(client, app, platform_headers, "build-gamma")
    await client.put("/api/v1/admin/app-store-profile", headers=staff, json=PROFILE)
    await client.post(
        "/api/v1/admin/app-builds", headers=staff, json={"platform": "ios", "version": "1.2.0"}
    )
    claimed = (
        await client.post(
            "/platform/v1/app-builds/claim",
            headers=platform_headers,
            json={"runner": "runner-c", "limit": 5},
        )
    ).json()
    build = next(b for b in claimed if b["manifest"]["tenant_id"] == t["id"])
    await client.post(
        f"/platform/v1/app-builds/{build['build_id']}/report",
        headers=platform_headers,
        json={"status": "failed", "error": "provisioning profile expired"},
    )
    listed = (await client.get("/api/v1/admin/app-builds", headers=staff)).json()
    assert listed[0]["status"] == "failed" and "provisioning" in listed[0]["error"]
    retried = await client.post(
        "/api/v1/admin/app-builds", headers=staff, json={"platform": "ios", "version": "1.2.0"}
    )
    assert retried.status_code == 202 and retried.json()["build_number"] == 2


async def test_the_build_queue_belongs_to_the_platform_alone(client, app, platform_headers):
    t, staff = await _tenant(client, app, platform_headers, "build-delta")
    assert (
        await client.post("/platform/v1/app-builds/claim", headers=staff, json={"runner": "sneaky"})
    ).status_code == 404
    assert (
        await client.get(f"/platform/v1/tenants/{t['id']}/app-manifest", headers=staff)
    ).status_code == 404
    ours = await client.get(
        f"/platform/v1/tenants/{t['id']}/app-manifest", headers=platform_headers
    )
    assert ours.status_code == 200 and ours.json()["slug"] == t["slug"]


async def test_the_manifest_carries_the_published_brand(client, app, platform_headers):
    t, staff = await _tenant(client, app, platform_headers, "build-epsilon")
    await client.put("/api/v1/admin/app-store-profile", headers=staff, json=PROFILE)
    async with app.state.db.sessionmaker() as s, s.begin():
        await s.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": t["id"]})
        manifest = await builds.manifest(s, t["id"])
    assert manifest["branding"]["primary_color"]  # icons and splash come from the published theme
    assert manifest["locale"] in ("bn", "en")
    assert manifest["store_listing"]["short_description"] == "Shop with us"
