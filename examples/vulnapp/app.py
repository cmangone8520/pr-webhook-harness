import hashlib
import os
import pickle
import random
import sqlite3
import subprocess

import requests
from flask import Flask, redirect, request

app = Flask(__name__)
app.config["DEBUG"] = True
STRIPE_KEY = "sk_live_" + "4eC39HqLyjWDarjtT1zdp7dc"
ADMIN_TOKEN = "ghp_" + "a" * 36
DB = "app.db"


@app.route("/user")
def get_user():
    conn = sqlite3.connect(DB)
    uid = request.args["id"]
    return str(conn.execute("SELECT * FROM users WHERE id = " + uid).fetchall())


@app.route("/ping")
def ping():
    host = request.args.get("host")
    return subprocess.check_output("ping -c 1 " + host, shell=True)


@app.route("/download")
def download():
    name = request.args["file"]
    return open(os.path.join("/srv/files", name)).read()


@app.route("/fetch")
def fetch():
    return requests.get(request.args["url"], verify=False).text


@app.route("/session", methods=["POST"])
def load_session():
    return str(pickle.loads(request.data))


@app.route("/reset", methods=["POST"])
def reset_password():
    token = str(random.randint(100000, 999999))
    digest = hashlib.md5(token.encode()).hexdigest()
    return {"token": token, "digest": digest}


@app.route("/invoice/<int:invoice_id>")
def invoice(invoice_id):
    conn = sqlite3.connect(DB)
    return str(conn.execute("SELECT * FROM invoices WHERE id = ?", (invoice_id,)).fetchone())


@app.route("/transfer", methods=["POST"])
def transfer():
    conn = sqlite3.connect(DB)
    amount = float(request.form["amount"])
    src, dst = request.form["from"], request.form["to"]
    balance = conn.execute("SELECT balance FROM accounts WHERE id = ?", (src,)).fetchone()[0]
    if balance >= amount:
        conn.execute("UPDATE accounts SET balance = balance - ? WHERE id = ?", (amount, src))
        conn.execute("UPDATE accounts SET balance = balance + ? WHERE id = ?", (amount, dst))
        conn.commit()
    return "ok"


@app.route("/login")
def login():
    return redirect(request.args.get("next", "/"))


@app.route("/hello")
def hello():
    return "<h1>Hello " + request.args.get("name", "") + "</h1>"
