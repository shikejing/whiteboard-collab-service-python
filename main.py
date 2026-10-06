import uvicorn
from fastapi import FastAPI

from whiteboard.api import router as board_router

app = FastAPI(title="whiteboard-collab-service")
app.include_router(board_router)


@app.get("/")
def read_root():
    return {"service": "whiteboard-collab", "status": "ok"}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=0)
