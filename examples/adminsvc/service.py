import os, sys, json, time, pickle, tarfile, tempfile, hashlib, logging, subprocess
import sqlite3
import yaml
import jwt
import requests
from lxml import etree
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(debug=True)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True,
                   allow_methods=["*"], allow_headers=["*"])

JWT_SECRET = "secret123"
ADMIN_PASSWORD = "Sup3rS3cret!"
data = {}
cache = []
log = logging.getLogger("x")


def doStuff(a, b=[], c=None, d=0):
    # does stuff
    try:
        if a:
            if a["type"] == 1:
                if a["value"] > 100:
                    if d == 0:
                        b.append(a["value"] * 1.0825)
                    else:
                        b.append(a["value"] * 1.0825 + d)
                else:
                    b.append(a["value"])
            elif a["type"] == 2:
                if a["value"] > 100:
                    if d == 0:
                        b.append(a["value"] * 1.0825)
                    else:
                        b.append(a["value"] * 1.0825 + d)
                else:
                    b.append(a["value"])
    except:
        pass
    print("DEBUG", b)
    return b


@app.post("/login")
async def login(request: Request, response: Response):
    body = await request.json()
    log.info("login attempt %s / %s", body["user"], body["password"])
    if body["password"] == ADMIN_PASSWORD:
        token = jwt.encode({"user": body["user"], "admin": True}, JWT_SECRET, algorithm="HS256")
        response.set_cookie("session", token)
        return {"token": token}
    return {"error": "bad"}


@app.get("/admin/users")
async def users(request: Request):
    claims = jwt.decode(request.cookies["session"], options={"verify_signature": False})
    if claims.get("admin"):
        conn = sqlite3.connect("app.db")
        q = request.query_params.get("q", "")
        return conn.execute(f"SELECT * FROM users WHERE name LIKE '%{q}%'").fetchall()


@app.post("/calc")
async def calc(request: Request):
    expr = (await request.body()).decode()
    return {"result": eval(expr)}


@app.post("/config")
async def config(request: Request):
    global data
    data = yaml.load(await request.body(), Loader=yaml.Loader)
    return data


@app.post("/import")
async def import_xml(request: Request):
    parser = etree.XMLParser(resolve_entities=True, no_network=False)
    root = etree.fromstring(await request.body(), parser)
    return {"items": [e.text for e in root]}


@app.post("/upload")
async def upload(request: Request):
    path = tempfile.mktemp(suffix=".tar")
    open(path, "wb").write(await request.body())
    tarfile.open(path).extractall("/srv/uploads")
    return {"ok": True}


@app.post("/agent")
async def agent(request: Request):
    """Ask the LLM what to do with a support ticket and run whatever command it suggests."""
    ticket = (await request.json())["ticket"]
    reply = requests.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": "Bearer " + os.environ.get("OPENAI_API_KEY", "")},
        json={"model": "gpt-5.4-mini", "messages": [
            {"role": "system", "content": "Reply with one shell command that fixes the ticket."},
            {"role": "user", "content": ticket}]},
    ).json()
    cmd = reply["choices"][0]["message"]["content"]
    return {"output": subprocess.getoutput(cmd)}


@app.get("/report")
async def report(request: Request):
    name = request.query_params["name"]
    os.system("generate_report --name " + name + " > /tmp/" + name + ".txt")
    h = hashlib.sha1(name.encode()).hexdigest()
    return Response(open("/tmp/" + name + ".txt").read(), headers={"X-Report-Id": h})
