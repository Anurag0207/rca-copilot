# NL-to-Data Insight Platform — Startup Guide

Complete step-by-step instructions to bring the entire stack from zero to a running application.

---

## Stack Overview

| Layer | Service | How It Runs |
|---|---|---|
| Data | **MongoDB** (ecommerce_db + retail_warehouse_db) | Docker container `olist-mongodb` |
| Cache | **Redis** | Docker container |
| Database | **PostgreSQL** | Docker container |
| LLM | **Ollama** (qwen2.5:7b) | Native Windows app |
| API | **FastAPI** backend | Local Python venv |
| UI | **React/Vite** frontend | Local Node dev server |

---

## Step 1 — Start Docker Desktop

Docker Desktop must be running before any containers can start.

1. Open **Docker Desktop** from the Start menu (or taskbar)
2. Wait until the whale icon in the system tray is **steady** (not animating)
3. Verify it works — open a terminal and run:
   ```powershell
   docker ps
   ```
   You should see a table header with no error. If you see `"daemon is not running"`, Docker is still starting — wait 30 more seconds.

---

## Step 2 — Start MongoDB

Your MongoDB container is `olist-mongodb` and already has the full Olist ecommerce dataset loaded. Just start it:

```powershell
docker start olist-mongodb
```

**Verify MongoDB is ready:**
```powershell
docker exec olist-mongodb mongosh --eval "db.adminCommand('ping')" --quiet
```
Expected output: `{ ok: 1 }`

**Verify the data collections exist:**
```powershell
docker exec olist-mongodb mongosh ecommerce_db --eval "db.getCollectionNames().join(', ')" --quiet
```
Expected: 9 collections (customers, orders, order_items, order_payments, order_reviews, products, sellers, geolocation, product_category_name_translation)

**If you've also loaded the Retail Data Warehouse dataset** (see Appendix A), verify it the same way:
```powershell
docker exec olist-mongodb mongosh retail_warehouse_db --eval "db.getCollectionNames().join(', ')" --quiet
```
Expected: 12 collections (customers, stores, employees, categories, suppliers, products, promotions, orders, order_items, payments, shipments, returns)

---

## Step 3 — Start Redis

```powershell
docker run -d `
  --name nl_redis `
  -p 6379:6379 `
  -v nl_redis_data:/data `
  redis:7-alpine `
  redis-server --save 60 1 --loglevel warning
```

**If the container already exists:**
```powershell
docker start nl_redis
```

**Verify Redis is ready:**
```powershell
docker exec nl_redis redis-cli ping
```
Expected output: `PONG`

---

## Step 4 — Start PostgreSQL

```powershell
docker run -d `
  --name nl_pg `
  -p 5432:5432 `
  -e POSTGRES_USER=postgres `
  -e POSTGRES_PASSWORD=devpass `
  -e POSTGRES_DB=nl_insight `
  -v nl_pg_data:/var/lib/postgresql/data `
  postgres:16-alpine
```

**If the container already exists:**
```powershell
docker start nl_pg
```

**Verify PostgreSQL is ready:**
```powershell
docker exec nl_pg pg_isready -U postgres
```
Expected output: `localhost:5432 - accepting connections`

---

## Step 5 — Start Ollama

Open a **dedicated terminal window** and run:

```powershell
ollama serve
```

Leave this terminal open — Ollama must keep running.

**Verify the model is loaded:**
```powershell
ollama list
```
You should see `qwen2.5:7b` in the list. If not, pull it (one-time, ~4.7 GB):
```powershell
ollama pull qwen2.5:7b
```

**Verify Ollama API is responding:**
```powershell
(Invoke-WebRequest -Uri http://localhost:11434/api/tags -UseBasicParsing).StatusCode
```
Expected output: `200`

---

## Step 6 — Start the Backend

Open a **new terminal window**, navigate to the backend, and activate the virtual environment:

```powershell
cd C:\Users\anura\Downloads\nl-data-insight\backend
.\venv\Scripts\Activate.ps1
```

Run the FastAPI dev server:

```powershell
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

**Verify backend is ready:**
```powershell
(Invoke-WebRequest -Uri http://localhost:8000/health -UseBasicParsing).Content
```
Expected: `{"status":"ok",...}`

**What you'll see in the terminal on each user query:**
```
INFO  Intent classified        request_type=data_query  entities=[...]
INFO  Pipeline routing         request_type=data_query
INFO  Query plan generated     query={...}
INFO  MongoDB query executing  collection=...  stage_count=2
INFO  MongoDB query complete   rows_returned=1  first_row={...}
INFO  Query result             rows_returned=1  preview=[...]
```

---

## Step 7 — Start the Frontend

Open a **new terminal window**:

```powershell
cd C:\Users\anura\Downloads\nl-data-insight\frontend
npm run dev
```

The Vite dev server starts on **http://localhost:5173**

Open that URL in your browser. You should see the login screen.

---

## Quick Status Check

Run this in any terminal to see the health of all Docker services:

```powershell
docker ps --format "table {{.Names}}`t{{.Status}}`t{{.Ports}}" --filter "name=olist-mongodb" --filter "name=nl_"
```

Expected output:
```
NAMES           STATUS          PORTS
olist-mongodb   Up X minutes    0.0.0.0:27017->27017/tcp
nl_redis        Up X minutes    0.0.0.0:6379->6379/tcp
nl_pg           Up X minutes    0.0.0.0:5432->5432/tcp
```

---

## Startup Order Summary

```
Docker Desktop  ←  must be open first
      │
      ├── olist-mongodb   (port 27017)
      ├── nl_redis     (port 6379)
      └── nl_pg        (port 5432)

ollama serve             (port 11434)  ←  separate terminal
uvicorn app.main:app     (port 8000)   ←  separate terminal, venv activated
npm run dev              (port 5173)   ←  separate terminal
```

---

## Shutdown (Graceful Stop)

Stop the backend: `Ctrl+C` in the uvicorn terminal  
Stop the frontend: `Ctrl+C` in the npm terminal  
Stop Ollama: `Ctrl+C` in the ollama terminal  
Stop containers (data preserved):
```powershell
docker stop olist-mongodb nl_redis nl_pg
```

---

## Restart After a Previous Run

If you've run the stack before (containers exist, data is loaded):

```powershell
# 1. Start Docker Desktop — wait for it to be ready
# 2. Restart containers
docker start olist-mongodb nl_redis nl_pg

# 3. Ollama (new terminal)
ollama serve

# 4. Backend (new terminal, venv)
cd C:\Users\anura\Downloads\nl-data-insight\backend
.\venv\Scripts\Activate.ps1
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

# 5. Frontend (new terminal)
cd C:\Users\anura\Downloads\nl-data-insight\frontend
npm run dev
```

Open browser at **http://localhost:5173**

---

## Troubleshooting

### "Connection refused" on port 27017 / 6379 / 5432
The Docker container isn't running. Run `docker ps --filter "name=nl_"` to check. Start it with `docker start nl_<name>`.

### "cannot connect to the Docker daemon"
Docker Desktop is not running. Open it from the Start menu and wait for it to be ready.

### Backend starts but LLM calls hang
Ollama is not running. Open a terminal and run `ollama serve`.

### "model not found" error in backend logs
The model hasn't been pulled yet: `ollama pull qwen2.5:7b`

### Intent always classified as `describe_query` for count questions
Restart uvicorn — the fixed intent prompt in `intent_parser.py` may not be loaded if the process wasn't restarted after the last code change.

### Mermaid diagram generation fails
`mmdc` (mermaid-cli) must be installed globally:
```powershell
npm install -g @mermaid-js/mermaid-cli
```
The backend auto-detects `mmdc.cmd` on Windows via `_find_mmdc()`.

### Frontend shows CORS errors
Verify backend is running on port 8000. The `.env` file sets `CORS_ORIGINS=["http://localhost:5173"]` — if you change the frontend port, update `.env` and restart uvicorn.

### Redis session errors after restart
Redis data is persisted in the `nl_redis_data` Docker volume. If you want a clean slate: `docker rm -f nl_redis && docker volume rm nl_redis_data` then re-run Step 3.

---

## Appendix A — First-Time Data Setup

If this is a fresh environment and the MongoDB `ecommerce_db` database is empty:

1. Download the [Brazilian E-Commerce Public Dataset by Olist](https://www.kaggle.com/datasets/olistbr/brazilian-ecommerce) from Kaggle (CSV files)
2. Use `mongoimport` or a Python script to load each CSV into its collection:
   ```powershell
   # Example for one collection:
   mongoimport --host localhost:27017 --db ecommerce_db `
     --collection olist_customers_dataset `
     --type csv --headerline `
     --file olist_customers_dataset.csv
   ```
3. Repeat for all 9 collections (customers, orders, order_items, order_payments, order_reviews, products, sellers, geolocation, product_category_name_translation)

**Verify data is loaded:**
```powershell
docker exec nl_mongodb mongosh ecommerce_db --eval "db.getCollectionNames()"
```

---

## Appendix A2 — Adding the Retail Data Warehouse dataset as a second data source

This loads the [Retail Data Warehouse – 12 Table 1M+ Rows Dataset](https://www.kaggle.com/datasets/datarspectrum/retail-data-warehouse-12-table-1m-rows-dataset)
into the **same** `olist-mongodb` container, as a second database (`retail_warehouse_db`),
selectable per chat session alongside the Olist dataset. No new container needed —
MongoDB hosts multiple databases on one server for free.

1. Download the dataset's 12 CSVs from Kaggle (customers, stores, employees,
   categories, suppliers, products, promotions, orders, order_items, payments,
   shipments, returns).

2. Import each CSV into `retail_warehouse_db` on the existing container:
   ```powershell
   mongoimport --host localhost:27017 --db retail_warehouse_db `
     --collection customers --type csv --headerline --file customers.csv
   # Repeat for the other 11 files, using the CSV's base filename as --collection
   ```

3. Create the join-key indexes (the container's data volume already exists, so
   the `docker-entrypoint-initdb.d` auto-init hook in `mongo-init/create_indexes.js`
   won't fire on its own — run it manually):
   ```powershell
   docker cp mongo-init/create_indexes.js olist-mongodb:/tmp/create_indexes.js
   docker exec olist-mongodb mongosh /tmp/create_indexes.js
   ```
   The retail_warehouse_db indexes are keyed against the dataset's actual CSV
   headers (confirmed 2026-07-08 — see the comment block in that file), not a
   guess.

4. Add `MONGO_CONNECTIONS` to `backend/.env` (see Appendix B) so the backend
   knows both databases exist:
   ```env
   MONGO_CONNECTIONS={"olist":{"uri":"mongodb://localhost:27017","database":"ecommerce_db"},"retail_wh":{"uri":"mongodb://localhost:27017","database":"retail_warehouse_db"}}
   ```
   Restart uvicorn after editing `.env`.

5. Pick the dataset per conversation by passing `connection_id` when creating
   a chat session:
   ```json
   POST /chat/sessions
   { "connection_id": "retail_wh" }
   ```
   Use `"olist"` for the original e-commerce dataset. No code changes are
   needed to query the new dataset — schema discovery, intent parsing, and
   query planning all introspect whatever collections exist in the selected
   database at request time.

---

## Appendix B — Environment Variables

All configuration is in `backend/.env`. Key values for local dev:

```env
MONGODB_URI=mongodb://localhost:27017
MONGODB_DATABASE=ecommerce_db
MONGO_CONNECTIONS={"olist":{"uri":"mongodb://localhost:27017","database":"ecommerce_db"},"retail_wh":{"uri":"mongodb://localhost:27017","database":"retail_warehouse_db"}}
REDIS_URL=redis://localhost:6379/0
DATABASE_URL=postgresql+asyncpg://postgres:devpass@localhost:5432/nl_insight
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_MODEL=qwen2.5:7b
CORS_ORIGINS=["http://localhost:5173"]
```

Do not change these unless you move a service to a different port.
