from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

from playwright.sync_api import sync_playwright, Response

RAW_DEBUG_PATH = Path("data/raw_graphql_debug.json")
OUTPUT_DATA_PATH = Path("data/upcoming_races.json")

HKT = timezone(timedelta(hours=8))

DEFAULT_RACECARD_URL = (
    "https://racing.hkjc.com/zh-hk/local/information/racecard"
)


def build_target_url(
    race_date: str = "", course: str = "", race_no: int | None = None
) -> str:
  """組裝完整 HKJC 排位表網址（必須同時帶 raceDate、Racecourse 先會正確切換場次）。"""
  base_url = os.getenv("HKJC_TARGET_URL", DEFAULT_RACECARD_URL).strip()
  params = []
  if race_date:
    params.append(f"raceDate={race_date}")
  if course in {"ST", "HV"}:
    params.append(f"Racecourse={course}")
  if race_no is not None:
    params.append(f"RaceNo={race_no}")

  if not params:
    return base_url
  sep = "&" if "?" in base_url else "?"
  return f"{base_url}{sep}{'&'.join(params)}"


def safe_float(value: Any, default: float) -> float:
  if value is None:
    return default
  if isinstance(value, (int, float)):
    return float(value)
  text = str(value).strip()
  match = re.search(r"-?\d+(?:\.\d+)?", text.replace(",", ""))
  if not match:
    return default
  try:
    return float(match.group(0))
  except ValueError:
    return default


def safe_int(value: Any, default: int) -> int:
  return int(round(safe_float(value, float(default))))


def detect_meeting_params(page) -> tuple[str, str]:
  """由第 1 場載入後嘅網址或頁面文字，偵測最新賽日 (YYYY/MM/DD) 同場地 (ST/HV)。"""
  info = page.evaluate("""
    () => ({
      url: window.location.href,
      text: document.body ? document.body.innerText.slice(0, 1500) : ""
    })
    """)
  url = info.get("url", "")
  text = info.get("text", "")

  race_date = os.getenv("HKJC_RACE_DATE", "").strip()
  course = os.getenv("HKJC_RACE_COURSE", "").strip().upper()

  if not race_date:
    m_url_date = re.search(r"raceDate=(\d{4}/\d{2}/\d{2})", url, re.I)
    if m_url_date:
      race_date = m_url_date.group(1)
    else:
      m_txt_date = re.search(r"(\d{4})年(\d{1,2})月(\d{1,2})日", text)
      if m_txt_date:
        y, m, d = m_txt_date.groups()
        race_date = f"{y}/{int(m):02d}/{int(d):02d}"
      else:
        m_slash = re.search(r"(\d{2})/(\d{2})/(\d{4})", text)
        if m_slash:
          d, m, y = m_slash.groups()
          race_date = f"{y}/{m}/{d}"

  if course not in {"ST", "HV"}:
    if "Racecourse=HV" in url or "跑馬地" in text[:400]:
      course = "HV"
    else:
      course = "ST"

  return race_date, course


def parse_runners_from_dom(page) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
  """精準解析 HKJC 排位表主表格（排除烙號誤判及評分增減誤判）。"""
  eval_result = page.evaluate("""
    () => {
      const title = document.title || "";
      const bodyText = document.body ? document.body.innerText : "";
      const tables = Array.from(document.querySelectorAll("table"));
      const extractedTables = tables.map((tbl, idx) => {
        const rows = Array.from(tbl.querySelectorAll("tr")).map(tr =>
          Array.from(tr.querySelectorAll("th, td")).map(cell =>
            cell.innerText.replace(/\\s+/g, " ").trim()
          )
        );
        return { index: idx, rowCount: rows.length, rows };
      });
      return {
        title,
        url: window.location.href,
        bodySnippet: bodyText.slice(0, 800),
        tables: extractedTables
      };
    }
    """)

  runners: list[dict[str, Any]] = []
  meta = {"distance": "", "course": ""}

  body_snippet = eval_result.get("bodySnippet", "")
  dist_match = re.search(r"(\d{4})\s*米", body_snippet)
  if dist_match:
    meta["distance"] = dist_match.group(1)
  if "跑馬地" in body_snippet[:300]:
    meta["course"] = "跑馬地"
  elif "沙田" in body_snippet[:300]:
    meta["course"] = "沙田"

  for tbl in eval_result.get("tables", []):
    rows = tbl.get("rows", [])
    if len(rows) < 5:
      continue

    header_idx = -1
    col_map: dict[str, int] = {}

    for r_idx, row in enumerate(rows[:4]):
      joined = " ".join(row)
      # 必須同時包含「馬匹編號/編號」、「馬名」同「負磅/檔位」先係真正排位主表
      if ("馬名" in joined) and ("檔位" in joined) and ("負磅" in joined):
        header_idx = r_idx
        for c_idx, col_name in enumerate(row):
          clean_col = col_name.replace(" ", "")
          if clean_col in {"馬匹編號", "編號", "馬號"}:
            col_map["no"] = c_idx
          elif clean_col == "馬名":
            col_map["name"] = c_idx
          elif clean_col in {"負磅", "實際負磅"}:
            col_map["weight"] = c_idx
          elif clean_col == "檔位":
            col_map["draw"] = c_idx
          elif clean_col == "評分":
            col_map["rating"] = c_idx
          elif "獨贏" in clean_col or "賠率" in clean_col:
            col_map["odds"] = c_idx
        break

    if header_idx == -1 or "no" not in col_map or "name" not in col_map:
      continue

    seen_nos: set[int] = set()
    for row in rows[header_idx + 1 :]:
      if len(row) <= max(col_map.values()):
        continue

      raw_no = row[col_map["no"]].strip()
      # 嚴格要求編號係純數字（1-16），避免抓到空白或合併列
      if not re.fullmatch(r"\d{1,2}", raw_no):
        continue
      horse_no = int(raw_no)
      if not (1 <= horse_no <= 16) or horse_no in seen_nos:
        continue

      horse_name = row[col_map["name"]].strip()
      # 過濾掉烙號（例如 J394、L085）
      if not horse_name or re.fullmatch(r"[A-Za-z]\d{3}", horse_name):
        continue

      weight_lbs = (
          safe_float(row[col_map["weight"]], 125.0)
          if "weight" in col_map
          else 125.0
      )
      # 香港賽馬負磅合理範圍 103 - 135 磅，若超出表示抓錯欄，用預設值
      if not (100.0 <= weight_lbs <= 140.0):
        weight_lbs = 125.0

      draw = (
          safe_int(row[col_map["draw"]], 7)
          if "draw" in col_map
          else 7
      )
      if not (1 <= draw <= 16):
        draw = 7

      rating = (
          safe_float(row[col_map["rating"]], 60.0)
          if "rating" in col_map
          else 60.0
      )
      if not (10.0 <= rating <= 150.0):
        rating = 60.0

      win_odds = (
          safe_float(row[col_map["odds"]], 10.0)
          if "odds" in col_map
          else 10.0
      )

      seen_nos.add(horse_no)
      runners.append({
          "no": horse_no,
          "name": horse_name,
          "draw": draw,
          "weight_lbs": weight_lbs,
          "rating": rating,
          "win_odds": win_odds,
      })

    if len(runners) >= 4:
      break

  return runners, meta, eval_result


def fetch_hkjc_race_data() -> dict[str, Any]:
  """先載入預設最新排位表取得賽日與場地，再逐場抓取第 1 場至最後一場。"""
  races: list[dict[str, Any]] = []
  debug_summary: list[dict[str, Any]] = []
  max_races_to_check = int(os.getenv("HKJC_MAX_RACES", "11"))

  with sync_playwright() as p:
    browser = p.chromium.launch(
        headless=True,
        args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
    )
    context = browser.new_context(
        locale="zh-HK",
        timezone_id="Asia/Hong_Kong",
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
        viewport={"width": 1440, "height": 900},
    )
    page = context.new_page()

    def route_handler(route):
      if route.request.resource_type in {"image", "media", "font"}:
        route.abort()
      else:
        route.continue_()

    page.route("**/*", route_handler)

    try:
      # 第一步：載入預設排位頁面，自動識別下一賽日日期同場地 (ST/HV)
      initial_url = build_target_url(
          race_date=os.getenv("HKJC_RACE_DATE", "").strip(),
          course=os.getenv("HKJC_RACE_COURSE", "").strip().upper(),
          race_no=1,
      )
      print(f"Loading initial page: {initial_url}")
      page.goto(initial_url, wait_until="domcontentloaded", timeout=60000)
      page.wait_for_timeout(3500)

      race_date, course = detect_meeting_params(page)
      print(f"Detected meeting -> race_date={race_date}, course={course}")

      for race_no in range(1, max_races_to_check + 1):
        target_url = build_target_url(
            race_date=race_date, course=course, race_no=race_no
        )
        if race_no > 1:
          print(f"Fetching Race {race_no}: {target_url}")
          page.goto(target_url, wait_until="domcontentloaded", timeout=60000)
          page.wait_for_timeout(3000)

        runners, meta, eval_info = parse_runners_from_dom(page)

        if race_no in {1, 2}:
          # 記錄第1、2場主表表頭方便核對
          matching_headers = []
          for t in eval_info.get("tables", []):
            if t.get("rows") and any("馬名" in " ".join(r) for r in t["rows"][:3]):
              matching_headers.append(t["rows"][:3])
          debug_summary.append({
              "race_no": race_no,
              "url": eval_info.get("url"),
              "distance": meta.get("distance"),
              "runner_count": len(runners),
              "table_headers": matching_headers,
          })

        if not runners:
          print(f"No runners found for Race {race_no}, stopping loop.")
          break

        # 防止網頁重導向回第 1 場造成重複抓取
        if races and runners[0]["name"] == races[0]["runners"][0]["name"] and len(runners) == len(races[0]["runners"]):
          print(f"Race {race_no} returned identical runners to Race 1, stopping.")
          break

        races.append({
            "race_no": race_no,
            "distance": meta.get("distance", ""),
            "course": meta.get("course", "") or ("跑馬地" if course == "HV" else "沙田"),
            "runners": runners,
        })
        print(f"Race {race_no}: extracted {len(runners)} runners.")
    finally:
      browser.close()

  RAW_DEBUG_PATH.parent.mkdir(parents=True, exist_ok=True)
  RAW_DEBUG_PATH.write_text(
      json.dumps(debug_summary, ensure_ascii=False, indent=2),
      encoding="utf-8",
  )

  result = {
      "race_date": race_date or datetime.now(HKT).strftime("%Y-%m-%d"),
      "course_code": course,
      "source_url": build_target_url(race_date=race_date, course=course, race_no=1),
      "fetched_at_hkt": datetime.now(HKT).isoformat(),
      "races": races,
  }

  OUTPUT_DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
  OUTPUT_DATA_PATH.write_text(
      json.dumps(result, ensure_ascii=False, indent=2),
      encoding="utf-8",
  )
  print(f"Saved {len(races)} races to {OUTPUT_DATA_PATH}")
  return result


if __name__ == "__main__":
  fetch_hkjc_race_data()
