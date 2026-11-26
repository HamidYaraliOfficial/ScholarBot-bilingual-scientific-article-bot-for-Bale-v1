from fastapi import APIRouter

from app.dashboard.routes import articles, auth, overview, people, sources, system

router = APIRouter()
for module in (auth, overview, articles, people, sources, system):
    router.include_router(module.router)
