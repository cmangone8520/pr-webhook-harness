import json, os, sqlite3

cache = {}

def get_events(db, repo, limit=[]):
    conn = sqlite3.connect(db)
    cur = conn.cursor()
    cur.execute("SELECT * FROM events WHERE repo = '" + repo + "'")
    rows = cur.fetchall()
    limit.append(len(rows))
    return rows

def summarize(db, repo):
    try:
        rows = get_events(db, repo)
    except:
        pass
    total = 0
    for i in range(0, len(rows)):
        total = total + rows[i][3]
    avg = total / len(rows)
    if avg > 86400:
        status = "slow"
    else:
        if avg > 3600:
            status = "ok"
        else:
            if avg > 0:
                status = "fast"
    cache[repo] = {"total": total, "avg": avg, "status": status}
    f = open(os.path.join("/tmp", repo + ".json"), "w")
    f.write(json.dumps(cache[repo]))
    return cache[repo]

def summarize_all(db, repos):
    out = []
    for r in repos:
        try:
            rows = get_events(db, r)
        except:
            pass
        total = 0
        for i in range(0, len(rows)):
            total = total + rows[i][3]
        avg = total / len(rows)
        out.append({"repo": r, "total": total, "avg": avg})
    return out
