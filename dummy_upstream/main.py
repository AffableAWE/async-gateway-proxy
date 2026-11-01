"""A small backend for local testing of the gateway.

Run it on its own port and point a route at it:
    uvicorn dummy_upstream.main:app --port 8001

It stands in for your C++ server: HTTP in, HTTP out.
"""

import asyncio
from collections import defaultdict

from fastapi import FastAPI, Query, Request, Response

app = FastAPI(title="dummy-upstream")


@app.get("/")
async def root() -> dict:
    return {"service": "dummy_upstream", "endpoint": "root"}


@app.get("/items/{item_id}")
async def get_item(item_id: int) -> dict:
    return {"item_id": item_id, "name": f"item-{item_id}"}


@app.api_route("/echo", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def echo(request: Request) -> dict:
    """Returns what it received, so you can check what the gateway forwards:
    which headers survived, what X-Forwarded-For says, whether the body arrived.
    """
    body = await request.body()
    return {
        "method": request.method,
        "path": request.url.path,
        "query": request.url.query,
        "headers": dict(request.headers),
        "body": body.decode(errors="replace"),
    }


@app.get("/slow")
async def slow(delay: float = Query(2.0, ge=0, le=30)) -> dict:
    """Waits `delay` seconds, then answers. Lets you trigger a real 504:
    with the gateway's 5 s timeout, /api/slow?delay=6 times out.

    asyncio.sleep, not time.sleep: time.sleep would block this server's
    event loop, and every other request to it would stall too.
    """
    await asyncio.sleep(delay)
    return {"slept_seconds": delay}


# Calls seen so far per /flaky key. In-process only: restarting resets it.
_flaky_calls: defaultdict[str, int] = defaultdict(int)


@app.get("/flaky")
async def flaky(key: str = "default", fail: int = Query(2, ge=0, le=100)) -> Response:
    """Answers 503 for the first `fail` calls with this key, then 200.

    Shows retries working: through the gateway, /api/flaky?key=a&fail=2
    succeeds on the third attempt, and the client never sees the 503s.
    Use a new key each time to start over.
    """
    _flaky_calls[key] += 1
    call = _flaky_calls[key]
    if call <= fail:
        return Response(status_code=503, content=f"failing on purpose ({call}/{fail})")
    return Response(content=f'{{"ok": true, "call": {call}}}', media_type="application/json")


@app.get("/status/{code}")
async def status(code: int) -> Response:
    """Answers with any status code: /status/503, /status/500, /status/404..."""
    if not 200 <= code <= 599:
        return Response(status_code=400, content="code must be 200-599")
    return Response(status_code=code, content=f"status {code}")
