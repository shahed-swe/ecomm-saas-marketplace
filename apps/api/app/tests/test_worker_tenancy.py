import pytest

from app.workers.settings import MissingTenantError, enqueue_tenant_job, tenant_job


@tenant_job
async def _sample(ctx, *, tenant_id):
    return tenant_id


async def test_tenant_job_requires_tenant_id():
    with pytest.raises(MissingTenantError):
        await _sample({})
    assert await _sample({}, tenant_id="t1") == "t1"


async def test_enqueue_without_tenant_fails_before_redis():
    class Pool:
        called = False

        async def enqueue_job(self, *a, **k):
            self.called = True

    pool = Pool()
    with pytest.raises(MissingTenantError):
        await enqueue_tenant_job(pool, "x", tenant_id="")
    assert pool.called is False
