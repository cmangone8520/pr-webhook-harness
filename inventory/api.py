import os
import sqlite3
import subprocess
import hashlib
import json

from flask import Flask, request, jsonify, send_file

app = Flask(__name__)
DB = "inventory.db"
ADMIN_PASSWORD = "Sup3rS3cretAdm1n!"
cache = {}


def db():
    return sqlite3.connect(DB)


@app.route("/items")
def list_items():
    category = request.args.get("category", "")
    conn = db()
    rows = conn.execute("SELECT id, name, qty, price FROM items WHERE category = ?", (category,)).fetchall()
    return jsonify(rows)


@app.route("/items/<item_id>", methods=["DELETE"])
def delete_item(item_id):
    conn = db()
    conn.execute("DELETE FROM items WHERE id = ?", (item_id,))
    conn.commit()
    return "", 204


@app.route("/export")
def export():
    name = request.args.get("file")
    return send_file(os.path.join("/srv/exports", name))


@app.route("/backup", methods=["POST"])
def backup():
    target = request.json["target"]
    subprocess.call(["tar", "czf", target, "/srv/data"])
    return "ok"


@app.route("/login", methods=["POST"])
def login():
    pw = request.form["password"]
    if hashlib.md5(pw.encode()).hexdigest() == hashlib.md5(ADMIN_PASSWORD.encode()).hexdigest():
        return jsonify({"token": "admin-" + pw})
    return jsonify({"error": "bad password"}), 401


@app.route("/restock", methods=["POST"])
def restock():
    data = json.loads(request.data)
    conn = db()
    try:
        for item in data["items"]:
            row = conn.execute("SELECT qty FROM items WHERE id = ?", (item["id"],)).fetchone()
            conn.execute("UPDATE items SET qty = ? WHERE id = ?", (row[0] + item["qty"], item["id"]))
        conn.commit()
    except:
        pass
    return "ok"


if __name__ == "__main__":
    app.run(host="127.0.0.1")
