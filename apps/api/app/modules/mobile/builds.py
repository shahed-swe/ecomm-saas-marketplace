"""The white-label build pipeline's half of the contract.

CI needs three things from us and gives one back:

* a **manifest** per tenant app: the bundle ids, the API host, the brand colours and logo, and the
  *names* of the secrets to resolve — never the secrets themselves, which live only in CI;
* a **queue** to pull from, so one workflow run can build whatever is waiting;
* somewhere to **report** what happened, so a tenant can see "uploaded to internal track" without
  anyone reading CI logs.

Signing keys, service-account JSON and App Store keys are referenced by name and resolved by the
runner from its own secret store. A database that never held them cannot leak them.
"""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError, NotFound


class BuildError(AppError):
    status = 409
    code = "build_conflict"


async def manifest(db: AsyncSession, tenant_id: str, *, app: str = "buyer") -> dict:
    """Everything a build needs to become one tenant's app, and nothing a build does not."""
    row = (
        (
            await db.execute(
                text(
                    """SELECT t.id, t.slug, t.name, t.default_locale,
                              (SELECT host FROM domains d WHERE d.tenant_id = t.id AND d.is_primary) AS host,
                              p.android_package, p.ios_bundle_id, p.play_track, p.asc_app_id,
                              p.android_signing_ref, p.ios_signing_ref, p.firebase_android_ref,
                              p.firebase_ios_ref, p.store_listing, p.status,
                              c.app_name, c.icon_url, c.splash_url, c.crash_dsn
                       FROM tenants t
                       LEFT JOIN app_store_profiles p ON p.tenant_id = t.id AND p.app = :a
                       LEFT JOIN app_configs c ON c.tenant_id = t.id AND c.app = :a
                       WHERE t.id = :t"""
                ),
                {"t": tenant_id, "a": app},
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        raise NotFound("Not found")
    # The icon and splash are generated from the published brand colours, so a build looks like
    # the shop does. A tenant that has not published a theme yet still builds, with defaults.
    document = (
        await db.execute(
            text(
                """SELECT v.document FROM tenant_themes th
                   JOIN theme_versions v ON v.id = th.published_version_id AND v.tenant_id = th.tenant_id
                   WHERE th.tenant_id = :t"""
            ),
            {"t": tenant_id},
        )
    ).scalar() or {}
    colors = (document.get("tokens") or {}).get("colors") or {}
    return {
        "tenant_id": str(row["id"]),
        "slug": row["slug"],
        "app": app,
        "app_name": row["app_name"] or row["name"],
        "locale": row["default_locale"],
        "host": row["host"],
        "android_package": row["android_package"],
        "ios_bundle_id": row["ios_bundle_id"],
        "play_track": row["play_track"],
        "asc_app_id": row["asc_app_id"],
        "secrets": {
            "android_signing": row["android_signing_ref"],
            "ios_signing": row["ios_signing_ref"],
            "firebase_android": row["firebase_android_ref"],
            "firebase_ios": row["firebase_ios_ref"],
        },
        "branding": {
            "icon_url": row["icon_url"],
            "splash_url": row["splash_url"],
            "primary_color": colors.get("primary"),
            "background_color": colors.get("bg"),
        },
        "crash_dsn": row["crash_dsn"],
        "store_listing": row["store_listing"] or {},
        "profile_status": row["status"],
    }


def _ready(manifest_row: dict, platform: str) -> list[str]:
    """What is still missing before this tenant's app can be built for a store."""
    missing = []
    if not manifest_row.get("host"):
        missing.append("primary domain")
    if platform == "android":
        if not manifest_row.get("android_package"):
            missing.append("android package name")
        if not manifest_row["secrets"].get("android_signing"):
            missing.append("android signing secret")
    else:
        if not manifest_row.get("ios_bundle_id"):
            missing.append("ios bundle id")
        if not manifest_row["secrets"].get("ios_signing"):
            missing.append("ios signing secret")
    return missing


async def request_build(
    db: AsyncSession,
    tenant_id: str,
    *,
    app: str,
    platform: str,
    version: str,
    actor_id: str,
) -> dict:
    """Queue a build. Refused with a list of what is missing rather than failing in CI ten minutes later."""
    profile = await manifest(db, tenant_id, app=app)
    missing = _ready(profile, platform)
    if missing:
        raise BuildError(
            "This app is not ready to build: " + ", ".join(missing), code="profile_incomplete"
        )
    in_flight = (
        await db.execute(
            text(
                """SELECT id FROM app_builds WHERE tenant_id = :t AND app = :a AND platform = :p
                   AND status IN ('queued','building')"""
            ),
            {"t": tenant_id, "a": app, "p": platform},
        )
    ).scalar()
    if in_flight:
        raise BuildError("A build for this app is already in progress", code="build_in_flight")
    build_number = (
        await db.execute(
            text(
                """SELECT coalesce(max(build_number), 0) + 1 FROM app_builds
                   WHERE tenant_id = :t AND app = :a AND platform = :p AND version = :v"""
            ),
            {"t": tenant_id, "a": app, "p": platform, "v": version},
        )
    ).scalar()
    build_id = (
        await db.execute(
            text(
                """INSERT INTO app_builds (tenant_id, app, platform, version, build_number, requested_by)
                   VALUES (:t, :a, :p, :v, :n, :by) RETURNING id"""
            ),
            {
                "t": tenant_id,
                "a": app,
                "p": platform,
                "v": version,
                "n": build_number,
                "by": actor_id,
            },
        )
    ).scalar()
    return {
        "id": str(build_id),
        "status": "queued",
        "version": version,
        "build_number": build_number,
        "platform": platform,
        "app": app,
    }


async def claim_next(db: AsyncSession, *, runner: str, limit: int = 1) -> list[dict]:
    """Platform-wide: hand the next queued build(s) to a runner, marking them as taken.

    `FOR UPDATE SKIP LOCKED` is what makes two runners safe: neither can claim the same build.
    """
    rows = (
        (
            await db.execute(
                text(
                    """WITH next AS (
                         SELECT id FROM app_builds WHERE status = 'queued'
                         ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT :l
                       )
                       UPDATE app_builds b SET status = 'building', ci_ref = :r, started_at = now()
                       FROM next WHERE b.id = next.id
                       RETURNING b.id, b.tenant_id, b.app, b.platform, b.version, b.build_number"""
                ),
                {"l": limit, "r": runner},
            )
        )
        .mappings()
        .all()
    )
    out = []
    for row in rows:
        profile = await manifest(db, str(row["tenant_id"]), app=row["app"])
        out.append(
            {
                "build_id": str(row["id"]),
                "platform": row["platform"],
                "version": row["version"],
                "build_number": row["build_number"],
                "manifest": profile,
            }
        )
    return out


async def report(
    db: AsyncSession,
    build_id,
    *,
    status: str,
    artifact_url: str | None = None,
    store_status: str | None = None,
    error: str | None = None,
) -> dict:
    """CI reports back. Terminal states stamp a finish time; nothing else is trusted from the runner."""
    if status not in ("building", "succeeded", "failed", "uploaded", "rejected"):
        raise BuildError("Unknown build status", status=422, code="bad_status")
    row = (
        (
            await db.execute(
                text(
                    """UPDATE app_builds SET status = :s, artifact_url = coalesce(:a, artifact_url),
                              store_status = coalesce(:ss, store_status), error = :e,
                              finished_at = CASE WHEN :s IN ('succeeded','failed','uploaded','rejected')
                                                 THEN now() ELSE finished_at END
                       WHERE id = :i RETURNING tenant_id, app, platform, version, build_number, status"""
                ),
                {"s": status, "a": artifact_url, "ss": store_status, "e": error, "i": build_id},
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        raise NotFound("Not found")
    return {k: (str(v) if k == "tenant_id" else v) for k, v in row.items()}
