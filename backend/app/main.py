from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import models  # noqa: F401  -- registers tables on Base.metadata
from app.api import router
from app.config import settings
from app.database import Base, engine
from app.matrix import load_decision_matrix
from explain.explain import Explainer


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    matrix = load_decision_matrix()
    settings.matrix_version = matrix.matrix_version
    app.state.decision_matrix = matrix
    # One Explainer per process: the explanation cache and the API-call
    # counter are process-wide, which is what makes "500 decisions, ~20
    # API calls" true across a whole run rather than per request.
    app.state.explainer = Explainer()
    yield


app = FastAPI(title="Kairo", lifespan=lifespan)
app.include_router(router)


@app.get("/health")
def health():
    return {
        "status": "ok",
        "engine_version": settings.engine_version,
        "matrix_version": settings.matrix_version,
    }
