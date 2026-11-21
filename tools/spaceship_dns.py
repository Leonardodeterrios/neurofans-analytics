#!/usr/bin/env python3
"""Переводит короткие домены партнёров (.shop) с Cloudflare на DNS Spaceship
и ставит A-запись @ → IP сервера. Только стандартная библиотека Python.

Запуск на сервере:  python3 tools/spaceship_dns.py
Ключ и секрет из Spaceship → API Manager спрашиваются при запуске (ввод скрыт),
никуда не сохраняются. Сначала показывает план, меняет только после «да».
Права ключа: Domains (nameservers) — write, DNS records — read + write.
"""
import getpass, json, re, sys, urllib.error, urllib.request
from pathlib import Path

API = "https://spaceship.dev/api/v1"
IP = "188.130.251.125"


def hosts_from_caddyfile():
    """{домен: [имена записей]} из таблицы map в Caddyfile: '@' для самого домена, 'go' для go.домен и т.д."""
    text = (Path(__file__).resolve().parent.parent / "Caddyfile").read_text(encoding="utf-8")
    out = {}
    for h in re.findall(r"^\t\t([a-z0-9.-]+\.shop)\s", text, re.M):
        parts = h.split(".")
        root, name = ".".join(parts[-2:]), (".".join(parts[:-2]) or "@")
        out.setdefault(root, []).append(name)
    return dict(sorted(out.items()))


def call(method, path, key, secret, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API + path, data=data, method=method, headers={
        "X-API-Key": key, "X-API-Secret": secret, "Content-Type": "application/json",
        "Accept": "application/json", "User-Agent": "curl/8.5.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read().decode() or "{}"
            return r.status, json.loads(raw) if raw.strip().startswith(("{", "[")) else raw
    except urllib.error.HTTPError as e:
        txt = e.read().decode(errors="replace")
        if "<html" in txt.lower():  # страница защиты, а не ответ API
            t = re.search(r"<title>(.*?)</title>", txt, re.S | re.I)
            ray = re.search(r"Ray ID:?\s*(?:<[^>]+>\s*)*([0-9a-f]+)", txt, re.I)
            txt = "HTML: " + (t.group(1).strip() if t else "без заголовка") + (f" (Ray {ray.group(1)})" if ray else "")
        return e.code, txt[:300]


def main():
    hosts = hosts_from_caddyfile()
    doms = list(hosts)
    names = sorted({n for v in hosts.values() for n in v}, key=lambda n: (n != "@", n))
    print(f"Доменов: {len(doms)}. Для каждого:")
    print("  1) NS → Spaceship (basic)")
    print("  2) удалить старые записи @ типа A / AAAA / CNAME (парковка)")
    print(f"  3) A-записи {', '.join(names)} → {IP}\n  " + ", ".join(doms))
    key = getpass.getpass("\nAPI key (ввод скрыт): ").strip()
    secret = getpass.getpass("API secret (ввод скрыт): ").strip()
    if input("Применить? Напиши да: ").strip().lower() not in ("да", "yes", "y"):
        print("Отменено, ничего не менял.")
        return
    bad = []
    for d in doms:
        st, res = call("PUT", f"/domains/{d}/nameservers", key, secret, {"provider": "basic"})
        if st in (401, 403):
            if isinstance(res, str) and res.startswith("HTML"):
                print(f"Spaceship не пускает запросы с этого компьютера ({st}): {res}")
                print("Запусти скрипт на своём Mac — команда есть в чате у Claude.")
            else:
                print(f"Ключ не подходит или без прав ({st}): {res}")
            sys.exit(1)
        ns_ok = st < 300 or (st == 422 and "already used the same provider" in str(res))  # уже на Spaceship
        st2, lst = call("GET", f"/dns/records/{d}?take=500&skip=0", key, secret)
        old = []
        if st2 < 300 and isinstance(lst, dict):
            for r in lst.get("items", []):
                if r.get("name") == "@" and r.get("type") in ("A", "AAAA", "CNAME") \
                        and not (r.get("type") == "A" and r.get("address") == IP):
                    old.append({k: v for k, v in r.items() if k != "ttl"})
        if old:
            call("DELETE", f"/dns/records/{d}", key, secret, old)
        st3, res3 = call("PUT", f"/dns/records/{d}", key, secret, {
            "force": True, "items": [{"type": "A", "name": "@", "address": IP, "ttl": 600}]})
        ok = ns_ok and st3 < 300
        print(f"{'✅' if ok else '❌'} {d:<20} NS:{st}  удалено старых:{len(old)}  A({len(hosts[d])}):{st3}"
              + ("" if ok else f"  {res if not ns_ok else res3}"))
        if not ok:
            bad.append(d)
    print("\nГотово." if not bad else f"\nНе получилось: {', '.join(bad)} — пришли вывод Claude.")


if __name__ == "__main__":
    main()
