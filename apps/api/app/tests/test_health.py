async def test_healthz(client):
    r = await client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}
    assert len(r.headers["x-request-id"]) >= 8


async def test_readyz_checks_db_and_redis(client):
    r = await client.get("/readyz")
    assert r.status_code == 200, r.text
    assert r.json()["checks"] == {"db": "ok", "redis": "ok"}


async def test_request_id_is_propagated(client):
    r = await client.get("/healthz", headers={"x-request-id": "abc12345-trace"})
    assert r.headers["x-request-id"] == "abc12345-trace"


async def test_unknown_route_is_problem_json(client):
    r = await client.get("/nope")
    assert r.status_code == 404
    assert r.headers["content-type"].startswith("application/problem+json")
