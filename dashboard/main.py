from fastapi import FastAPI, Header, HTTPException, BackgroundTasks, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
import json
from pathlib import Path
import asyncio  # Import asyncio for sleep
import os
import logging
import redis
import httpx
import ipaddress

app = FastAPI()

# Load API key securely from environment variables
LOCAL_API_KEY = os.getenv("LOCAL_API_KEY")
CLOUD_API_KEY = os.getenv("CLOUD_API_KEY")
# 🌐 Local server details
# LOCAL_SERVER_URL = os.getenv("LOCAL_SERVER_URL")
LOCAL_SERVER_PORT = os.getenv("LOCAL_SERVER_PORT")
REDIS_URL = os.getenv("REDIS_URL")


# 🚨 Ensure API keys and local server URL are set
if not LOCAL_API_KEY:
    raise ValueError("🚨 LOCAL_API_KEY is missing! Set it as an environment variable.")
if not CLOUD_API_KEY:
    raise ValueError("🚨 CLOUD_API_KEY is missing! Set it as an environment variable.")
if not LOCAL_SERVER_PORT:
    raise ValueError("🚨 LOCAL_SERVER_PORT is missing! Set it as an environment variable.")
if not REDIS_URL:
    raise ValueError("🚨 REDIS_URL is missing! Set it as an environment variable."
                     )

# 📂 Static assets directory
STATIC_DIR = Path(__file__).parent / "static"
INDEX_FILE = STATIC_DIR / "index.html"

# 🏠 Serve static files (CSS, JS, favicon, etc.)
app.mount("/static", StaticFiles(directory=STATIC_DIR, html=True), name="static")
# 🛠 Setup Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


app.state.latest_data = {}


redis_client = redis.Redis.from_url(REDIS_URL, decode_responses=True)

def save_cached_data():
    """Save latest_data to Render Redis Key-Value Store."""
    try:
        redis_client.set("cached_data", json.dumps(app.state.latest_data))  # Save data as a JSON string
        logger.info("✅ Cached data saved to Redis.")
    except Exception as e:
        logger.error(f"🚨 Exception in saving cached data: {str(e)}")

def load_cached_data():
    """Load cached data from Render Redis Key-Value Store."""
    try:
        data = redis_client.get("cached_data")  # Retrieve data from Redis
        if data:
            app.state.latest_data = json.loads(data)  # Convert JSON string back to dictionary
            logger.info("✅ Loaded cached data from Redis.")
        else:
            app.state.latest_data = {}  # Default to an empty dictionary if no data is found
            logger.warning("⚠️ No cached data found in Redis.")
    except Exception as e:
        app.state.latest_data = {}
        logger.error(f"🚨 Failed to load cached data: {str(e)}")

from fastapi.middleware.cors import CORSMiddleware

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
allow_headers=[
    "*"
])




# 📌 Serve the frontend dashboard
@app.get("/")
async def serve_frontend():
    return FileResponse(INDEX_FILE)


@app.head("/", include_in_schema=False)
async def serve_head():
    return Response(status_code=200)

# Serve the favicon
@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return FileResponse(STATIC_DIR / "favicon.ico")

@app.get("/health_check")
async def health_check():
    return {"status": "ok"}
# 🔐 API Key Verification
def verify_cloud_api_key(x_cloud_api_key: str):
    
    """Verify that the provided Cloud API key matches the expected value."""
    if x_cloud_api_key != CLOUD_API_KEY:
        logger.warning("❌ Unauthorized attempt with an invalid CLOUD API Key")
        raise HTTPException(status_code=403, detail="Invalid CLOUD API Key")


# # 🔄 API: Local server requests data retrieval
# @app.get("/retrieve_data_from_local")
# def retrieve_data_from_local(background_tasks: BackgroundTasks, x_cloud_api_key: str = Header(..., alias="x-cloud-api-key")):
#     """Triggered by the local server to retrieve and cache the latest data."""
    
#     if not x_cloud_api_key:
#         raise HTTPException(status_code=403, detail="CLOUD API Key required")
    
#     verify_cloud_api_key(x_cloud_api_key)

#     try:
#         # start a background task to fetch data from the local server in fastapi
#         background_tasks.add_task(fetch_dashboard_data)
#         logger.info("✅ Data successfully retrieved from local server and stored in memory.")
#         return {"message": "✅ Data retrieved from local server and stored in memory"}

#     except requests.RequestException as e:
#         logger.error(f"🚨 Retrieval failed: {str(e)}")
#         return {"error": f"🚨 Retrieval failed: {str(e)}"}

# 🔄 API: Local server requests data retrieval
def is_valid_ip(ip_addr):
    try:
        # This converts the IP to an IPv4 or IPv6 address object and checks validity
        ip = ipaddress.ip_address(ip_addr)
        # Add additional checks for private, reserved, or specific allowed ranges if necessary
        return not ip.is_private and not ip.is_reserved
    except ValueError:
        return False
    

@app.post("/retrieve_data_from_local")
async def retrieve_data_from_local(request: Request, background_tasks: BackgroundTasks, x_cloud_api_key: str = Header(..., alias="x-cloud-api-key")):
    """Triggered by the local server to retrieve and cache the latest data."""
    
    if not x_cloud_api_key:
        raise HTTPException(status_code=403, detail="CLOUD API Key required")
    
    verify_cloud_api_key(x_cloud_api_key)

    data = await request.json()
    external_ip = data.get("external_ip")

    if not external_ip or not is_valid_ip(external_ip):
        raise HTTPException(status_code=400, detail="External IP is required.")

    # Dynamically set LOCAL_SERVER_URL using the provided external IP
    local_server_url = f"https://{external_ip}:{LOCAL_SERVER_PORT}"

    background_tasks.add_task(fetch_dashboard_data, local_server_url)
    logger.info("✅ Background task initiated.")
    return {"message": "✅ Data retrieval initiated from local server."}


@app.post("/ingest")
async def ingest_data(request: Request, x_cloud_api_key: str = Header(..., alias="x-cloud-api-key")):
    """Receives data pushed directly from the local server."""

    if not x_cloud_api_key:
        raise HTTPException(status_code=403, detail="CLOUD API Key required")

    verify_cloud_api_key(x_cloud_api_key)

    data = await request.json()

    # Store using same keys as fetch_dashboard_data
    for key in ["progress", "complexity_warnings", "heatmap_data_weekly", "heatmap_data_monthly", "heatmap_data_yearly", "meta"]:
        if key in data:
            app.state.latest_data[key] = data[key]
            logger.info(f"✅ Stored {key}")

    save_cached_data()
    return {"message": "✅ Data ingested successfully"}


# async def fetch_dashboard_data(local_server_url):
#     await asyncio.sleep(10)
#     app.state.app.state.latest_data
#     headers = {"x-local-api-key": LOCAL_API_KEY,
#                    'accept': 'application/json'}

#     # # 🔍 Fetch data from local server
#     # progress_response = requests.get(f"{local_server_url}/progress", headers=headers, verify=False)
#     # complexity_response = requests.get(f"{local_server_url}/complexity_warnings", headers=headers, verify=False)
#     # heatmap_response_weekly = requests.get(f"{local_server_url}/heatmap/data?period=weekly", headers=headers, verify=False)
#     # heatmap_response_monthly = requests.get(f"{local_server_url}/heatmap/data?period=monthly", headers=headers, verify=False)
#     # heatmap_response_yearly = requests.get(f"{local_server_url}/heatmap/data?period=yearly", headers=headers, verify=False)
#     async with httpx.AsyncClient(verify=False, timeout=200) as client:
#         progress_response = await client.get(f"{local_server_url}/progress", headers=headers)
#         complexity_response = await client.get(f"{local_server_url}/complexity_warnings", headers=headers)
#         heatmap_response_weekly = await client.get(f"{local_server_url}/heatmap/data?period=weekly", headers=headers)
#         heatmap_response_monthly = await client.get(f"{local_server_url}/heatmap/data?period=monthly", headers=headers)
#         heatmap_response_yearly = await client.get(f"{local_server_url}/heatmap/data?period=yearly", headers=headers)

#     if progress_response.status_code == 200:
#         latest_data["progress"] = progress_response.json()
#     else:
#         logger.warning("⚠️ Failed to retrieve /progress data from local server.")

#     if complexity_response.status_code == 200:
#         latest_data["complexity_warnings"] = complexity_response.json()
#     else:
#         logger.warning("⚠️ Failed to retrieve /complexity_warnings data from local server.")

#     if heatmap_response_weekly.status_code == 200:
#         latest_data["heatmap_data_weekly"] = heatmap_response_weekly.json()
#     else:
#         logger.warning("⚠️ Failed to retrieve weekly heatmap data from local server .")
    
#     if heatmap_response_monthly.status_code == 200:
#         latest_data["heatmap_data_monthly"] = heatmap_response_monthly.json()
#     else:
#         logger.warning("⚠️ Failed to retrieve monthly heatmap data from local server.")

#     if heatmap_response_yearly.status_code == 200:
#         latest_data["heatmap_data_yearly"] = heatmap_response_yearly.json()
#     else:
#         logger.warning("⚠️ Failed to retrieve yearly heatmap data from local server.")

   
#     save_cached_data()

async def fetch_dashboard_data(local_server_url):
    await asyncio.sleep(10)
    headers = {"x-local-api-key": LOCAL_API_KEY, 'accept': 'application/json'}

    endpoints = {
        "progress": "progress",
        "complexity_warnings": "complexity_warnings",
        "heatmap_data_weekly": "heatmap/data?period=weekly",
        "heatmap_data_monthly": "heatmap/data?period=monthly",
        "heatmap_data_yearly": "heatmap/data?period=yearly"
    }

    async with httpx.AsyncClient(verify=False, timeout=200) as client:
        for key, endpoint in endpoints.items():
            try:
                response = await client.get(f"{local_server_url}/{endpoint}", headers=headers)
                response.raise_for_status()
                app.state.latest_data[key] = response.json()
            except httpx.HTTPStatusError as e:
                logger.warning(f"⚠️ HTTP error for {endpoint}: {e.response.status_code} - {e.response.text}")
            except httpx.RequestError as e:
                logger.warning(f"⚠️ Failed to retrieve {endpoint} data: {e}")

    save_cached_data()
    
# 📊 API: Serve cached progress data (Requires CLOUD API Key)
@app.get("/progress")
async def get_progress(x_cloud_api_key: str = Header(..., alias="x-cloud-api-key")):
    """Returns the latest stored progress data."""
    verify_cloud_api_key(x_cloud_api_key)
    return app.state.latest_data.get("progress", {"error": "No progress data available. Please trigger retrieval from the local server."})


@app.get("/project_info")
async def project_info(x_cloud_api_key: str = Header(..., alias="x-cloud-api-key")):
    """Returns the latest stored project metadata (for page title)."""
    verify_cloud_api_key(x_cloud_api_key)
    return app.state.latest_data.get("meta", {})

# 🔎 API: Serve cached high-complexity function warnings (Requires CLOUD API Key)
@app.get("/complexity_warnings")
async def get_complexity_warnings(x_cloud_api_key: str = Header(..., alias="x-cloud-api-key")):
    """Returns the latest stored high-complexity function data."""
    verify_cloud_api_key(x_cloud_api_key)
    return app.state.latest_data.get("complexity_warnings", {"error": "No complexity warnings available. Please trigger retrieval from the local server."})

# 🔥 API: Serve cached heatmap data (Requires CLOUD API Key)
@app.get("/heatmap/data")
async def get_heatmap_data(x_cloud_api_key: str = Header(..., alias="x-cloud-api-key"), period: str = "monthly"):
    """Returns the latest stored heatmap data."""
    verify_cloud_api_key(x_cloud_api_key)
    if period == "monthly":
        return app.state.latest_data.get("heatmap_data_monthly",{"error": f"No heatmap data available for {period}."})
    elif period == "yearly":
        return app.state.latest_data.get("heatmap_data_yearly",{"error": f"No heatmap data available for {period}."})
    elif period == "weekly":
        return app.state.latest_data.get("heatmap_data_weekly",{"error": f"No heatmap data available for {period}."})
    else:
        return {"error": "Invalid period. Use 'monthly', 'yearly', or 'weekly'."}





@app.on_event("startup")
async def startup_event():
    try:
        redis_client.ping()  # Check Redis connection
        load_cached_data()  # Load cached data from Redis
        logger.info("🚀 Redis connected, and application started successfully!")
    except redis.exceptions.ConnectionError:
        logger.error("🚨 Redis connection failed!")
