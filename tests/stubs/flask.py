import json
class Response:
    def __init__(self, content='', mimetype=None, headers=None, status=200):
        self.content=content; self.mimetype=mimetype; self.headers=dict(headers or {}); self.status=status; self.cookies={}
    def set_cookie(self, key, value, **kw): self.cookies[key]=(value,kw)
class _Abort(Exception):
    def __init__(self, code): self.code=code
def abort(code): raise _Abort(code)
def jsonify(*a, **k):
    data = k if k else (a[0] if a else {})
    return Response(json.dumps(data), 'application/json')
def redirect(location, code=302): return Response('', status=code, headers={'Location': location})
class _Headers(dict):
    def get(self, k, d=''): return dict.get(self, k, d)
class _Req:
    def __init__(self): self.args={}; self.values={}; self.headers=_Headers(); self.remote_addr='1.2.3.4'; self.cookies={}
request=_Req()
class Flask:
    def __init__(self, name): pass
    def route(self, *a, **k):
        def deco(f): return f
        return deco
    def run(self, **k): pass
