"""How the gateway process is started: `python -m app`.

One place for the server flags that matter for a gateway, so a Dockerfile,
a Kubernetes manifest or a person typing commands can't forget one:

    proxy_headers=False   Don't trust X-Forwarded-For from clients. By default
                          uvicorn trusts it from 127.0.0.1 and rewrites the
                          client address, so a caller could fake its IP.
    access_log=False      The gateway logs its own request_completed line.
    timeout_graceful_shutdown
                          On SIGTERM uvicorn stops accepting connections and
                          waits for requests already in flight, streams
                          included, then runs the app's shutdown. This caps
                          the wait, so a never-ending stream can't block a
                          deploy forever. Kubernetes allows 30 s by default
                          before it sends SIGKILL, so stay under that.

Settings come from environment variables, the usual way to configure a
process inside a container:

    GATEWAY_HOST                       default 127.0.0.1 (0.0.0.0 in a container)
    GATEWAY_PORT                       default 8000
    GATEWAY_GRACEFUL_TIMEOUT_SECONDS   default 25
"""

import os

import uvicorn


def server_config(app: object = "app.main:app", **overrides: object) -> uvicorn.Config:
    settings: dict = {
        "host": os.getenv("GATEWAY_HOST", "127.0.0.1"),
        "port": int(os.getenv("GATEWAY_PORT", "8000")),
        "proxy_headers": False,
        "access_log": False,
        "timeout_graceful_shutdown": int(os.getenv("GATEWAY_GRACEFUL_TIMEOUT_SECONDS", "25")),
        "lifespan": "on",  # fail to start if the app's startup fails (bad config)
    }
    settings.update(overrides)
    return uvicorn.Config(app, **settings)


def main() -> None:
    uvicorn.Server(server_config()).run()
