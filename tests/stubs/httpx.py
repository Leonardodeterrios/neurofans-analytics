CALLS=[]
def post(url, json=None, timeout=None, **k):
    CALLS.append(json)
    class R: status_code=200
    return R()
