#!/usr/bin/env python3
"""Генератор ссылок с меткой-источником (метка = имя профиля). BOT_USERNAME из .env или впиши ниже."""
import os, re
from dotenv import load_dotenv
load_dotenv()

BOT_USERNAME = os.getenv("BOT_USERNAME", "AccessGrantedGuide_bot")
PROFILES = ["Threads_DE_01", "Threads_DE_02", "Threads_DE_03"]  # <-- твои профили

_T = {'а':'a','б':'b','в':'v','г':'g','д':'d','е':'e','ё':'e','ж':'zh','з':'z','и':'i','й':'y',
 'к':'k','л':'l','м':'m','н':'n','о':'o','п':'p','р':'r','с':'s','т':'t','у':'u','ф':'f','х':'h',
 'ц':'c','ч':'ch','ш':'sh','щ':'sch','ъ':'','ы':'y','ь':'','э':'e','ю':'yu','я':'ya'}
def _tr(s): return ''.join(_T.get(c.lower(), c) if c.lower() in _T else c for c in s)
def tag(name):
    t = re.sub(r'_+','_', re.sub(r'[^A-Za-z0-9_-]','_', _tr(name.strip()))).strip('_')
    return t[:64]
def build(profiles):
    out, used = {}, set()
    for i,p in enumerate(profiles,1):
        base = tag(p) or f"acc{i:02d}"; t=base; n=1
        while t in used: n+=1; t=f"{base}-{n}"[:64]
        used.add(t); out[p]=t
    return out
if __name__ == "__main__":
    tags = build(PROFILES)
    print(f"{'ПРОФИЛЬ':<24}{'МЕТКА':<20}ССЫЛКА В БИО")
    print("-"*84)
    for p in PROFILES:
        print(f"{p:<24}{tags[p]:<20}https://t.me/{BOT_USERNAME}?start={tags[p]}")
