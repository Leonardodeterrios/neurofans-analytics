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
        if name not in out.setdefault(root, []):
            out[root].append(name)
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
        want = [{"type": "A", "name": n, "address": IP, "ttl": 600} for n in hosts[d]]
        st3, res3 = call("PUT", f"/dns/records/{d}", key, secret, {"force": True, "items": want})
        # проверка: что реально лежит в зоне; недостающие — по одной, с ответом API
        def have():
            s4, l4 = call("GET", f"/dns/records/{d}?take=500&skip=0", key, secret)
            return {r.get("name") for r in (l4.get("items", []) if isinstance(l4, dict) else [])
                    if r.get("type") == "A" and r.get("address") == IP}
        missing = [n for n in hosts[d] if n not in have()]
        errs = []
        for n in missing:
            s5, r5 = call("PUT", f"/dns/records/{d}", key, secret,
                          {"force": True, "items": [{"type": "A", "name": n, "address": IP, "ttl": 600}]})
            if s5 >= 300:
                errs.append(f"{n}:{s5} {r5}")
        if missing:
            missing = [n for n in hosts[d] if n not in have()]
        ok = ns_ok and not missing
        print(f"{'✅' if ok else '❌'} {d:<20} NS:{st}  удалено старых:{len(old)}  "
              f"A-записей есть:{len(hosts[d]) - len(missing)}/{len(hosts[d])}"
              + (f"  нет: {', '.join(missing)} (в зоне A→IP: {', '.join(sorted(have())) or 'пусто'})" if missing else "")
              + (f"  {' | '.join(errs)}" if errs else "")
              + ("" if ns_ok else f"  NS: {res}"))
        if not ok:
            bad.append(d)
    print("\nГотово." if not bad else f"\nНе получилось: {', '.join(bad)} — пришли вывод Claude.")


if __name__ == "__main__":
    main()
