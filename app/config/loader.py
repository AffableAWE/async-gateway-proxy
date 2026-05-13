import yaml

from app.config.models import AppConfig


def load_config(path: str) -> AppConfig:
    with open(path, "r") as file:
        data = yaml.safe_load(file)

    return AppConfig(**data)


def find_upstream(path: str, config: AppConfig):
    matched_route = None

    for route in config.routes:
        if path.startswith(route.path_prefix):

            if (
                matched_route is None
                or len(route.path_prefix) > len(matched_route.path_prefix)
            ):
                matched_route = route

    return matched_route
