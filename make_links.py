#!/usr/bin/env python3
"""Генератор партнёрских ссылок по двум командам и двум каналам.

Для каждого партнёра две ссылки на одном домене:
  BASE_URL/t/<метка>  — для Threads
  BASE_URL/v/<метка>  — для видео (Shorts / Reels / TikTok)
Канал виден в дашборде, поэтому можно сравнивать конверсию Threads и видео.
BASE_URL = адрес сервера (позже — https://ваш-домен). Пишет links.csv."""
import os, csv
from dotenv import load_dotenv
load_dotenv()

def env(name, default=""):
    return os.getenv(name, default).split("  #")[0].strip() or default

BASE_URL      = env("BASE_URL", "http://188.130.251.125").rstrip("/")
TEAM_A_NAME   = env("TEAM_A_NAME", "Команда Лёни")
TEAM_A_PREFIX = env("TEAM_A_PREFIX", "leo")
TEAM_B_NAME   = env("TEAM_B_NAME", "Команда Кати")
TEAM_B_PREFIX = env("TEAM_B_PREFIX", "kate")
PER_TEAM      = int(env("PER_TEAM", "50"))

def rows():
    out = []
    for name, prefix in ((TEAM_A_NAME, TEAM_A_PREFIX), (TEAM_B_NAME, TEAM_B_PREFIX)):
        for i in range(1, PER_TEAM + 1):
            label = f"{prefix}{i:02d}"
            out.append((name, label, f"{BASE_URL}/t/{label}", f"{BASE_URL}/v/{label}"))
    return out

if __name__ == "__main__":
    data = rows()
    with open("links.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["team", "label", "threads_link", "video_link"])
        w.writerows(data)
    print(f"{'КОМАНДА':<16}{'МЕТКА':<9}{'THREADS':<42}ВИДЕО")
    print("-" * 110)
    for team, label, t, v in data:
        print(f"{team:<16}{label:<9}{t:<42}{v}")
    print(f"\nПартнёров: {len(data)}  ->  сохранено в links.csv")
