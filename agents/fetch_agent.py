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

# 可按需要調整 HKJC 賽馬排位／賠率入口網址
DEFAULT_RACECARD_URL = (
    "https://racing.hkjc.com/zh-hk/local/information/racecard"
)


def build_target_url() -> str:
  """根據環境變數組裝目標網址（可選 raceDate 與 Racecourse）。"""
  base_url = os.getenv("HKJC_TARGET_URL", DEFAULT_RACECARD_URL).strip()
  race_date = os.getenv("HKJC_RACE_DATE", "").strip()  # 格式例: 2026/11/29
  course = os.getenv("HKJC_RACE_COURSE", "").strip().upper()  # ST 或 HV

  params = []
  if race_date:
    params.append(f"raceDate={race_date}")
  if course in {"ST", "HV"}:
    params.append(f"Racecourse={course}")

  if not params:
    return base_url

  sep = "&" if "?" in base_url else "?"
  return f"{base_url}{sep}{'&'.join(params)}"


def is_graphql_response(response: Response) -> bool:
  """判斷是否為 HKJC GraphQL API 回應。"""
  url = response.url.lower()
  if "graphql" not in url:
    return False
  content_type = (response.headers.get("content-type") or "").lower()
  return "json" in content_type or "graphql" in url


def safe_float(value: Any, default: float) -> float:
  if value is None:
    return default
  if isinstance(value, (int, float)):
    return float(value)
  text = str(value).strip()
  # 處理例如 "128 lbs"、"128"、"SCR" 等情況
  match = re.search(r"-?\d+(?:\.\d+)?", text.replace(",", ""))
  if not match:
    return default
  try:
    return float(match.group(0))
  except ValueError:
    return default


def safe_int(value: Any, default: int) -> int:
  return int(round(safe_float(value, float(default))))


def extract_odds_value(runner_obj: dict[str, Any]) -> float:
  """嘗試由不同可能欄位提取獨贏賠率。"""
  candidate_keys = [
      "winOdds",
      "win_odds",
      "odds",
      "currentOdds",
      "winDividend",
  ]
  for key in candidate_keys:
    if key in runner_obj and runner_obj[key] not in (None, "", "-"):
      val = runner_obj[key]
      if isinstance(val, dict):
        for sub_k in ("oddsValue", "value", "odds", "winOdds"):
          if sub_k in val and val[sub_k] not in (None, "", "-"):
            return safe_float(val[sub_k], 10.0)
      else:
        return safe_float(val, 10.0)
  return 10.0


def normalize_runner(raw_runner: dict[str, Any], fallback_idx: int) -> dict[str, Any]:
  """將單匹馬的原始欄位標準化。"""
  no = safe_int(
      raw_runner.get("horseNo")
      or raw_runner.get("no")
      or raw_runner.get("runnerNo")
      or raw_runner.get("clothNo"),
      fallback_idx,
  )

  name_obj = raw_runner.get("name_ch") or raw_runner.get("horseName") or raw_runner.get("name")
  if isinstance(name_obj, dict):
    name = (
        name_obj.get("ch")
        or name_obj.get("tc")
        or name_obj.get("en")
        or f"Horse_{no}"
    )
  else:
    name = str(name_obj or f"Horse_{no}").strip()

  draw = safe_int(
      raw_runner.get("draw")
      or raw_runner.get("barrier")
      or raw_runner.get("drawNo"),
      7,
  )

  weight_lbs = safe_float(
      raw_runner.get("weight")
      or raw_runner.get("actualWt")
      or raw_runner.get("wt")
      or raw_runner.get("weight_lbs"),
      125.0,
  )

  rating = safe_float(
      raw_runner.get("rating")
      or raw_runner.get("currentRating")
      or raw_runner.get("rtg"),
      60.0,
  )

  win_odds = extract_odds_value(raw_runner)

  return {
      "no": no,
      "name": name,
      "draw": draw,
      "weight_lbs": weight_lbs,
      "rating": rating,
      "win_odds": win_odds,
  }


def looks_like_runner_list(items: Any) -> bool:
  """判斷 list 是否為馬匹出賽名單。"""
  if not isinstance(items, list) or len(items) < 2:
    return False
  dict_items = [x for x in items if isinstance(x, dict)]
  if len(dict_items) < 2:
    return False
  runner_hints = {
      "horseno",
      "runnerno",
      "clothno",
      "draw",
      "barrier",
      "actualwt",
      "rating",
      "winodds",
      "jockey",
      "trainer",
  }
  matches = 0
  for d in dict_items[:5]:
    lower_keys = {k.lower() for k in d.keys()}
    if lower_keys & runner_hints:
      matches += 1
  return matches >= 2


def collect_races_from_payloads(payloads: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
  """遞歸搜尋攔截到的 GraphQL JSON，萃取出賽日與各場馬匹資料。"""
  detected_date = ""
  races_by_no: dict[int, dict[str, Any]] = {}

  def walk(node: Any, current_race_no: int | None = None, current_meta: dict[str, Any] | None = None) -> None:
    nonlocal detected_date
    meta = dict(current_meta or {})

    if isinstance(node, dict):
      # 嘗試更新賽日
      for date_key in ("raceDate", "date", "meetingDate"):
        if node.get(date_key) and not detected_date:
          detected_date = str(node[date_key])

      # 嘗試更新場次編號、途程、跑道
      race_no = current_race_no
      for r_key in ("raceNo", "raceNumber", "no"):
        if r_key in node and node[r_key] is not None:
          candidate_no = safe_int(node[r_key], -1)
          if 1 <= candidate_no <= 14:
            race_no = candidate_no
            break

      if "distance" in node and node["distance"]:
        meta["distance"] = str(node["distance"])
      if "course" in node and node["course"]:
        meta["course"] = str(node["course"])
      elif "track" in node and node["track"]:
        meta["course"] = str(node["track"])

      for k, v in node.items():
        if looks_like_runner_list(v):
          r_no = race_no or (len(races_by_no) + 1)
          normalized = [
              normalize_runner(item, idx + 1)
              for idx, item in enumerate(v)
              if isinstance(item, dict)
          ]
          # 去重並按馬號排序
          dedup: dict[int, dict[str, Any]] = {}
          for item in normalized:
            dedup[item["no"]] = item
          runner_list = sorted(dedup.values(), key=lambda x: x["no"])

          if r_no not in races_by_no or len(runner_list) > len(races_by_no[r_no]["runners"]):
            races_by_no[r_no] = {
                "race_no": r_no,
                "distance": meta.get("distance", ""),
                "course": meta.get("course", ""),
                "runners": runner_list,
            }
        else:
          walk(v, race_no, meta)

    elif isinstance(node, list):
      for item in node:
        walk(item, current_race_no, meta)

  for p in payloads:
    walk(p)

  sorted_races = [races_by_no[k] for k in sorted(races_by_no.keys())]
  if not detected_date:
    detected_date = datetime.now(HKT).strftime("%Y-%m-%d")
  return detected_date, sorted_races


def fetch_hkjc_race_data() -> dict[str, Any]:
  """使用 Playwright 載入 HKJC 頁面並攔截 GraphQL API 回應。"""
  target_url = build_target_url()
  captured_responses: list[dict[str, Any]] = []

  print(f"Launching Playwright for URL: {target_url}")

  with sync_playwright() as p:
    browser = p.chromium.launch(
        headless=True,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
        ],
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

    # 遮蔽大型圖片與字型以加快 GitHub Runner 載入速度，同時保留 JS 與 XHR/Fetch
    def route_handler(route):
      if route.request.resource_type in {"image", "media", "font"}:
        route.abort()
      else:
        route.continue_()

    page.route("**/*", route_handler)

    def handle_response(response: Response) -> None:
      if not is_graphql_response(response):
        return
      try:
        data = response.json()
        if isinstance(data, dict) and data:
          captured_responses.append({
              "url": response.url,
              "payload": data,
          })
          print(f"Captured GraphQL response from: {response.url}")
      except Exception as exc:
        print(f"Failed to parse JSON from {response.url}: {exc}")

    page.on("response", handle_response)

    try:
      page.goto(target_url, wait_until="domcontentloaded", timeout=60000)
      # 等待 GraphQL 請求完成與動態元件渲染
      page.wait_for_timeout(8000)
      try:
        page.wait_for_load_state("networkidle", timeout=15000)
      except Exception:
        pass
      page.wait_for_timeout(2000)
    finally:
      browser.close()

  # 儲存原始 GraphQL 回應方便除錯與檢視欄位
  RAW_DEBUG_PATH.parent.mkdir(parents=True, exist_ok=True)
  RAW_DEBUG_PATH.write_text(
      json.dumps(captured_responses, ensure_ascii=False, indent=2),
      encoding="utf-8",
  )
  print(
      f"Saved {len(captured_responses)} GraphQL payloads to {RAW_DEBUG_PATH}"
  )

  payload_bodies = [item["payload"] for item in captured_responses]
  race_date, races = collect_races_from_payloads(payload_bodies)

  result = {
      "race_date": race_date,
      "source_url": target_url,
      "fetched_at_hkt": datetime.now(HKT).isoformat(),
      "graphql_response_count": len(captured_responses),
      "races": races,
  }

  OUTPUT_DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
  OUTPUT_DATA_PATH.write_text(
      json.dumps(result, ensure_ascii=False, indent=2),
      encoding="utf-8",
  )
  print(
      f"Wrote {len(races)} races for {race_date} to {OUTPUT_DATA_PATH}"
  )
  return result


if __name__ == "__main__":
  fetch_hkjc_race_data()
