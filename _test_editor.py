import json
import os

os.environ.setdefault("DEV_MODE", "true")

import app as a

a.app.config["TESTING"] = True
a.app.config["SECRET_KEY"] = "test"

with a.app.test_client() as c:
    with c.session_transaction() as sess:
        sess["user_id"] = "bee184d78a9ec1c6"
    r = c.get("/saved_sets/582e117c7422dfed/edit")
    print("status", r.status_code)
    html = r.data.decode()
    marker = 'id="questions-data"'
    start = html.find(marker)
    end = html.find("</script>", start)
    chunk = html[start:end]
    print("script content length:", len(chunk))
    # extract JSON between > and end
    json_start = chunk.find(">") + 1
    data = chunk[json_start:]
    questions = json.loads(data)
    print("questions loaded:", len(questions))
    q0 = questions[0]
    print("q0 type:", q0["type"], "answer:", repr(q0["answer"]))
    idx = q0["choices"].index(q0["answer"])
    print("expected _ansIdx:", idx)

    # simulate initAnswerState + renderMCQ selection flags
    for q in questions:
        if q["type"] == "mcq":
            ans_idx = q["choices"].index(q["answer"]) if q["answer"] in q["choices"] else -1
            if ans_idx < 0:
                ans_idx = None
            dots = ["●" if ans_idx == i else "○" for i in range(4)]
            print("MCQ:", q["question"][:40], "->", dots)
