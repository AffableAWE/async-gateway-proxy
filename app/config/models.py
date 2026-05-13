from pydantic import BaseModel
from typing import List


class RouteConfig(BaseModel):
    path_prefix: str
    upstream: str


class AppConfig(BaseModel):
    routes: List[RouteConfig]
