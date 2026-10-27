"""A small backend for local testing of the gateway.

Run it on its own port and point a route at it:
    uvicorn dummy_upstream.main:app --port 8001

It stands in for your C++ server: HTTP in, HTTP out.
"""

from fastapi import FastAPI, Request

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
