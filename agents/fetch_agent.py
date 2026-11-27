from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

from playwright.sync_api import sync_playwright

RAW_DEBUG_PATH = Path("data/raw_graphql_debug.json")
OUTPUT_DATA_PATH = Path("data/upcoming_races.json")

HKT = timezone(timedelta(hours=8))

DEFAULT_RACECARD_URL = (
    "https://racing.hkjc.com/zh-hk/local/information/racecard"
)


def build_target_url(
    race_date: str = "", course: str = "", race_no: int | None = None
) -> str:
  """組裝完整 HKJC 排位表網址（同時帶 raceDate、Racecourse、RaceNo）。"""
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


def clean_person_name(raw_text: str, default: str = "N/A") -> str:
  """清理騎師／練馬師文字（例如去除括號內讓磅數字或多餘空白）。"""
  cleaned = re.sub(r"\s+", " ", str(raw_text or "")).strip()
  if not cleaned or cleaned in {"-", "--", "N/A"}:
    return default
  # 保留中文人名或英文名，移除純括號負磅標註如 (2) 或 (-5)
  cleaned_no_paren = re.sub(r"\s*\([+-]?\d+\)\s*$", "", cleaned).strip()
  return cleaned_no_paren or cleaned


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


def extract_race_meta(body_text: str, default_course: str) -> dict[str, str]:
  """從頁面文字提取班次、途程及跑道特色。"""
  snippet = re.sub(r"\s+", " ", body_text or "")[:1200]

  meta: dict[str, str] = {
      "distance": "",
      "course": "跑馬地" if default_course == "HV" else "沙田",
      "class_name": "N/A",
      "track_feature": "標準賽道",
  }

  dist_match = re.search(r"(\d{3,4})\s*米", snippet)
  if dist_match:
    meta["distance"] = dist_match.group(1)

  if "跑馬地" in snippet[:350]:
    meta["course"] = "跑馬地"
  elif "沙田" in snippet[:350]:
    meta["course"] = "沙田"

  # 班次辨識（例如 第五班、第四班、第三班、第二班、第一班、表列賽、分級賽）
  class_match = re.search(
      r"(第[一二三四五六]班|公開賽|表列賽|三級賽|二級賽|一級賽)",
      snippet,
  )
  if class_match:
    meta["class_name"] = class_match.group(1)

  # 跑道特色辨識（例如 "C+3" 賽道、"C" 賽道、"B+2" 賽道、全天候跑道、草地）
  track_match = re.search(
      r'["“]?(A\+3|B\+2|C\+3|A|B|C)["”]?\s*賽道',
      snippet,
  )
  if track_match:
    meta["track_feature"] = f'"{track_match.group(1)}" 賽道'
  elif "全天候" in snippet or "泥地" in snippet:
    meta["track_feature"] = "全天候跑道"
  elif "草地" in snippet:
    meta["track_feature"] = "草地跑道"

  return meta


def parse_runners_from_dom(
    page, default_course: str
) -> tuple[list[dict[str, Any]], dict[str, str], dict[str, Any]]:
  """精準解析 HKJC 排位表主表格（馬號、馬名、檔位、負磅、評分、騎師、練馬師、近績）。"""
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
        bodySnippet: bodyText.slice(0, 1400),
        tables: extractedTables
      };
    }
    """)

  runners: list[dict[str, Any]] = []
  meta = extract_race_meta(eval_result.get("bodySnippet", ""), default_course)

  for tbl in eval_result.get("tables", []):
    rows = tbl.get("rows", [])
    if len(rows) < 5:
      continue

    header_idx = -1
    col_map: dict[str, int] = {}

    for r_idx, row in enumerate(rows[:4]):
      joined = " ".join(row)
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
          elif clean_col == "騎師":
            col_map["jockey"] = c_idx
          elif clean_col == "練馬師":
            col_map["trainer"] = c_idx
          elif "近績" in clean_col or "往績" in clean_col:
            col_map["recent_runs"] = c_idx
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
      if not re.fullmatch(r"\d{1,2}", raw_no):
        continue
      horse_no = int(raw_no)
      if not (1 <= horse_no <= 16) or horse_no in seen_nos:
        continue

      horse_name = row[col_map["name"]].strip()
      if not horse_name or re.fullmatch(r"[A-Za-z]\d{3}", horse_name):
        continue

      weight_lbs = (
          safe_float(row[col_map["weight"]], 125.0)
          if "weight" in col_map
          else 125.0
      )
      if not (100.0 <= weight_lbs <= 140.0):
        weight_lbs = 125.0

      draw = safe_int(row[col_map["draw"]], 7) if "draw" in col_map else 7
      if not (1 <= draw <= 16):
        draw = 7

      rating = (
          safe_float(row[col_map["rating"]], 60.0)
          if "rating" in col_map
          else 60.0
      )
      if not (10.0 <= rating <= 150.0):
        rating = 60.0

      jockey = (
          clean_person_name(row[col_map["jockey"]], "N/A")
          if "jockey" in col_map
          else "N/A"
      )
      trainer = (
          clean_person_name(row[col_map["trainer"]], "N/A")
          if "trainer" in col_map
          else "N/A"
      )
      recent_runs = (
          row[col_map["recent_runs"]].strip() or "N/A"
          if "recent_runs" in col_map
          else "N/A"
      )
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
          "jockey": jockey,
          "trainer": trainer,
          "recent_runs": recent_runs,
          "running_style": "前置機動",
          "body_weight": "N/A",
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

        runners, meta, eval_info = parse_runners_from_dom(page, course)

        if race_no in {1, 2}:
          matching_headers = []
          for t in eval_info.get("tables", []):
            if t.get("rows") and any(
                "馬名" in " ".join(r) for r in t["rows"][:3]
            ):
              matching_headers.append(t["rows"][:3])
          debug_summary.append({
              "race_no": race_no,
              "url": eval_info.get("url"),
              "meta": meta,
              "runner_count": len(runners),
              "table_headers": matching_headers,
          })

        if not runners:
          print(f"No runners found for Race {race_no}, stopping loop.")
          break

        if (
            races
            and runners[0]["name"] == races[0]["runners"][0]["name"]
            and len(runners) == len(races[0]["runners"])
        ):
          print(
              f"Race {race_no} returned identical runners to Race 1, stopping."
          )
          break

        races.append({
            "race_no": race_no,
            "distance": meta.get("distance", ""),
            "course": meta.get("course", ""),
            "class_name": meta.get("class_name", "N/A"),
            "track_feature": meta.get("track_feature", "標準賽道"),
            "runners": runners,
        })
        print(
            f"Race {race_no}: extracted {len(runners)} runners "
            f"({meta.get('class_name')}, {meta.get('track_feature')})."
        )
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
      "source_url": build_target_url(
          race_date=race_date, course=course, race_no=1
      ),
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
