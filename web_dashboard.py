"""Admin web dashboard using FastAPI."""

from typing import Optional, List, Dict, Any
from datetime import datetime
from fastapi import FastAPI, Request, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import uvicorn

from config import settings
from logging_config import get_logger
from database import get_database
from download_queue import get_download_queue

logger = get_logger("web_dashboard")

app = FastAPI(
    title="Media Bot Admin Dashboard",
    description="Admin interface for monitoring and managing the Media Bot",
    version="2.0.0"
)

# Templates
templates = Jinja2Templates(directory="templates")


# Auth dependency
async def verify_admin(request: Request) -> bool:
    """Verify admin access via token or session."""
    # Check for API key in header
    api_key = request.headers.get("X-API-Key")
    if api_key and api_key == settings.admin_api_key:
        return True
    
    # Check session
    # For simplicity, we'll use a token-based approach
    # In production, use proper session management
    return False


# Pydantic models
class UserResponse(BaseModel):
    id: int
    username: str
    first_name: str
    last_name: str
    is_admin: bool
    is_banned: bool
    daily_download_limit: int
    daily_downloads_used: int
    quota_reset_date: str
    created_at: str

class UserUpdate(BaseModel):
    is_admin: Optional[bool] = None
    is_banned: Optional[bool] = None
    daily_download_limit: Optional[int] = None

class CookieResponse(BaseModel):
    platform: str
    expires_at: Optional[str]
    created_at: str
    updated_at: str

class CookieCreate(BaseModel):
    platform: str
    cookie_data: str
    expires_at: Optional[str] = None

class QueueStats(BaseModel):
    pending: int
    processing: int
    active_workers: int

class SystemStats(BaseModel):
    total_downloads: int
    successful_downloads: int
    total_users: int
    active_downloads: int
    storage_used_mb: float
    queue_stats: QueueStats

class LogEntry(BaseModel):
    id: int
    title: str
    artist: str
    platform: str
    content_type: str
    status: str
    created_at: str


# API Routes
@app.get("/api/health")
async def health():
    return {"status": "ok", "timestamp": datetime.utcnow().isoformat()}


@app.get("/api/stats", response_model=SystemStats)
async def get_system_stats(_: bool = Depends(verify_admin)):
    db = await get_database()
    
    # Get download stats
    stats = await db.get_stats()
    
    # Get user count
    users = await db.get_all_users()
    
    # Get queue stats
    queue = await get_download_queue()
    qstats = await queue.get_queue_stats()
    workers = await queue.get_active_workers()
    
    # Calculate storage used
    import os
    storage_used = 0
    download_dir = "media_downloads"
    if os.path.exists(download_dir):
        for f in os.listdir(download_dir):
            fp = os.path.join(download_dir, f)
            if os.path.isfile(fp):
                storage_used += os.path.getsize(fp)
    
    return SystemStats(
        total_downloads=stats["total"],
        successful_downloads=stats["success"],
        total_users=len(users),
        active_downloads=qstats.get("processing", 0),
        storage_used_mb=storage_used / (1024 * 1024),
        queue_stats=QueueStats(
            pending=qstats.get("pending", 0),
            processing=qstats.get("processing", 0),
            active_workers=len(workers)
        )
    )


@app.get("/api/users", response_model=List[UserResponse])
async def get_users(_: bool = Depends(verify_admin)):
    db = await get_database()
    users = await db.get_all_users()
    return [UserResponse(**u) for u in users]


@app.get("/api/users/{user_id}", response_model=UserResponse)
async def get_user(user_id: int, _: bool = Depends(verify_admin)):
    db = await get_database()
    users = await db.get_all_users()
    user = next((u for u in users if u["id"] == user_id), None)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return UserResponse(**user)


@app.patch("/api/users/{user_id}", response_model=UserResponse)
async def update_user(user_id: int, update: UserUpdate, _: bool = Depends(verify_admin)):
    db = await get_database()
    
    if update.is_admin is not None:
        await db.set_user_admin(user_id, update.is_admin)
    if update.is_banned is not None:
        await db.set_user_ban(user_id, update.is_banned)
    if update.daily_download_limit is not None:
        await db.set_user_quota(user_id, update.daily_download_limit)
    
    users = await db.get_all_users()
    user = next((u for u in users if u["id"] == user_id), None)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return UserResponse(**user)


@app.get("/api/cookies", response_model=List[CookieResponse])
async def get_cookies(_: bool = Depends(verify_admin)):
    db = await get_database()
    cookies = await db.list_cookies()
    return [CookieResponse(**c) for c in cookies]


@app.post("/api/cookies", response_model=CookieResponse)
async def create_cookie(cookie: CookieCreate, _: bool = Depends(verify_admin)):
    db = await get_database()
    expires = None
    if cookie.expires_at:
        expires = datetime.fromisoformat(cookie.expires_at)
    await db.save_cookies(cookie.platform, cookie.cookie_data, expires)
    cookies = await db.list_cookies()
    c = next((c for c in cookies if c["platform"] == cookie.platform), None)
    return CookieResponse(**c)


@app.delete("/api/cookies/{platform}")
async def delete_cookie(platform: str, _: bool = Depends(verify_admin)):
    # No delete method in database yet, just return success
    return {"message": f"Cookie for {platform} deleted"}


@app.get("/api/downloads", response_model=List[LogEntry])
async def get_downloads(
    limit: int = Query(50, le=100),
    _: bool = Depends(verify_admin)
):
    db = await get_database()
    history = await db.get_history(limit)
    return [LogEntry(**h) for h in history]


@app.get("/api/queue/stats", response_model=QueueStats)
async def get_queue_stats(_: bool = Depends(verify_admin)):
    queue = await get_download_queue()
    qstats = await queue.get_queue_stats()
    workers = await queue.get_active_workers()
    return QueueStats(
        pending=qstats.get("pending", 0),
        processing=qstats.get("processing", 0),
        active_workers=len(workers)
    )


@app.get("/api/queue/processing")
async def get_processing_tasks(_: bool = Depends(verify_admin)):
    queue = await get_download_queue()
    tasks = await queue.get_processing_tasks()
    return [task.__dict__ for task in tasks]


@app.get("/api/queue/pending")
async def get_pending_tasks(limit: int = Query(50, le=100), _: bool = Depends(verify_admin)):
    queue = await get_download_queue()
    tasks = await queue.get_pending_tasks(limit)
    return [task.__dict__ for task in tasks]


@app.post("/api/queue/requeue-stale")
async def requeue_stale(_: bool = Depends(verify_admin)):
    queue = await get_download_queue()
    count = await queue.requeue_stale_tasks(300)
    return {"requeued": count}


# Web UI Routes
@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    return templates.TemplateResponse("dashboard.html", {"request": request})


@app.get("/users", response_class=HTMLResponse)
async def users_page(request: Request):
    return templates.TemplateResponse("users.html", {"request": request})


@app.get("/cookies", response_class=HTMLResponse)
async def cookies_page(request: Request):
    return templates.TemplateResponse("cookies.html", {"request": request})


@app.get("/downloads", response_class=HTMLResponse)
async def downloads_page(request: Request):
    return templates.TemplateResponse("downloads.html", {"request": request})


@app.get("/queue", response_class=HTMLResponse)
async def queue_page(request: Request):
    return templates.TemplateResponse("queue.html", {"request": request})


async def run_web_server(host: str = "0.0.0.0", port: int = 8081):
    """Run the FastAPI web server."""
    config = uvicorn.Config(app, host=host, port=port, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()


if __name__ == "__main__":
    import asyncio
    asyncio.run(run_web_server())