#!/usr/bin/env python3
"""Генератор партнёрских ссылок по двум командам.
Команда A (leoNN) и команда B (kateNN). Ставишь ссылку партнёру в био.
BASE_URL = адрес сервера. Печатает таблицу и пишет links.csv."""
import os, csv
from dotenv import load_dotenv
load_dotenv()

BASE_URL      = os.getenv("BASE_URL", "http://188.130.251.125").rstrip("/")
TEAM_A_NAME   = os.getenv("TEAM_A_NAME", "Команда Лёни")
TEAM_A_PREFIX = os.getenv("TEAM_A_PREFIX", "leo")
TEAM_B_NAME   = os.getenv("TEAM_B_NAME", "Команда Кати")
TEAM_B_PREFIX = os.getenv("TEAM_B_PREFIX", "kate")
PER_TEAM      = int(os.getenv("PER_TEAM", "50"))

def rows():
    out = []
    for name, prefix in ((TEAM_A_NAME, TEAM_A_PREFIX), (TEAM_B_NAME, TEAM_B_PREFIX)):
        for i in range(1, PER_TEAM + 1):
            label = f"{prefix}{i:02d}"
            out.append((name, label, f"{BASE_URL}/go?source={label}"))
    return out

if __name__ == "__main__":
    data = rows()
    with open("links.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f); w.writerow(["team", "label", "link"]); w.writerows(data)
    print(f"{'КОМАНДА':<16}{'МЕТКА':<10}ССЫЛКА В БИО")
    print("-" * 70)
    for team, label, link in data:
        print(f"{team:<16}{label:<10}{link}")
    print(f"\nВсего ссылок: {len(data)}  ->  сохранено в links.csv")
