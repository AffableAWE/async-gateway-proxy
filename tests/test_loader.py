from app.config.loader import load_config, find_upstream


config = load_config("config.yaml")

route = find_upstream("/service-a/hello", config)

print(route)

assert route is not None
assert route.upstream == "http://localhost:9001"


