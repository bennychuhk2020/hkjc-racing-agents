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


def build_target_url(race_no: int | None = None) -> str:
  """組裝 HKJC 排位表網址，支援指定賽日、場地與場次。"""
  base_url = os.getenv("HKJC_TARGET_URL", DEFAULT_RACECARD_URL).strip()
  race_date = os.getenv("HKJC_RACE_DATE", "").strip()  # 格式: 2026/11/29
  course = os.getenv("HKJC_RACE_COURSE", "").strip().upper()  # ST 或 HV

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


def parse_runners_from_dom(page) -> tuple[str, dict[str, Any]]:
  """直接從 HKJC 排位表網頁 DOM 表格提取馬匹資料。"""
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
        bodySnippet: bodyText.slice(0, 600),
        tables: extractedTables
      };
    }
    """)

  runners: list[dict[str, Any]] = []
  meta = {"distance": "", "course": ""}

  # 嘗試從頁面文字提取途程（例如 1200米 / 1650米）
  body_snippet = eval_result.get("bodySnippet", "")
  dist_match = re.search(r"(\d{4})\s*米", body_snippet)
  if dist_match:
    meta["distance"] = dist_match.group(1)
  if "沙田" in body_snippet:
    meta["course"] = "沙田"
  elif "跑馬地" in body_snippet:
    meta["course"] = "跑馬地"

  # 尋找包含「馬匹編號 / 馬名 / 負磅 / 檔位 / 評分」嘅排位表
  for tbl in eval_result.get("tables", []):
    rows = tbl.get("rows", [])
    if len(rows) < 4:
      continue

    header_idx = -1
    col_map: dict[str, int] = {}

    for r_idx, row in enumerate(rows[:5]):
      joined = " ".join(row)
      if ("馬名" in joined or "Horse" in joined) and (
          "負磅" in joined or "檔位" in joined or "評分" in joined or "Wt" in joined
      ):
        header_idx = r_idx
        for c_idx, col_name in enumerate(row):
          if any(k in col_name for k in ("馬匹編號", "編號", "No")):
            col_map["no"] = c_idx
          elif any(k in col_name for k in ("馬名", "Horse")):
            col_map["name"] = c_idx
          elif any(k in col_name for k in ("負磅", "實際負磅", "Wt")):
            col_map["weight"] = c_idx
          elif any(k in col_name for k in ("檔位", "Draw")):
            col_map["draw"] = c_idx
          elif any(k in col_name for k in ("評分", "Rating", "Rtg")):
            col_map["rating"] = c_idx
          elif any(k in col_name for k in ("獨贏", "賠率", "Win")):
            col_map["odds"] = c_idx
        break

    if header_idx == -1 or "name" not in col_map:
      continue

    for row in rows[header_idx + 1 :]:
      if len(row) <= col_map["name"]:
        continue
      raw_no = row[col_map["no"]] if "no" in col_map and len(row) > col_map["no"] else ""
      horse_no = safe_int(raw_no, -1)
      if not (1 <= horse_no <= 16):
        continue

      horse_name = row[col_map["name"]].strip()
      if not horse_name:
        continue

      weight_lbs = (
          safe_float(row[col_map["weight"]], 125.0)
          if "weight" in col_map and len(row) > col_map["weight"]
          else 125.0
      )
      draw = (
          safe_int(row[col_map["draw"]], 7)
          if "draw" in col_map and len(row) > col_map["draw"]
          else 7
      )
      rating = (
          safe_float(row[col_map["rating"]], 60.0)
          if "rating" in col_map and len(row) > col_map["rating"]
          else 60.0
      )
      win_odds = (
          safe_float(row[col_map["odds"]], 10.0)
          if "odds" in col_map and len(row) > col_map["odds"]
          else 10.0
      )

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
  """抓取 HKJC 排位表（支援多場次自動遍歷及除錯資訊輸出）。"""
  captured_api: list[dict[str, Any]] = []
  debug_pages: list[dict[str, Any]] = []
  races: list[dict[str, Any]] = []
  detected_date = datetime.now(HKT).strftime("%Y-%m-%d")

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

    def handle_response(response: Response) -> None:
      url_l = response.url.lower()
      if any(k in url_l for k in ("graphql", "racecard", "odds", "api")):
        ct = (response.headers.get("content-type") or "").lower()
        if "json" in ct:
          try:
            captured_api.append({
                "url": response.url,
                "payload": response.json(),
            })
          except Exception:
            pass

    page.on("response", handle_response)

    try:
      for race_no in range(1, max_races_to_check + 1):
        target_url = build_target_url(race_no=race_no)
        print(f"Fetching Race {race_no}: {target_url}")
        page.goto(target_url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3500)

        runners, meta, eval_info = parse_runners_from_dom(page)

        if race_no == 1:
          debug_pages.append({
              "race_no": race_no,
              "title": eval_info.get("title"),
              "final_url": eval_info.get("url"),
              "body_snippet": eval_info.get("bodySnippet"),
              "table_count": len(eval_info.get("tables", [])),
              "sample_tables": eval_info.get("tables", [])[:3],
          })
          date_match = re.search(
              r"(\d{2}/\d{2}/\d{4}|\d{4}/\d{2}/\d{2})",
              eval_info.get("bodySnippet", ""),
          )
          if date_match:
            detected_date = date_match.group(1)

        if not runners:
          print(f"No runners found for Race {race_no}, stopping loop.")
          break

        races.append({
            "race_no": race_no,
            "distance": meta.get("distance", ""),
            "course": meta.get("course", ""),
            "runners": runners,
        })
        print(f"Race {race_no}: extracted {len(runners)} runners.")
    finally:
      browser.close()

  RAW_DEBUG_PATH.parent.mkdir(parents=True, exist_ok=True)
  RAW_DEBUG_PATH.write_text(
      json.dumps(
          {
              "page_diagnostics": debug_pages,
              "api_responses": captured_api[:10],
          },
          ensure_ascii=False,
          indent=2,
      ),
      encoding="utf-8",
  )

  result = {
      "race_date": detected_date,
      "source_url": build_target_url(race_no=1),
      "fetched_at_hkt": datetime.now(HKT).isoformat(),
      "graphql_response_count": len(captured_api),
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
